"""What happens to one product: choose the photo, make the listing images, check them, upload them,
then make and link the single-colour variant images. Progress is saved on the product after every image,
so a run that stops (daily limit, error) carries on from the same place next time."""
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

from . import config
from .gemini import GenerationFailed, QuotaExhausted, TemporaryFailure

HERE = Path(__file__).parent
PROMPTS = json.loads((HERE / "prompts.json").read_text(encoding="utf-8"))

MIXED = re.compile(r"(-|&|/|\band\b|\bwith\b|ombr|tie-?dye|multi)", re.I)
PATTERNS = {"floral", "checkered", "geometric"}

CLOSED_NOTES = {
    "multi-1": "Note: this abaya does not open at the front, so show the simple front view.",
    "multi-2": "Note: this abaya does not open at the front, so show a three-quarter view instead of worn open.",
}

FITTING_FRAMING = (
    "For this fitting image also check the framing, roughly (within about a tenth of the frame): full-length front view, "
    "garment centred left to right, neckline in the upper part of the frame (about 20% from the top), hem near the bottom "
    "(about 88% from the top), and a sleeve visible on the left side. Fail only if the framing is clearly different, "
    "for example the head or hem is cut off, or it is not a front view."
)

COLOUR_PROMPT = (
    "Use the attached image as the only reference. Change only the colour of the abaya fabric to {colour}. "
    "If the model wears a niqab or headscarf in the abaya's colour, change it to {colour} too, so it still matches the abaya. "
    "Keep everything else exactly the same: the cut, fabric texture, drape, sleeves, neckline, length, pose, "
    "model or mannequin, background, lighting and framing. Keep every embroidery, stonework, cutdana, beadwork, "
    "lace, button and trim detail in exactly the same place, shape and colour as in the reference; only the base "
    "fabric changes colour. The fabric must clearly read as {colour}, true to life, as in a real studio photograph. "
    "Do not add any text, logo or watermark to the image."
)

SELECT_PROMPT = """You are helping an abaya store prepare product images.
The images above are the store's photos of ONE product: "{title}" (type: {type}; tags: {tags}).
Colour options for this product: {colours}.

Answer in JSON with exactly these keys:
- "best_index": the index of the single best photo to use as the reference for new product images: a clear, sharp, well-lit photo showing the whole abaya from the front, head to hem, with the design visible. Use null if no photo is good enough (blurry, cropped, only a detail, only a size chart, several products, heavy text).
- "reason": one short sentence explaining the choice or why none is usable.
- "front_open": true if this abaya opens at the front (open front, kimono, shrug, cardigan style), false if it is a closed abaya. Use the title, tags and photos.
- "has_design_work": true if the abaya has visible embroidery, stonework, cutdana, beadwork, lace, print or other surface decoration; false if it is plain.
- "input_colour": the colour option (copied exactly from the list) that the photo at best_index shows, or null if it matches none or you are unsure.
- "colour_photos": an object with every colour option as a key; the value is the index of a photo that shows this same abaya alone, whole or nearly whole, in that colour, so it can be the picture for that colour on its own. Use null if no photo does, and always null for a photo that shows several abayas or colours together (a rack, a group, a collage), a close-up or a size chart. Only give an index when you are confident.
"""

CHECK_PROMPT = """You are checking an AI-generated image for an online abaya store.
Image 0 is the real product photo. Image 1 is the generated image. The brief for image 1: {brief}
{scope}
Fail image 1 ONLY for a major problem that a customer would notice and that misrepresents the product:
- it shows a different garment: a different type or length (for example a short cape, top or mini dress instead of a full-length abaya), a clearly different cut or silhouette, or a main part missing or added (overlay, cape, sleeves, inner layer); this includes a person or mannequin wearing some other dress instead of the abaya from image 0;
- the fabric or detail shown does not come from the abaya in image 0 (for example a close-up of a fabric, colour or stitching that image 0 does not have);
- the design work is missing, clearly in a different place, or added where there was none;
- the fabric colour is clearly not {colour};
- a model wears a niqab or headscarf that is not in the main colour of the abaya (for example a black niqab with a beige abaya); it must match the abaya;
- any text, logo, label or watermark appears;
- the person or mannequin is clearly deformed (extra or missing fingers, hands or limbs, a twisted body);
- the image clearly ignores the brief (wrong view or wrong kind of shot).
Do NOT fail for small things: embroidery, beadwork, stonework or trim that is slightly simplified or less detailed; small differences in drape, folds or volume; hands that are slightly imperfect; small differences in framing, pose, lighting or background; shoes or jewellery.
{extra}{others}
Answer in JSON: {{"pass": true or false, "problems": ["short plain reason", ...]}}. List only major problems; an image without a major problem passes."""

OTHERS_NOTE = (
    "\nImages 2 to {last} are the other images already made for this same section of the product page. "
    "Also fail image 1 if it shows the same view or the same detail as one of them (each image in a section must show something different)."
)

CLOSE_UP_SCOPE = (
    "Image 1 is a close-up, so judge the design details only on the part that is visible. "
    "But it must still look like a real photo of this abaya: if the brief puts the garment on a person or mannequin, "
    "it must be worn or dressed on them as a whole garment. Fail it if, for example, a mannequin is bare with only a "
    "sleeve or piece of fabric attached, or the garment is cut, floating or incomplete where it should be whole."
)

CLOSE_UP_SLOTS = {"multi-3b", "closer-1", "closer-2", "closer-3", "closer-4"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_jpeg(data, max_side=None, quality=92):
    image = Image.open(io.BytesIO(data)).convert("RGB")
    if max_side and max(image.size) > max_side:
        image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def fetch(url, width=None, http=requests):
    if width:
        url = f"{url}{'&' if '?' in url else '?'}width={width}"
    for attempt in range(4):
        try:
            r = http.get(url, timeout=120)
            break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 3:
                raise
            time.sleep(5 + attempt * 10)  # connection dropped: wait and try again
    r.raise_for_status()
    return to_jpeg(r.content, max_side=width or 2048)


def classify_colour(option_name, value, product_type):
    if option_name.strip().lower() not in config.COLOUR_OPTION_NAMES:
        return "not_fabric_colour"
    if value.strip().lower() in PATTERNS:
        return "pattern"
    if MIXED.search(value):
        return "mixed"
    if product_type in config.PRINTED_TYPES:
        return "printed"
    return "single"


def listing_plan(front_open, has_design_work):
    """The slots to make, in display order, for each image field."""
    media = ["hero-1", "hero-2", "hero-3", "hero-4"] if front_open else ["hero-1", "hero-2", "hero-4"]
    multi = ["multi-1", "multi-2", "multi-3b" if has_design_work else "multi-3a"]
    return {"media": media, "fitting": ["fitting-1"], "multi": multi, "closer": ["closer-1", "closer-2", "closer-3", "closer-4"]}


RESOLVED = ("done", "manual", "existing")  # a listing image needs no more work
COLOUR_FINAL = ("done", "existing_photo", "had_image", "main_image", "manual")
PLACEHOLDER_ALT = "PLACEHOLDER"


class Job:
    """One image still to be made: a listing image (kind "slot") or a colour image (kind "colour")."""

    def __init__(self, kind, name, prompt, ratio, brief, colour, base, field=None, value=None, tries=0):
        self.kind, self.name, self.prompt, self.ratio = kind, name, prompt, ratio
        self.brief, self.colour, self.base = brief, colour, base
        self.field, self.value, self.tries = field, value, tries

    def __repr__(self):
        return f"Job({self.kind} {self.name}, try {self.tries + 1})"


class ProductRun:
    """Everything about one product. One-by-one runs call run(); Batch runs call prepare(), pending(),
    batch_result() and finish() (see batch.py). Both follow exactly the same rules."""

    def __init__(self, product, gemini, shopify, out_dir=None, log=print):
        self.p = product
        self.g = gemini
        self.s = shopify
        self.out = Path(out_dir) / product["handle"] if out_dir else None
        self.log = log
        self.made = 0
        self.hero_jpeg = None
        self.reference = None
        self.plan = None
        self.colour_opts = []
        raw = (product.get("audit") or {}).get("value")
        local = self.out / "report.json" if self.out else None
        if local and local.exists():
            raw = local.read_text(encoding="utf-8")  # a pilot carries on from its own saved progress
        self.audit = json.loads(raw) if raw else {"version": 1, "status": "new", "slots": {}, "colours": {}, "notes": []}
        self.last_local = None
        self.small = {}  # slot -> small copy of each image made, for the repeat check
        self.audit.setdefault("slots", {})
        self.audit.setdefault("colours", {})
        self.audit.setdefault("notes", [])
        self.audit.setdefault("ours", [])  # every file this program uploaded for the product
        self.attached = {m["id"] for m in product["media"]["nodes"]}
        placed_media = self.audit.get("placed", {}).get("media")
        if isinstance(placed_media, list):
            self.attached.update(placed_media)  # what this program already put on the product
        if self.out:
            self.out.mkdir(parents=True, exist_ok=True)

    # ---------- helpers ----------
    def save(self, status=None):
        if status:
            self.audit["status"] = status
        self.audit["updated"] = now()
        self.s.save_audit(self.p["id"], self.audit)
        if self.out:
            (self.out / "report.json").write_text(json.dumps(self.audit, indent=2), encoding="utf-8")

    def keep_local(self, name, jpeg):
        if self.out:
            (self.out / name).write_bytes(jpeg)

    def images(self):
        """The product's own photos (not images or placeholders this program added)."""
        ours = set(self.audit["ours"])
        for group in ("slots", "colours"):
            for v in self.audit[group].values():
                ours.update(x for x in (v.get("file"), v.get("placeholder")) if x)
        return [
            m for m in self.p["media"]["nodes"]
            if m.get("mediaContentType") == "IMAGE" and m.get("image") and m["id"] not in ours and PLACEHOLDER_ALT not in (m.get("alt") or "")
        ]

    def check(self, original, generated, brief, colour, slot=None, others=()):
        scope = CLOSE_UP_SCOPE if slot in CLOSE_UP_SLOTS else ""
        extra = FITTING_FRAMING if slot == "fitting-1" else ""
        others = list(others)[:3]
        note = OTHERS_NOTE.format(last=1 + len(others)) if others else ""
        answer = self.g.ask_json([original, generated, *others], CHECK_PROMPT.format(brief=brief, scope=scope, colour=colour, extra=extra, others=note))
        if not isinstance(answer, dict):
            answer = {}
        passed = answer.get("pass") is True or str(answer.get("pass")).strip().lower() == "true"
        problems = answer.get("problems") or []
        if not isinstance(problems, list):
            problems = [problems]
        return passed, [str(x) for x in problems] or ([] if passed else ["the check gave no clear answer"])

    def small_image(self, slot):
        """A small copy of an image already made for this product (for the repeat check), or None."""
        if slot in self.small:
            return self.small[slot]
        state = self.audit["slots"].get(slot, {})
        try:
            if self.out and state.get("local") and (self.out / state["local"]).exists():
                self.small[slot] = to_jpeg((self.out / state["local"]).read_bytes(), max_side=768)
            elif state.get("file") and not str(state["file"]).startswith("dry-run"):
                url = self.s.file_url(state["file"])
                self.small[slot] = fetch(url, 768) if url else None
        except Exception:
            self.small[slot] = None
        return self.small.get(slot)

    def others_for(self, job):
        """The images already made for the job's own section, for the repeat check."""
        if job.kind != "slot":
            return []
        slots = self.plan[job.field]
        done = [o for o in slots if o != job.name and self.audit["slots"].get(o, {}).get("status") == "done"]
        return [img for img in (self.small_image(o) for o in done) if img]

    def upload(self, jpeg, filename, alt):
        file_id = self.s.upload_image(jpeg, filename, alt)
        self.audit["ours"].append(file_id)
        return file_id

    # ---------- reopening ----------
    def reopen_manual(self):
        """Give the images marked "do manually" another go (python run.py --retry-manual).
        Their placeholders stay on the product page until the new image passes."""
        audit = self.audit
        for group in ("slots", "colours"):
            for key in [k for k, v in audit[group].items() if v.get("status") == "manual"]:
                del audit[group][key]
        if audit.get("status") == "manual":
            audit["status"] = "in_progress"

    def reopen(self, names):
        """Make these images again (python run.py --redo closer-1,closer-3 or --redo closer).
        Names are image slots (hero-1 ... closer-4) or sections (media, fitting, multi, closer).
        The old images stay on the page until the new ones are in place; replaced Media images then leave
        the product page but stay in Shopify Files (nothing is deleted)."""
        audit = self.audit
        if "choice" not in audit:
            return []
        plan = listing_plan(audit["choice"]["front_open"], audit["choice"]["has_design_work"])
        wanted = set()
        for name in names:
            wanted.update(plan.get(name, [name]))
        reopened = []
        for field, slots in plan.items():
            hit = [sl for sl in slots if sl in wanted and sl in audit["slots"]]
            if not hit:
                continue
            if field != "media" and any(audit["slots"].get(sl, {}).get("status") == "existing" for sl in slots):
                # the section was filled by hand: it can only be replaced as a whole, so all of it is made again
                hit = [sl for sl in slots if sl in audit["slots"]]
            for sl in hit:
                old = audit["slots"].pop(sl)
                if field == "media" and old.get("file") and old["file"] in self.attached:
                    audit.setdefault("detach", []).append(old["file"])
                reopened.append(sl)
                if sl == "hero-1":
                    # the colour that used the old front image is linked again to the new one
                    for value in [v for v, st in audit["colours"].items() if st.get("status") == "main_image"]:
                        del audit["colours"][value]
            audit.setdefault("placed", {})[field] = False
        if reopened:
            audit["status"] = "in_progress"
        return reopened

    # ---------- steps ----------
    def prepare(self, force=False):
        """Checks, photo choice and the reference photo. Returns a final status (nothing to do), or None to go on.
        force: go on even for a finished product (used by recheck)."""
        p, audit = self.p, self.audit
        if audit.get("status") in ("needs_photo", "skipped") or (not force and audit.get("status") in ("done", "manual")):
            return audit["status"]
        if hasattr(self.g, "start_product"):
            self.g.start_product(p["handle"])  # the website gives each product its own chat
        if p["productType"] not in config.PRODUCT_TYPES:
            audit["notes"] = [f"Not an abaya type ({p['productType'] or 'none'})"]
            self.save("skipped")
            return "skipped"

        self.colour_opts = [o for o in p["options"] if o["name"].strip().lower() in config.COLOUR_OPTION_NAMES]
        colour_names = [v for o in self.colour_opts for v in o["values"]]
        photos = self.images()[: config.MAX_SELECTION_PHOTOS]
        if not photos and "choice" not in audit:
            audit["notes"] = ["No product photos to work from"]
            self.save("needs_photo")
            return "needs_photo"

        # Choose the reference photo (once; kept in the record)
        if "choice" not in audit:
            small = [fetch(m["image"]["url"], 768) for m in photos]
            answer = self.g.ask_json(small, SELECT_PROMPT.format(title=p["title"], type=p["productType"], tags=", ".join(p["tags"]), colours=", ".join(colour_names) or "none"))
            if not isinstance(answer, dict):
                answer = {}
            index = answer.get("best_index")
            if not isinstance(index, int) or not 0 <= index < len(photos):
                audit["notes"] = [f"No usable photo: {answer.get('reason', 'none suitable')}"]
                self.save("needs_photo")
                return "needs_photo"
            colour_photos = {}
            for name, i in (answer.get("colour_photos") or {}).items():
                if isinstance(i, int) and 0 <= i < len(photos) and name in colour_names:
                    colour_photos[name] = photos[i]["id"]
            audit["choice"] = {
                "media_id": photos[index]["id"],
                "url": photos[index]["image"]["url"],
                "reason": answer.get("reason", ""),
                "front_open": bool(answer.get("front_open")),
                "has_design_work": bool(answer.get("has_design_work")),
                "input_colour": answer.get("input_colour") if answer.get("input_colour") in colour_names else None,
                "colour_photos": colour_photos,
            }
            self.save("in_progress")
        choice = audit["choice"]
        self.reference = fetch(choice["url"], 2048)
        self.keep_local("reference.jpg", self.reference)

        # Sections already filled by hand (never by this program) are left alone; if all three are, only colours run
        self.plan = listing_plan(choice["front_open"], choice["has_design_work"])
        ours = audit.get("placed", {})
        filled = {f: bool((p.get(f) or {}).get("value")) and f not in ours for f in ("fitting", "multi", "closer")}
        by_hand = all(filled.values())
        for field, slots in self.plan.items():
            if field != "media" and filled[field]:
                # filled by hand: leave the whole section as it is, even images this program made before
                for slot in slots:
                    if audit["slots"].get(slot, {}).get("status") != "existing":
                        audit["slots"][slot] = {"status": "existing"}
            elif by_hand:
                for slot in slots:
                    if audit["slots"].get(slot, {}).get("status") not in RESOLVED:
                        audit["slots"][slot] = {"status": "existing"}
        return None

    def slot_job(self, field, slot):
        choice = self.audit["choice"]
        prompt = PROMPTS[slot]["prompt"]
        brief = f"{PROMPTS[slot]['title']} ({field} image)"
        if not choice["front_open"] and slot in CLOSED_NOTES:
            prompt = prompt + "\n\n" + CLOSED_NOTES[slot]
            # the checker must judge against what Gemini was really asked for
            brief += f". {CLOSED_NOTES[slot]} The brief's 'worn open' does not apply to this abaya."
        tries = self.audit["slots"].get(slot, {}).get("tries", 0)
        return Job("slot", slot, prompt, PROMPTS[slot]["ratio"], brief, "the same as image 0", self.reference, field=field, tries=tries)

    def media_ready(self):
        return all(self.audit["slots"].get(s, {}).get("status") in RESOLVED for s in self.plan["media"])

    def pending(self):
        """The images still to be made now. Colour images come once the Media section is finished,
        because they are made from the new front image (1.1)."""
        jobs = []
        for field, slots in self.plan.items():
            for slot in slots:
                status = self.audit["slots"].get(slot, {}).get("status")
                if status not in RESOLVED and status != "queued":
                    jobs.append(self.slot_job(field, slot))
        if self.media_ready():
            self.place_fields()
            jobs.extend(self.colour_jobs())
        return jobs

    # ---------- one-by-one ----------
    def judge(self, job, raw):
        """Check one generated image. Returns (passed, problems, full-size jpeg)."""
        jpeg = to_jpeg(raw)
        self.last_local = f"{job.name}-try{job.tries + 1}.jpg"
        self.keep_local(self.last_local, jpeg)
        ok, problems = self.check(job.base, to_jpeg(raw, max_side=1024), job.brief, job.colour, job.name if job.kind == "slot" else None, self.others_for(job))
        if ok:
            self.made += 1
            self.small[job.name] = to_jpeg(raw, max_side=768)
        return ok, problems, jpeg

    def unchecked_path(self, job):
        """Where an image waits for its check. If a run stops between making and checking (the checker busy, Ctrl+C),
        the next run checks this image instead of paying for a new one."""
        folder = Path(config.UNCHECKED_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{slug(self.p['handle'])}--{job.name}.img"

    def make(self, job):
        """Generate and check, up to 1 + config.RETRIES tries in a row. Returns (jpeg or None, problems)."""
        problems = []
        waiting = self.unchecked_path(job)
        for attempt in range(job.tries, 1 + config.RETRIES):
            job.tries = attempt
            raw = None
            if waiting.exists():
                raw = waiting.read_bytes()
                self.log(f"    {job.name}: checking the image made last time (not paid for again)")
            for pause in range(0 if raw is not None else 1 + config.TEMPORARY_RETRIES):
                try:
                    raw = self.g.generate([job.base], job.prompt, job.ratio)
                    break
                except TemporaryFailure as error:
                    problems = [f"Gemini gave no image ({error})"]
                    if pause < config.TEMPORARY_RETRIES:
                        self.log(f"    {job.name}: {str(error)[:120]}; waiting {config.TEMPORARY_WAIT_SECONDS:.0f}s and trying again")
                        time.sleep(config.TEMPORARY_WAIT_SECONDS)
                except GenerationFailed as error:
                    problems = [f"Gemini gave no image ({error})"]
                    break
            if raw is None:
                self.log(f"    {job.name}: {problems[0][:160]}, attempt {attempt + 1}")
                continue
            waiting.write_bytes(raw)
            ok, problems, jpeg = self.judge(job, raw)  # if the checker stops the run here, the image is kept
            waiting.unlink(missing_ok=True)
            if ok:
                return jpeg, []
            self.log(f"    {job.name}: check failed ({'; '.join(problems)[:160]}), attempt {attempt + 1}")
        return None, problems

    def run(self):
        """One-by-one: make everything that is still to do, then finish."""
        status = self.prepare()
        if status:
            return status
        while True:
            jobs = self.pending()
            if not jobs:
                break
            for job in jobs:
                self.log(f"  {job.name}: generating")
                started = time.monotonic()
                jpeg, problems = self.make(job)
                self.record(job, jpeg, problems)
                self.place_fields()
                took = int(time.monotonic() - started)
                self.log(f"  {job.name}: {'passed' if jpeg is not None else 'do manually'}, {took // 60}m {took % 60:02d}s in all")
        return self.finish()

    # ---------- results ----------
    def record(self, job, jpeg, problems):
        """Save the outcome of one image: uploaded (passed) or "do manually" (failed every try)."""
        p, audit = self.p, self.audit
        if job.kind == "slot":
            if jpeg is not None:
                if job.name == "hero-1":
                    self.hero_jpeg = jpeg
                file_id = self.upload(jpeg, f"{p['handle']}-{job.name}.jpg", f"{p['title']} - {PROMPTS[job.name]['title']}")
                audit["slots"][job.name] = {"status": "done", "file": file_id}
                if self.out:
                    audit["slots"][job.name]["local"] = self.last_local
            else:
                state = {"status": "manual", "problems": problems[:4]}
                if job.field != "media":  # keeps the section's order on the page; replaced once the image is made
                    state["placeholder"] = self.s.placeholder(job.ratio, job.name)
                audit["slots"][job.name] = state
        else:
            value = job.value
            if jpeg is None:
                audit["colours"][value] = {"status": "manual", "problems": problems[:4]}
            else:
                media_id = self.upload(jpeg, f"{p['handle']}-colour-{slug(value)}.jpg", f"{p['title']} - {value}")
                self.s.attach_to_product(p["id"], [media_id])
                self.attached.add(media_id)
                self.s.link_variants(p["id"], [(v["id"], media_id) for v in self.variants_of(value)])
                old = audit.get("colour_replace", {}).pop(value, None)
                if old and old != media_id:
                    self.s.detach_from_product(p["id"], [old])  # the replaced colour image (it stays in Files)
                    self.attached.discard(old)
                audit["colours"][value] = {"status": "done", "file": media_id}
                if self.out:
                    audit["colours"][value]["local"] = self.last_local
        self.save()

    def place_fields(self):
        """Put each finished section in place. A section is finished when every image in it passed or is marked
        "do manually". Media: the passed images go first, in order (no placeholders on the main gallery).
        Fitting, Multi display and A little closer: every position is filled, a black placeholder where an
        image still has to be made, so the order never shifts. Run again after a retry: only changes are written."""
        audit, pid = self.audit, self.p["id"]
        placed = audit.setdefault("placed", {})
        changed = False
        for field, slots in self.plan.items():
            states = [audit["slots"].get(s, {}) for s in slots]
            if any(st.get("status") not in RESOLVED for st in states):
                continue  # not finished yet
            if all(st.get("status") == "existing" for st in states):
                continue  # filled by hand
            if field != "media" and any(st.get("status") == "existing" for st in states):
                continue  # a section filled by hand is never written by the program
            if placed.get(field) is True:  # filled completely by an earlier version of the program
                continue
            if field == "media":
                files = [st["file"] for st in states if st.get("status") == "done"]
                if placed.get(field) == files and not audit.get("detach"):
                    continue
                if audit.get("detach"):
                    self.s.detach_from_product(pid, audit["detach"])  # images being replaced (--redo)
                    self.attached.difference_update(audit["detach"])
                    audit["detach"] = []
                new = [f for f in files if f not in self.attached]
                self.s.attach_to_product(pid, new)
                self.attached.update(new)
                self.s.move_media_to_front(pid, files)
            else:
                files = [st["file"] if st.get("status") == "done" else st.get("placeholder") for st in states]
                files = [f for f in files if f]
                if placed.get(field) == files:
                    continue
                key, kind = config.FIELD_KEYS[field], "file_reference" if field == "fitting" else "list.file_reference"
                value = files[0] if kind == "file_reference" else json.dumps(files)
                self.s.set_metafields([{"ownerId": pid, "namespace": "custom", "key": key, "type": kind, "value": value}])
            placed[field] = files
            changed = True
        if changed:
            self.save()

    def finish(self):
        """Final status: done, manual (something to do by hand) or in_progress (Batch work still out)."""
        self.place_fields()
        audit = self.audit
        states = list(audit["slots"].values()) + list(audit["colours"].values())
        waiting = any(st.get("status") in ("queued", "pending") for st in states) or (self.plan and not self.media_ready())
        if not waiting and self.plan:
            waiting = bool(self.pending())  # e.g. colour images, which can only start once Media is finished
        manual = [k for k, v in audit["slots"].items() if v.get("status") == "manual"] + [k for k, v in audit["colours"].items() if v.get("status") == "manual"]
        audit["notes"] = [f"Do manually: {', '.join(manual)}"] if manual else []
        self.save("in_progress" if waiting else ("manual" if manual else "done"))
        return audit["status"]

    # ---------- colours ----------
    def variants_of(self, value):
        return [v for v in self.p["variants"]["nodes"] if any(o["name"].strip().lower() in config.COLOUR_OPTION_NAMES and o["value"] == value for o in v["selectedOptions"])]

    def colour_base(self):
        """Colours are made from the new front image (1.1) so every colour has the same pose; the reference photo otherwise."""
        hero = self.audit["slots"].get("hero-1", {})
        if self.hero_jpeg:
            return self.hero_jpeg
        if self.out and hero.get("local") and (self.out / hero["local"]).exists():
            self.hero_jpeg = (self.out / hero["local"]).read_bytes()
            return self.hero_jpeg
        if hero.get("status") == "done" and not str(hero.get("file", "")).startswith("dry-run"):
            url = self.s.file_url(hero["file"])
            if url:
                self.hero_jpeg = fetch(url, 2048)
                return self.hero_jpeg
        return self.reference

    def colour_jobs(self):
        """Settle every colour that needs no new image (skipped, already has one, own photo, same as the front
        image) and return jobs for the ones that need a recoloured image."""
        audit, p = self.audit, self.p
        hero = audit["slots"].get("hero-1", {})
        jobs, changed = [], False
        for option in self.colour_opts:
            for value in option["values"]:
                state = audit["colours"].get(value, {})
                status = state.get("status", "")
                hero_ready = hero.get("status") == "done" and audit.get("placed", {}).get("media")
                if status == "existing_photo" and value == audit["choice"]["input_colour"] and hero_ready:
                    status = ""  # the main image's colour now shows the new main image (see below), not the old photo
                if status in COLOUR_FINAL or status.startswith("skipped") or status == "queued":
                    continue
                kind = classify_colour(option["name"], value, p["productType"])
                variants = self.variants_of(value)
                if kind != "single":
                    audit["colours"][value], changed = {"status": f"skipped_{kind}"}, True
                    continue
                if any(v["media"]["nodes"] for v in variants) and state.get("status") != "existing_photo":
                    audit["colours"][value], changed = {"status": "had_image"}, True
                    continue
                if value == audit["choice"]["input_colour"] and hero_ready:
                    # the colour of the main image shows the new main image, which matches the rest of the listing
                    # (an old photo of it can differ, e.g. a black niqab or a logo); the old photo stays in Media
                    self.s.link_variants(p["id"], [(v["id"], hero["file"]) for v in variants])
                    audit["colours"][value], changed = {"status": "main_image", "file": hero["file"]}, True
                    continue
                if value in audit["choice"]["colour_photos"]:
                    media_id = audit["choice"]["colour_photos"][value]
                    self.s.link_variants(p["id"], [(v["id"], media_id) for v in variants])
                    audit["colours"][value], changed = {"status": "existing_photo", "file": media_id}, True
                    continue
                jobs.append(Job(
                    "colour", f"colour-{slug(value)}", COLOUR_PROMPT.format(colour=value), "4:5",
                    f"the same abaya recoloured to {value} (with a matching niqab if worn), everything else unchanged", value, None, value=value, tries=state.get("tries", 0),
                ))
        if jobs:
            base = self.colour_base()
            for job in jobs:
                job.base = base
        if changed:
            self.save()
        return jobs

    # ---------- recheck ----------
    def file_image(self, file_id):
        """An uploaded image, downloaded again for a check (None if it cannot be read)."""
        if not file_id or str(file_id).startswith("dry-run"):
            return None
        try:
            url = self.s.file_url(file_id)
            return fetch(url, 1024) if url else None
        except Exception:
            return None

    def recheck(self):
        """Check the images already made against the current rules. The ones that fail are made again (the next run
        replaces them in place); colour images are replaced and their variants relinked. Only the checks are paid for.
        Returns the names of the images to make again. Call after prepare(force=True)."""
        audit, failed = self.audit, []
        for field, slots in self.plan.items():
            for slot in slots:
                state = audit["slots"].get(slot, {})
                if state.get("status") != "done":
                    continue
                image = self.file_image(state["file"])
                if image is None:
                    continue
                job = self.slot_job(field, slot)
                ok, problems = self.check(self.reference, image, job.brief, job.colour, slot, self.others_for(job))
                if not ok:
                    self.log(f"  {slot}: fails the current check ({'; '.join(problems)[:140]}); will be made again")
                    failed.append(slot)
        for value, state in list(audit["colours"].items()):
            if state.get("status") != "done":
                continue
            image = self.file_image(state.get("file"))
            if image is None:
                continue
            ok, problems = self.check(self.colour_base(), image, f"the same abaya recoloured to {value} (with a matching niqab if worn), everything else unchanged", value)
            if not ok:
                self.log(f"  colour {value}: fails the current check ({'; '.join(problems)[:140]}); will be made again")
                audit.setdefault("colour_replace", {})[value] = state["file"]
                audit["colours"][value] = {"status": "pending", "tries": 0, "problems": problems[:4]}
                failed.append(f"colour {value}")
        hero = audit["slots"].get("hero-1", {})
        main = audit.get("choice", {}).get("input_colour")
        if audit["colours"].get(main, {}).get("status") == "existing_photo" and hero.get("status") == "done":
            # the main image's colour still shows its old photo: the run links it to the new main image
            self.log(f"  colour {main}: will show the new main image instead of its old photo")
            failed.append(f"colour {main}")
        slots_failed = [f for f in failed if not f.startswith("colour ")]
        if slots_failed:
            self.reopen(slots_failed)
        if failed:
            audit["status"] = "in_progress"
            self.save()
        return failed

    # ---------- Batch ----------
    def job_for(self, ref, batch_name=None):
        """Rebuild the job for an image that is out in a Batch job (ref from the job's map), or None if it is
        no longer queued in that job (already handled, redone, or changed by hand since)."""
        if self.plan is None:
            return None
        group, key = ("slots", ref["name"]) if ref["kind"] == "slot" else ("colours", ref["value"])
        state = self.audit[group].get(key, {})
        if state.get("status") != "queued" or (batch_name and state.get("batch") != batch_name):
            return None
        if ref["kind"] == "slot":
            field = next((f for f, slots in self.plan.items() if ref["name"] in slots), None)
            if field is None:
                return None
            job = self.slot_job(field, ref["name"])
        else:
            value = ref["value"]
            base = self.reference if ref.get("base") == "ref" else self.colour_base()
            job = Job("colour", ref["name"], COLOUR_PROMPT.format(colour=value), "4:5",
                      f"the same abaya recoloured to {value} (with a matching niqab if worn), everything else unchanged", value, base, value=value)
        job.tries = state.get("tries", 0)
        return job

    def release_orphans(self, known_batches):
        """Images marked as out in a Batch job that no longer exists go back into the queue. This counts as a try,
        so an image that can never be finished ends as "do manually" instead of being paid for again and again."""
        changed = False
        for group in ("slots", "colours"):
            for key, st in list(self.audit[group].items()):
                if st.get("status") != "queued" or st.get("batch") in known_batches:
                    continue
                tries = st.get("tries", 0) + 1
                problem = ["its Batch job could not be finished"]
                if tries < 1 + config.RETRIES:
                    self.audit[group][key] = {"status": "pending", "tries": tries, "problems": problem}
                else:
                    state = {"status": "manual", "problems": problem}
                    if group == "slots" and key not in self.plan["media"]:
                        try:
                            state["placeholder"] = self.s.placeholder(PROMPTS[key]["ratio"], key)
                        except Exception:
                            pass  # placed without it; the report still lists the image as to do
                    self.audit[group][key] = state
                changed = True
        if changed:
            self.save()
        return changed

    def mark_queued(self, job, batch_name):
        group = "slots" if job.kind == "slot" else "colours"
        self.audit[group][job.value if job.kind == "colour" else job.name] = {"status": "queued", "batch": batch_name, "tries": job.tries}

    def batch_result(self, job, raw, problem=None, temporary=False):
        """One answer from a Batch job: check it, then upload it, or count a failed try, or mark it "do manually"
        after the last try. A passing Google error (temporary) does not count as a try."""
        group = "slots" if job.kind == "slot" else "colours"
        key = job.value if job.kind == "colour" else job.name
        if raw is not None:
            ok, problems, jpeg = self.judge(job, raw)
            if ok:
                self.record(job, jpeg, [])
                return "passed"
        else:
            problems = [problem or "Gemini gave no image"]
        if temporary:
            self.audit[group][key] = {"status": "pending", "tries": job.tries, "problems": problems[:4]}
            self.save()
            return "again"
        if job.tries + 1 >= 1 + config.RETRIES:
            self.record(job, None, problems)
            return "manual"
        self.audit[group][key] = {"status": "pending", "tries": job.tries + 1, "problems": problems[:4]}
        self.save()
        return "retry"
