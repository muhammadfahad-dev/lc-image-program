"""Offline tests: fake Gemini and fake Shopify. Run with: python -m pytest -q  (or python tests/test_pipeline.py)"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image

from lc_images import config, pipeline
from lc_images.gemini import QuotaExhausted

config.SECONDS_BETWEEN_CALLS = 0


def jpeg(colour=(200, 180, 160)):
    out = io.BytesIO()
    Image.new("RGB", (64, 80), colour).save(out, "JPEG")
    return out.getvalue()


pipeline.fetch = lambda url, width=None, http=None: jpeg()


class FakeGemini:
    def __init__(self, select=None, fail_slots=(), quota_after=None):
        self.select = select or {}
        self.fail_slots = set(fail_slots)
        self.quota_after = quota_after
        self.calls = {"image": 0, "check": 0}
        self.prompts = []
        self.current = None

    def generate(self, images, prompt, ratio):
        if self.quota_after is not None and self.calls["image"] >= self.quota_after:
            raise QuotaExhausted("PerDay limit")
        self.calls["image"] += 1
        self.prompts.append(prompt)
        return jpeg((10, 20, 30))

    def ask_json(self, images, prompt):
        self.calls["check"] += 1
        if prompt.startswith("You are helping"):
            return self.select
        failing = any(slot_title in prompt for slot_title in self.fail_slots)
        return {"pass": not failing, "problems": ["embroidery changed at the cuff"] if failing else []}


class FakeShopify:
    def __init__(self):
        self.uploads, self.attached, self.moved, self.metafields, self.links, self.audits = [], [], [], [], [], []

    def upload_image(self, data, filename, alt):
        self.uploads.append(filename)
        return f"gid://shopify/MediaImage/{len(self.uploads)}"

    def attach_to_product(self, pid, ids):
        self.attached.extend(ids)

    def move_media_to_front(self, pid, ids):
        self.moved.append(list(ids))

    def set_metafields(self, entries):
        self.metafields.extend(entries)

    def link_variants(self, pid, pairs):
        self.links.extend(pairs)

    def save_audit(self, pid, audit):
        self.audits.append(json.loads(json.dumps(audit)))

    def file_url(self, fid):
        return "https://cdn.shopify.com/x.jpg"


def product(ptype="Stonework Abaya", colours=("Navy", "Black-Pink", "Teal", "Maroon"), variant_media=("Teal",), filled=False, audit=None):
    variants = []
    for c in colours:
        for size in ("S", "M"):
            variants.append({"id": f"v-{c}-{size}", "selectedOptions": [{"name": "Size", "value": size}, {"name": "Color", "value": c}], "media": {"nodes": [{"id": "m"}] if c in variant_media and size == "S" else []}})
    return {
        "id": "gid://shopify/Product/1", "title": "Front Open Stonework Abaya", "handle": "front-open-stonework", "productType": ptype, "tags": ["Front Open Abaya"], "status": "ACTIVE",
        "options": [{"name": "Size", "values": ["S", "M"]}, {"name": "Color", "values": list(colours)}],
        "media": {"nodes": [{"id": f"gid://shopify/MediaImage/p{i}", "mediaContentType": "IMAGE", "image": {"url": f"https://cdn.shopify.com/p{i}.jpg"}} for i in range(3)]},
        "variants": {"nodes": variants},
        "audit": {"value": json.dumps(audit)} if audit else None,
        "fitting": {"value": "gid://x"} if filled else None, "multi": {"value": "[]"} if filled else None, "closer": {"value": "[]"} if filled else None,
    }


SELECT_OPEN = {"best_index": 1, "reason": "clear front view", "front_open": True, "has_design_work": True, "input_colour": "Navy", "colour_photos": {"Maroon": 2, "Navy": None}}


def test_front_open_full_run():
    g, s = FakeGemini(SELECT_OPEN), FakeShopify()
    run = pipeline.ProductRun(product(), g, s, log=lambda *a: None)
    assert run.run() == "done"
    a = run.audit
    assert [k for k, v in a["slots"].items() if v["status"] == "done"] == ["hero-1", "hero-2", "hero-3", "hero-4", "fitting-1", "multi-1", "multi-2", "multi-3b", "closer-1", "closer-2", "closer-3", "closer-4"]
    assert s.moved == [[a["slots"][k]["file"] for k in ("hero-1", "hero-2", "hero-3", "hero-4")]]
    keys = {m["key"]: m for m in s.metafields}
    assert keys["fitting_image"]["type"] == "file_reference"
    assert json.loads(keys["multi_display_images"]["value"]) == [a["slots"][k]["file"] for k in ("multi-1", "multi-2", "multi-3b")]
    assert len(json.loads(keys["closer_images"]["value"])) == 4
    c = a["colours"]
    assert c["Navy"]["status"] == "main_image" and c["Navy"]["file"] == a["slots"]["hero-1"]["file"]
    assert c["Black-Pink"]["status"] == "skipped_mixed"
    assert c["Teal"]["status"] == "had_image"
    assert c["Maroon"]["status"] == "existing_photo" and c["Maroon"]["file"] == "gid://shopify/MediaImage/p2"
    linked = {v for v, m in s.links}
    assert linked == {"v-Navy-S", "v-Navy-M", "v-Maroon-S", "v-Maroon-M"}
    assert g.calls["image"] == 12


def test_colour_generated_and_linked():
    sel = dict(SELECT_OPEN, colour_photos={}, input_colour=None)
    g, s = FakeGemini(sel), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy", "Emerald Green")), g, s, log=lambda *a: None)
    run.run()
    assert run.audit["colours"]["Emerald Green"]["status"] == "done"
    assert any("Change only the colour of the abaya fabric to Emerald Green" in p for p in g.prompts)
    assert ("v-Emerald Green-M", run.audit["colours"]["Emerald Green"]["file"]) in s.links
    assert run.audit["colours"]["Emerald Green"]["file"] in s.attached


def test_closed_abaya_plan_and_notes():
    sel = dict(SELECT_OPEN, front_open=False, has_design_work=False)
    g, s = FakeGemini(sel), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    run.run()
    done = [k for k, v in run.audit["slots"].items() if v["status"] == "done"]
    assert "hero-3" not in done and "multi-3a" in done and "multi-3b" not in done
    assert s.moved == [[run.audit["slots"][k]["file"] for k in ("hero-1", "hero-2", "hero-4")]]
    assert any("three-quarter view instead of worn open" in p for p in g.prompts)
    assert g.calls["image"] == 11


def test_failed_check_retries_then_manual():
    g, s = FakeGemini(SELECT_OPEN, fail_slots=["Seam and stitching"]), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    assert run.run() == "manual"
    slot = run.audit["slots"]["closer-2"]
    assert slot["status"] == "manual" and "embroidery changed at the cuff" in slot["problems"][0]
    assert g.calls["image"] == 12 - 1 + 3  # closer-2 tried three times
    assert "closer_images" not in {m["key"] for m in s.metafields}  # an incomplete field is not filled
    assert "closer-2" in run.audit["notes"][0]


def test_quota_stop_and_resume():
    g, s = FakeGemini(SELECT_OPEN, quota_after=5), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    try:
        run.run()
        raise AssertionError("expected the daily limit to stop the run")
    except QuotaExhausted:
        pass
    saved = s.audits[-1]
    assert saved["status"] == "in_progress" and sum(v["status"] == "done" for v in saved["slots"].values()) == 5
    g2, s2 = FakeGemini(SELECT_OPEN), FakeShopify()
    run2 = pipeline.ProductRun(product(colours=("Navy",), audit=saved), g2, s2, log=lambda *a: None)
    assert run2.run() == "done"
    assert g2.calls["image"] == 7  # only the remaining images
    assert not any(p.startswith("You are helping") for p in [])  # selection not repeated
    assert g2.calls["check"] == 7


def test_done_by_hand_only_colours():
    sel = dict(SELECT_OPEN, input_colour=None, colour_photos={})
    g, s = FakeGemini(sel), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",), filled=True), g, s, log=lambda *a: None)
    assert run.run() == "done"
    assert all(v["status"] == "existing" for v in run.audit["slots"].values())
    assert s.moved == [] and g.calls["image"] == 1 and run.audit["colours"]["Navy"]["status"] == "done"


def test_printed_and_not_abaya_and_no_photo():
    g, s = FakeGemini(SELECT_OPEN), FakeShopify()
    run = pipeline.ProductRun(product(ptype="Printed Abaya", colours=("Navy",)), g, s, log=lambda *a: None)
    run.run()
    assert run.audit["colours"]["Navy"]["status"] == "skipped_printed"
    run = pipeline.ProductRun(product(ptype="Hijab"), FakeGemini(SELECT_OPEN), FakeShopify(), log=lambda *a: None)
    assert run.run() == "skipped"
    run = pipeline.ProductRun(product(), FakeGemini(dict(SELECT_OPEN, best_index=None, reason="only a size chart")), FakeShopify(), log=lambda *a: None)
    assert run.run() == "needs_photo" and "size chart" in run.audit["notes"][0]


def test_classify():
    c = pipeline.classify_colour
    assert c("Color", "Navy", "Plain Abaya") == "single"
    assert c("Color", "Skyblue-Peach Ombré", "Plain Abaya") == "mixed"
    assert c("Color", "Black & Peach Flare", "Plain Abaya") == "mixed"
    assert c("Color", "Floral", "Plain Abaya") == "pattern"
    assert c("Stonework", "Pink Stonework Black Abaya", "Stonework Abaya") == "not_fabric_colour"
    assert c("Color", "Pink", "Printed Abaya") == "printed"


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as error:
                failures += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, error)
    sys.exit(1 if failures else 0)
