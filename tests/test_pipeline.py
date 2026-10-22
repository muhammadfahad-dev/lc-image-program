"""Offline tests: fake Gemini and fake Shopify. Run with: python -m pytest -q  (or python tests/test_pipeline.py)"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image

from lc_images import config, pipeline
from lc_images.gemini import QuotaExhausted, TemporaryFailure

config.SECONDS_BETWEEN_CALLS = 0
import tempfile as _tempfile
config.UNCHECKED_DIR = _tempfile.mkdtemp()
config.TEMPORARY_WAIT_SECONDS = 0


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
        self.image_counts = getattr(self, "image_counts", []) + [len(images)]
        self.check_prompts = getattr(self, "check_prompts", []) + [prompt]
        if prompt.startswith("You are helping"):
            return self.select
        failing = any(slot_title in prompt for slot_title in self.fail_slots)
        return {"pass": not failing, "problems": ["embroidery changed at the cuff"] if failing else []}


class FakeShopify:
    def __init__(self):
        self.uploads, self.attached, self.moved, self.metafields, self.links, self.audits = [], [], [], [], [], []

    counter = 0  # shared, so two runs never get the same file id (like real Shopify)

    def upload_image(self, data, filename, alt):
        self.uploads.append(filename)
        FakeShopify.counter += 1
        return f"gid://shopify/MediaImage/{FakeShopify.counter}"

    def attach_to_product(self, pid, ids):
        self.attached.extend(ids)

    def move_media_to_front(self, pid, ids):
        self.moved.append(list(ids))

    def set_metafields(self, entries):
        self.metafields.extend(entries)

    def placeholder(self, ratio, slot):
        return f"placeholder-{slot}"

    def detach_from_product(self, pid, ids):
        self.detached = getattr(self, "detached", []) + list(ids)

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


def test_main_image_colour_uses_the_new_main_image_not_its_old_photo():
    # the old photo of the main colour can differ from the new listing (black niqab, logo): its sizes show hero-1
    sel = dict(SELECT_OPEN, colour_photos={"Navy": 1, "Maroon": 2})
    g, s = FakeGemini(sel), FakeShopify()
    run = pipeline.ProductRun(product(), g, s, log=lambda *a: None)
    run.run()
    c, hero = run.audit["colours"], run.audit["slots"]["hero-1"]["file"]
    assert c["Navy"] == {"status": "main_image", "file": hero}
    assert c["Maroon"]["status"] == "existing_photo"
    # a product finished before this rule: its main colour is moved from the old photo to the new main image
    c["Navy"] = {"status": "existing_photo", "file": "gid://shopify/MediaImage/p1"}
    s.links.clear()
    run.colour_jobs()
    assert c["Navy"] == {"status": "main_image", "file": hero}
    assert {v for v, m in s.links} == {"v-Navy-S", "v-Navy-M"}
    # and a recheck of a finished product does the same
    c["Navy"] = {"status": "existing_photo", "file": "gid://shopify/MediaImage/p1"}
    run.audit["status"] = "done"
    assert "colour Navy" in run.recheck()
    assert run.run() == "done" and c["Navy"] == {"status": "main_image", "file": hero}


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
    checks = [p for p in g.check_prompts if "Left: worn open" in p]
    assert checks and all("does not apply to this abaya" in p for p in checks), "the check must know the abaya is closed"
    assert g.calls["image"] == 11


def test_failed_check_retries_then_manual():
    g, s = FakeGemini(SELECT_OPEN, fail_slots=["Seam and stitching"]), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    assert run.run() == "manual"
    slot = run.audit["slots"]["closer-2"]
    assert slot["status"] == "manual" and "embroidery changed at the cuff" in slot["problems"][0]
    assert g.calls["image"] == 12 - 1 + 3  # closer-2 tried three times
    closer = json.loads([m for m in s.metafields if m["key"] == "closer_images"][-1]["value"])
    assert closer[1] == "placeholder-closer-2" and len(closer) == 4, closer  # a black placeholder keeps the order
    assert run.audit["slots"]["closer-2"]["placeholder"] == "placeholder-closer-2"
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


def test_pilot_resumes_from_its_own_folder():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        sel = dict(SELECT_OPEN, colour_photos={}, input_colour=None)
        g, s = FakeGemini(sel, quota_after=4), FakeShopify()
        run = pipeline.ProductRun(product(colours=("Emerald Green",)), g, s, out_dir=tmp, log=lambda *a: None)
        try:
            run.run()
            raise AssertionError("expected the daily limit to stop the run")
        except QuotaExhausted:
            pass
        saved = json.loads((Path(tmp) / "front-open-stonework" / "report.json").read_text())
        assert saved["slots"]["hero-1"]["local"] == "hero-1-try1.jpg"
        g2 = FakeGemini(sel)
        run2 = pipeline.ProductRun(product(colours=("Emerald Green",)), g2, FakeShopify(), out_dir=tmp, log=lambda *a: None)
        assert run2.run() == "done"
        assert g2.calls["image"] == 12 - 4 + 1  # the rest of the listing images and the colour image
        assert g2.calls["check"] == 12 - 4 + 1  # the photo choice is not repeated


def test_temporary_gemini_error_waits_and_tries_again():
    class Flaky(FakeGemini):
        hiccups = 2

        def generate(self, images, prompt, ratio):
            if self.hiccups:
                self.hiccups -= 1
                raise TemporaryFailure("I encountered an error doing what you asked. Could you try again?")
            return super().generate(images, prompt, ratio)

    g = Flaky(SELECT_OPEN)
    run = pipeline.ProductRun(product(colours=("Navy",)), g, FakeShopify(), log=lambda *a: None)
    assert run.run() == "done"  # two hiccups on the first image do not use up its attempts
    assert g.calls["image"] == 12


def test_retry_manual_reopens_only_manual_images():
    audit = {"version": 1, "status": "manual", "slots": {"hero-1": {"status": "done", "file": "f1"}, "closer-2": {"status": "manual", "problems": ["x"]}},
             "colours": {"Navy": {"status": "manual"}, "Teal": {"status": "had_image"}}, "notes": []}
    run = pipeline.ProductRun(product(audit=audit), FakeGemini(SELECT_OPEN), FakeShopify(), log=lambda *a: None)
    run.reopen_manual()
    assert run.audit["status"] == "in_progress"
    assert "closer-2" not in run.audit["slots"] and run.audit["slots"]["hero-1"]["status"] == "done"
    assert "Navy" not in run.audit["colours"] and "Teal" in run.audit["colours"]


def test_repeat_check_sees_the_sections_other_images():
    g = FakeGemini(SELECT_OPEN)
    run = pipeline.ProductRun(product(colours=("Navy",)), g, FakeShopify(), log=lambda *a: None)
    run.run()
    # selection (3 photos), then per image: photo + new image + up to 3 earlier images of the same section
    checks = g.image_counts[1:]
    assert checks[:4] == [2, 3, 4, 5]  # hero-1..hero-4
    assert checks[4] == 2  # fitting has no other images
    assert checks[-4:] == [2, 3, 4, 5]  # closer-1..closer-4


def test_redo_section_and_media_image():
    g, s = FakeGemini(SELECT_OPEN), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    run.run()
    old_closer = [run.audit["slots"][k]["file"] for k in ("closer-1", "closer-2", "closer-3", "closer-4")]
    old_hero2 = run.audit["slots"]["hero-2"]["file"]
    saved = json.loads(json.dumps(run.audit))
    g2, s2 = FakeGemini(SELECT_OPEN), FakeShopify()
    # the product's fields are now filled (by this program), as they are in Shopify after the first run
    run2 = pipeline.ProductRun(product(colours=("Navy",), audit=saved, filled=True), g2, s2, log=lambda *a: None)
    assert sorted(run2.reopen(["closer", "hero-2"])) == ["closer-1", "closer-2", "closer-3", "closer-4", "hero-2"]
    assert run2.run() == "done"
    assert g2.calls["image"] == 5
    closer = [m for m in s2.metafields if m["key"] == "closer_images"][-1]
    assert not set(json.loads(closer["value"])) & set(old_closer)
    assert s2.detached == [old_hero2] and len(s2.moved[-1]) == 4


def test_retry_replaces_the_placeholder_in_place():
    g, s = FakeGemini(SELECT_OPEN, fail_slots=["Seam and stitching"]), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    assert run.run() == "manual"
    saved = json.loads(json.dumps(run.audit))
    s2 = FakeShopify()
    run2 = pipeline.ProductRun(product(colours=("Navy",), audit=saved, filled=True), FakeGemini(SELECT_OPEN), s2, log=lambda *a: None)
    run2.reopen_manual()
    assert run2.run() == "done"
    closer = json.loads([m for m in s2.metafields if m["key"] == "closer_images"][-1]["value"])
    assert "placeholder-closer-2" not in closer and closer[1] == run2.audit["slots"]["closer-2"]["file"]
    assert [m["key"] for m in s2.metafields if m["key"] in ("fitting_image", "multi_display_images")] == []  # unchanged sections not rewritten


def test_media_with_a_failed_image_places_the_rest_without_placeholders():
    g, s = FakeGemini(SELECT_OPEN, fail_slots=["Back, full length"]), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    assert run.run() == "manual"
    a = run.audit
    assert "placeholder" not in a["slots"]["hero-2"]
    assert s.moved[-1] == [a["slots"][k]["file"] for k in ("hero-1", "hero-3", "hero-4")]
    saved = json.loads(json.dumps(a))
    s2 = FakeShopify()
    run2 = pipeline.ProductRun(product(colours=("Navy",), audit=saved, filled=True), FakeGemini(SELECT_OPEN), s2, log=lambda *a: None)
    run2.reopen_manual()
    run2.run()
    b = run2.audit
    assert s2.moved[-1] == [b["slots"][k]["file"] for k in ("hero-1", "hero-2", "hero-3", "hero-4")]  # back in order
    assert s2.attached == [b["slots"]["hero-2"]["file"]]  # only the new image attached


def test_batch_result_counts_tries():
    g, s = FakeGemini(SELECT_OPEN, fail_slots=["Seam and stitching"]), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    assert run.prepare() is None
    job = [j for j in run.pending() if j.name == "closer-2"][0]
    assert run.batch_result(job, None, "busy", temporary=True) == "again" and run.audit["slots"]["closer-2"]["tries"] == 0
    for expected in ("retry", "retry", "manual"):
        job = [j for j in run.pending() if j.name == "closer-2"][0]
        assert run.batch_result(job, jpeg()) == expected
    assert run.audit["slots"]["closer-2"]["status"] == "manual"
    job = [j for j in run.pending() if j.name == "closer-1"][0]
    assert run.batch_result(job, jpeg()) == "passed" and run.audit["slots"]["closer-1"]["status"] == "done"


def test_section_filled_by_hand_later_is_never_overwritten():
    g, s = FakeGemini(SELECT_OPEN, quota_after=10), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    try:
        run.run()
    except QuotaExhausted:
        pass
    saved = json.loads(json.dumps(run.audit))
    assert saved["slots"]["closer-1"]["status"] == "done"  # made before the stop, not placed yet
    p2 = product(colours=("Navy",), audit=saved)
    p2["closer"] = {"value": json.dumps(["by-hand-1", "by-hand-2", "by-hand-3", "by-hand-4"])}  # owner filled it meanwhile
    s2 = FakeShopify()
    run2 = pipeline.ProductRun(p2, FakeGemini(SELECT_OPEN), s2, log=lambda *a: None)
    run2.run()
    assert "closer_images" not in {m["key"] for m in s2.metafields}
    assert all(run2.audit["slots"][k]["status"] == "existing" for k in ("closer-1", "closer-2", "closer-3", "closer-4"))


def test_second_redo_before_placing_still_detaches_old_media():
    g, s = FakeGemini(SELECT_OPEN), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    run.run()
    a = json.loads(json.dumps(run.audit))
    old1, old2 = a["slots"]["hero-1"]["file"], a["slots"]["hero-2"]["file"]
    p2 = product(colours=("Navy",), audit=a, filled=True)
    p2["media"]["nodes"] += [{"id": f, "mediaContentType": "IMAGE"} for f in (old1, old2)]  # they are on the product
    run2 = pipeline.ProductRun(p2, FakeGemini(SELECT_OPEN), FakeShopify(), log=lambda *a: None)
    run2.reopen(["hero-1"])
    run2.reopen(["hero-2"])  # a second redo before the first was placed
    assert set(run2.audit["detach"]) == {old1, old2}


def test_redo_hero1_relinks_the_input_colour():
    g, s = FakeGemini(SELECT_OPEN), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    run.run()
    assert run.audit["colours"]["Navy"]["status"] == "main_image"
    a = json.loads(json.dumps(run.audit))
    s2 = FakeShopify()
    run2 = pipeline.ProductRun(product(colours=("Navy",), audit=a, filled=True), FakeGemini(SELECT_OPEN), s2, log=lambda *a: None)
    run2.reopen(["hero-1"])
    run2.run()
    new_hero = run2.audit["slots"]["hero-1"]["file"]
    assert run2.audit["colours"]["Navy"] == {"status": "main_image", "file": new_hero}
    assert ("v-Navy-S", new_hero) in s2.links


def test_redo_part_of_a_hand_filled_section_remakes_and_writes_it_all():
    p1 = product(colours=("Navy",))
    p1["closer"] = {"value": json.dumps(["h1", "h2", "h3", "h4"])}  # closer filled by hand
    g, s = FakeGemini(SELECT_OPEN), FakeShopify()
    run = pipeline.ProductRun(p1, g, s, log=lambda *a: None)
    run.run()
    assert "closer_images" not in {m["key"] for m in s.metafields}
    a = json.loads(json.dumps(run.audit))
    p2 = product(colours=("Navy",), audit=a)
    p2["closer"] = p1["closer"]
    s2 = FakeShopify()
    run2 = pipeline.ProductRun(p2, FakeGemini(SELECT_OPEN), s2, log=lambda *a: None)
    assert sorted(run2.reopen(["closer-1"])) == ["closer-1", "closer-2", "closer-3", "closer-4"]
    run2.run()
    closer = json.loads([m for m in s2.metafields if m["key"] == "closer_images"][-1]["value"])
    assert closer == [run2.audit["slots"][k]["file"] for k in ("closer-1", "closer-2", "closer-3", "closer-4")]


def test_partial_media_then_all_sections_by_hand_still_places_media():
    g, s = FakeGemini(SELECT_OPEN, quota_after=2), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    try:
        run.run()
    except QuotaExhausted:
        pass
    a = json.loads(json.dumps(run.audit))
    s2 = FakeShopify()
    run2 = pipeline.ProductRun(product(colours=("Navy",), audit=a, filled=True), FakeGemini(SELECT_OPEN), s2, log=lambda *a: None)
    run2.run()
    assert s2.moved and s2.moved[-1] == [a["slots"]["hero-1"]["file"], a["slots"]["hero-2"]["file"]]


def test_check_answer_false_as_text_is_a_fail():
    run = pipeline.ProductRun(product(), FakeGemini(SELECT_OPEN), FakeShopify(), log=lambda *a: None)
    run.g.ask_json = lambda images, prompt: {"pass": "false", "problems": "wrong colour"}
    assert run.check(jpeg(), jpeg(), "b", "c") == (False, ["wrong colour"])
    run.g.ask_json = lambda images, prompt: [{"pass": True}]
    assert run.check(jpeg(), jpeg(), "b", "c")[0] is False  # not a clear answer


def test_image_made_before_a_stop_is_checked_not_made_again():
    g, s = FakeGemini(SELECT_OPEN), FakeShopify()
    real_ask = g.ask_json
    state = {"stop": True}

    def ask(images, prompt):
        if state["stop"] and "Sleeve (closer image)" in prompt:
            raise QuotaExhausted("checker busy")
        return real_ask(images, prompt)

    g.ask_json = ask
    run = pipeline.ProductRun(product(colours=("Navy",)), g, s, log=lambda *a: None)
    try:
        run.run()
    except QuotaExhausted:
        pass
    made = g.calls["image"]
    state["stop"] = False
    saved = json.loads(json.dumps(run.audit))
    g2 = FakeGemini(SELECT_OPEN)
    run2 = pipeline.ProductRun(product(colours=("Navy",), audit=saved), g2, FakeShopify(), log=lambda *a: None)
    assert run2.run() == "done"
    assert g2.calls["image"] == 1  # only closer-4: closer-3 was checked from the kept image
    assert not list(Path(config.UNCHECKED_DIR).glob("*.img"))


def test_niqab_must_match_and_colour_images_recolour_it():
    assert "niqab or headscarf that is not in the main colour of the abaya" in pipeline.CHECK_PROMPT
    assert "niqab" not in pipeline.CHECK_PROMPT.split("Do NOT fail for small things")[1]
    assert "change it to {colour} too" in pipeline.COLOUR_PROMPT


def test_recheck_remakes_only_failing_images_and_replaces_colours():
    sel = dict(SELECT_OPEN, colour_photos={}, input_colour=None)
    g, s = FakeGemini(sel), FakeShopify()
    run = pipeline.ProductRun(product(colours=("Emerald Green",)), g, s, log=lambda *a: None)
    assert run.run() == "done"
    a = json.loads(json.dumps(run.audit))
    old_multi1, old_colour = a["slots"]["multi-1"]["file"], a["colours"]["Emerald Green"]["file"]

    class Strict(FakeGemini):  # the new rules fail multi-1 and the colour image once
        failed = set()

        def ask_json(self, images, prompt):
            for key in ("Middle: worn closed", "recoloured to Emerald Green"):
                if key in prompt and key not in self.failed:
                    self.failed.add(key)
                    self.calls["check"] += 1
                    return {"pass": False, "problems": ["black niqab with a coloured abaya"]}
            return super().ask_json(images, prompt)

    g2, s2 = Strict(sel), FakeShopify()
    run2 = pipeline.ProductRun(product(colours=("Emerald Green",), audit=a, filled=True), g2, s2, log=lambda *a: None)
    run2.prepare(force=True)
    assert sorted(run2.recheck()) == ["colour Emerald Green", "multi-1"]
    assert g2.calls["image"] == 0  # only checks so far
    assert run2.run() == "done"
    assert g2.calls["image"] == 2
    multi = json.loads([m for m in s2.metafields if m["key"] == "multi_display_images"][-1]["value"])
    assert multi[0] == run2.audit["slots"]["multi-1"]["file"] != old_multi1
    assert old_colour in getattr(s2, "detached", []) and run2.audit["colours"]["Emerald Green"]["file"] != old_colour


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
