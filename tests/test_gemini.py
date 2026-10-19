import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lc_images import config, gemini

config.SECONDS_BETWEEN_CALLS = 0
gemini.time.sleep = lambda s: None


class Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._d, self.text = code, data, text or str(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, replies):
        self.replies, self.bodies = list(replies), []

    def post(self, url, params=None, json=None, timeout=None):
        self.bodies.append(json)
        return self.replies.pop(0)

    def get(self, url, params=None, timeout=None):
        return Resp(200, {"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/gemini-3.1-flash-image"}]})


IMG = {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"PNGDATA").decode()}}]}}]}


def test_models_and_image():
    s = Session([Resp(200, IMG)])
    g = gemini.Gemini("k", s)
    assert g.choose_models() == ("gemini-3.1-flash-image", "gemini-2.5-flash")
    assert g.generate([b"x"], "p", "4:5") == b"PNGDATA"
    cfg = s.bodies[0]["generationConfig"]
    assert cfg["responseModalities"] == ["IMAGE"] and cfg["imageConfig"] == {"aspectRatio": "4:5", "imageSize": "2K"}


def test_daily_limit_stops():
    g = gemini.Gemini("k", Session([Resp(429, text='{"error":{"details":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}')]))
    g.image_model = "m"
    try:
        g.generate([], "p", "1:1")
        raise AssertionError
    except gemini.QuotaExhausted:
        pass


def test_minute_limit_waits_then_works():
    g = gemini.Gemini("k", Session([Resp(429, text="PerMinute"), Resp(200, IMG)]))
    g.image_model = "m"
    assert g.generate([], "p", "1:1") == b"PNGDATA"


def test_size_refused_falls_back():
    s = Session([Resp(400, text='{"error":"Invalid image_size"}'), Resp(200, IMG)])
    g = gemini.Gemini("k", s)
    g.image_model = "m"
    assert g.generate([], "p", "1:1") == b"PNGDATA" and "imageSize" not in s.bodies[1]["generationConfig"]["imageConfig"]


def test_blocked_image():
    g = gemini.Gemini("k", Session([Resp(200, {"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]})]))
    g.image_model = "m"
    try:
        g.generate([], "p", "1:1")
        raise AssertionError
    except gemini.GenerationFailed as e:
        assert "IMAGE_SAFETY" in str(e)


def test_json_answer():
    g = gemini.Gemini("k", Session([Resp(200, {"candidates": [{"content": {"parts": [{"text": '```json\n{"pass": false, "problems": ["x"]}\n```'}]}}]})]))
    g.check_model = "m"
    assert g.ask_json([b"a"], "q") == {"pass": False, "problems": ["x"]}


if __name__ == "__main__":
    bad = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f(); print("PASS", n)
            except Exception as e:
                bad += 1; import traceback; traceback.print_exc(); print("FAIL", n)
    sys.exit(bad)
