"""Vertex AI wrapper with a fake client (no Google account needed)."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from google.genai import errors

from lc_images import config, vertex
from lc_images.gemini import GenerationFailed, QuotaExhausted

config.VERTEX_SECONDS_BETWEEN_CALLS = 0
vertex.time.sleep = lambda s: None


class FakeModels:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def image_reply(data=b"PNG"):
    part = NS(inline_data=NS(data=data))
    return NS(candidates=[NS(content=NS(parts=[part]), finish_reason="STOP")], prompt_feedback=None)


def make(replies, tmp):
    client = NS(models=FakeModels(replies))
    return GeminiVertex(project="p", client=client, out_dir=tmp, log=lambda *a: None), client.models


GeminiVertex = vertex.GeminiVertex


def test_image_ratio_and_spend():
    with tempfile.TemporaryDirectory() as tmp:
        g, m = make([image_reply(b"IMG")], tmp)
        assert g.generate([b"jpeg"], "prompt", "4:5") == b"IMG"
        model, contents, cfg = m.calls[0]
        assert model == config.VERTEX_IMAGE_MODEL and cfg.image_config.aspect_ratio == "4:5" and cfg.response_modalities == ["IMAGE"]
        assert contents[-1] == "prompt" and contents[0].inline_data.mime_type == "image/jpeg"
        assert g.spend.data["usd"] == config.VERTEX_PRICE_PER_IMAGE and (Path(tmp) / "vertex_spend.json").exists()


def test_rate_limit_waits_then_works():
    with tempfile.TemporaryDirectory() as tmp:
        g, m = make([errors.ClientError(429, {"error": {"message": "Resource exhausted"}}), image_reply()], tmp)
        assert g.generate([], "p", "1:1") == b"PNG" and len(m.calls) == 2


def test_stuck_request_times_out_and_is_retried():
    import httpx

    with tempfile.TemporaryDirectory() as tmp:
        g, m = make([httpx.ReadTimeout("timed out"), image_reply()], tmp)
        assert g.generate([], "p", "1:1") == b"PNG" and len(m.calls) == 2


def test_rate_limit_that_never_ends_stops_the_run():
    with tempfile.TemporaryDirectory() as tmp:
        g, _ = make([errors.ClientError(429, {"error": {"message": "Resource exhausted"}})] * 10, tmp)
        try:
            g.generate([], "p", "1:1")
            raise AssertionError("expected the run to stop")
        except QuotaExhausted as error:
            assert "too many requests" in str(error)


def test_busy_checker_uses_a_backup_model():
    busy = errors.ClientError(429, {"error": {"message": "Resource exhausted"}})
    with tempfile.TemporaryDirectory() as tmp:
        g, m = make([busy] * (len(config.VERTEX_RATE_LIMIT_WAITS) + 1) + [NS(text='{"pass": true, "problems": []}')], tmp)
        assert g.ask_json([b"a"], "check")["pass"] is True
        assert m.calls[-1][0] == config.VERTEX_CHECK_BACKUPS[0]


def test_no_image_is_generation_failed():
    with tempfile.TemporaryDirectory() as tmp:
        g, _ = make([NS(candidates=[NS(content=None, finish_reason="IMAGE_SAFETY")], prompt_feedback=None)], tmp)
        try:
            g.generate([], "p", "1:1")
            raise AssertionError("expected GenerationFailed")
        except GenerationFailed as error:
            assert "IMAGE_SAFETY" in str(error)


def test_spending_cap_stops_before_the_call():
    with tempfile.TemporaryDirectory() as tmp:
        g, m = make([image_reply()], tmp)
        g.spend.data["usd"] = config.VERTEX_BUDGET_USD - 0.01
        try:
            g.generate([], "p", "1:1")
            raise AssertionError("expected the cap to stop it")
        except QuotaExhausted:
            assert m.calls == []


def test_list_models():
    with tempfile.TemporaryDirectory() as tmp:
        g, m = make([], tmp)
        m.list = lambda config: [NS(name="publishers/google/models/gemini-x-image", display_name="X Image")]
        assert g.list_models() == [("gemini-x-image", "X Image")]


def test_shared_spend_is_never_wiped_when_it_cannot_be_read():
    import json as _json

    class Shop:
        def __init__(self):
            self.v, self.fail = {"vertex_spend": {"usd": 200.0}}, False

        def get_shop_json(self, key, default=None):
            if self.fail:
                raise RuntimeError("Shopify down")
            return _json.loads(_json.dumps(self.v.get(key, default)))

        def set_shop_json(self, key, value):
            self.v[key] = value

    shop = Shop()
    with tempfile.TemporaryDirectory() as tmp:
        sp = vertex.Spend(Path(tmp) / "s.json", shop)
        assert sp.data["usd"] == 200.0
        shop.fail = True
        sp.add(0.05, "checks")
        sp.flush()
        assert shop.v["vertex_spend"]["usd"] == 200.0  # not overwritten with 0.05
        shop.fail = False
        sp.flush()
        assert shop.v["vertex_spend"]["usd"] == 200.05


def test_json_check():
    with tempfile.TemporaryDirectory() as tmp:
        g, m = make([NS(text='{"pass": true, "problems": []}')], tmp)
        assert g.ask_json([b"a", b"b"], "check") == {"pass": True, "problems": []}
        assert m.calls[0][0] == config.VERTEX_CHECK_MODEL and m.calls[0][2].response_mime_type == "application/json"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
