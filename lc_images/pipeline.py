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
- "colour_photos": an object with every colour option as a key; the value is the index of a photo that clearly shows this same abaya, whole or nearly whole, in that colour, or null if no photo clearly does. Only give an index when you are confident.
"""

CHECK_PROMPT = """You are checking an AI-generated image for an online abaya store.
Image 0 is the real product photo. Image 1 is the generated image. The brief for image 1: {brief}
{scope}
Fail image 1 ONLY for a major problem that a customer would notice and that misrepresents the product:
- it shows a different garment: a different type or length (for example a short cape, top or mini dress instead of a full-length abaya), a clearly different cut or silhouette, or a main part missing or added (overlay, cape, sleeves, inner layer); this includes a person or mannequin wearing some other dress instead of the abaya from image 0;
- the fabric or detail shown does not come from the abaya in image 0 (for example a close-up of a fabric, colour or stitching that image 0 does not have);
- the design work is missing, clearly in a different place, or added where there was none;
- the fabric colour is clearly not {colour};
- any text, logo, label or watermark appears;
- the person or mannequin is clearly deformed (extra or missing fingers, hands or limbs, a twisted body);
- the image clearly ignores the brief (wrong view or wrong kind of shot).
Do NOT fail for small things: embroidery, beadwork, stonework or trim that is slightly simplified or less detailed; small differences in drape, folds or volume; hands that are slightly imperfect; small differences in framing, pose, lighting or background; accessories such as hijab, niqab, shoes or jewellery.
{extra}{others}
Answer in JSON: {{"pass": true or false, "problems": ["short plain reason", ...]}}. List only major problems; an image without a major problem passes."""

OTHERS_NOTE = (
    "\nImages 2 to {last} are the other images already made for this same section of the product page. "
    "Also fail image 1 if it shows the same view or the same detail as one of them (each image in a section must show something different)."
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


class ProductRun:
    def __init__(self, product, gemini, shopify, out_dir=None, log=print):
        self.p = product
        self.g = gemini
        self.s = shopify
        self.out = Path(out_dir) / product["handle"] if out_dir else None
        self.log = log
        self.made = 0
        self.hero_jpeg = None
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
        """The product's own photos (not images this program added)."""
        ours = {v.get("file") for v in self.audit["slots"].values()} | {v.get("file") for v in self.audit["colours"].values()}
        return [m for m in self.p["media"]["nodes"] if m.get("mediaContentType") == "IMAGE" and m.get("image") and m["id"] not in ours]

    def check(self, original, generated, brief, colour, slot=None, others=()):
        scope = "Image 1 is a close-up, so judge only the part that is visible." if slot in CLOSE_UP_SLOTS else ""
        extra = FITTING_FRAMING if slot == "fitting-1" else ""
        others = list(others)[:3]
        note = OTHERS_NOTE.format(last=1 + len(others)) if others else ""
        answer = self.g.ask_json([original, generated, *others], CHECK_PROMPT.format(brief=brief, scope=scope, colour=colour, extra=extra, others=note))
        return bool(answer.get("pass")), [str(x) for x in answer.get("problems") or []]

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

    def make(self, reference, prompt, ratio, brief, colour, slot, name, others=()):
        """Generate, check, and retry up to config.RETRIES times. Returns (jpeg or None, problems)."""
        problems = []
        for attempt in range(1 + config.RETRIES):
            raw = None
            for pause in range(1 + config.TEMPORARY_RETRIES):
                try:
                    raw = self.g.generate([reference], prompt, ratio)
                    break
                except TemporaryFailure as error:
                    problems = [f"Gemini gave no image ({error})"]
                    if pause < config.TEMPORARY_RETRIES:
                        self.log(f"    {name}: {str(error)[:120]}; waiting {config.TEMPORARY_WAIT_SECONDS:.0f}s and trying again")
                        time.sleep(config.TEMPORARY_WAIT_SECONDS)
                except GenerationFailed as error:
                    problems = [f"Gemini gave no image ({error})"]
                    break
            if raw is None:
                self.log(f"    {name}: {problems[0][:160]}, attempt {attempt + 1}")
                continue
            jpeg = to_jpeg(raw)
            self.last_local = f"{name}-try{attempt + 1}.jpg"
            self.keep_local(self.last_local, jpeg)
            ok, problems = self.check(reference, to_jpeg(raw, max_side=1024), brief, colour, slot, others)
            if ok:
                self.made += 1
                self.small[name] = to_jpeg(raw, max_side=768)
                return jpeg, []
            self.log(f"    {name}: check failed ({'; '.join(problems)[:160]}), attempt {attempt + 1}")
        return None, problems

    def reopen_manual(self):
        """Give the images marked "do manually" another go (python run.py --retry-manual)."""
        audit = self.audit
        for group in ("slots", "colours"):
            for key in [k for k, v in audit[group].items() if v.get("status") == "manual"]:
                del audit[group][key]
        if audit.get("status") == "manual":
            audit["status"] = "in_progress"

    def reopen(self, names):
        """Make these images again (python run.py --redo closer-1,closer-3 or --redo closer).
        Names are image slots (hero-1 ... closer-4) or sections (media, fitting, multi, closer).
        The old images leave the product page; they stay in Shopify Files (nothing is deleted)."""
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
            for sl in hit:
                old = audit["slots"].pop(sl)
                if field == "media" and old.get("file") and audit.get("placed", {}).get("media"):
                    audit.setdefault("detach", []).append(old["file"])
                reopened.append(sl)
            audit.setdefault("placed", {})[field] = False
        if reopened:
            audit["status"] = "in_progress"
        return reopened

    # ---------- main ----------
    def run(self):
        p, audit = self.p, self.audit
        if audit.get("status") in ("done", "manual", "needs_photo", "skipped"):
            return audit["status"]
        if hasattr(self.g, "start_product"):
            self.g.start_product(p["handle"])  # the website gives each product its own chat
        if p["productType"] not in config.PRODUCT_TYPES:
            audit["notes"] = [f"Not an abaya type ({p['productType'] or 'none'})"]
            self.save("skipped")
            return "skipped"

        colour_opts = [o for o in p["options"] if o["name"].strip().lower() in config.COLOUR_OPTION_NAMES]
        colour_names = [v for o in colour_opts for v in o["values"]]
        photos = self.images()[: config.MAX_SELECTION_PHOTOS]
        if not photos:
            audit["notes"] = ["No product photos to work from"]
            self.save("needs_photo")
            return "needs_photo"

        # 1. Choose the reference photo (once; kept in the audit)
        if "choice" not in audit:
            small = [fetch(m["image"]["url"], 768) for m in photos]
            answer = self.g.ask_json(small, SELECT_PROMPT.format(title=p["title"], type=p["productType"], tags=", ".join(p["tags"]), colours=", ".join(colour_names) or "none"))
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
        reference = fetch(choice["url"], 2048)
        self.keep_local("reference.jpg", reference)

        # 2. Listing images, field by field
        plan = listing_plan(choice["front_open"], choice["has_design_work"])
        filled = {field: bool((p.get(field) or {}).get("value")) for field in ("fitting", "multi", "closer")}
        already_done_by_hand = all(filled.values())
        for field, slots in plan.items():
            skip_field = already_done_by_hand or (field != "media" and filled[field])
            for slot in slots:
                state = audit["slots"].get(slot, {})
                if state.get("status") in ("done", "manual", "existing"):
                    continue
                if skip_field:
                    audit["slots"][slot] = {"status": "existing"}
                    continue
                prompt = PROMPTS[slot]["prompt"]
                if not choice["front_open"] and slot in CLOSED_NOTES:
                    prompt = prompt + "\n\n" + CLOSED_NOTES[slot]
                brief = f"{PROMPTS[slot]['title']} ({field} image)"
                self.log(f"  {slot}: generating")
                others = [img for img in (self.small_image(o) for o in slots if o != slot and audit["slots"].get(o, {}).get("status") == "done") if img]
                jpeg, problems = self.make(reference, prompt, PROMPTS[slot]["ratio"], brief, "the same as image 0", slot, slot, others)
                if jpeg is None:
                    audit["slots"][slot] = {"status": "manual", "problems": problems[:4]}
                else:
                    if slot == "hero-1":
                        self.hero_jpeg = jpeg
                    file_id = self.s.upload_image(jpeg, f"{p['handle']}-{slot}.jpg", f"{p['title']} - {PROMPTS[slot]['title']}")
                    audit["slots"][slot] = {"status": "done", "file": file_id}
                    if self.out:
                        audit["slots"][slot]["local"] = self.last_local
                self.save()
            self.place_field(field, slots)

        # 3. Single-colour variant images
        self.colours(colour_opts, reference)

        manual = [k for k, v in audit["slots"].items() if v.get("status") == "manual"] + [k for k, v in audit["colours"].items() if v.get("status") == "manual"]
        audit["notes"] = [f"Do manually: {', '.join(manual)}"] if manual else []
        self.save("manual" if manual else "done")
        return audit["status"]

    def place_field(self, field, slots):
        """Put a finished field's images in place. A field is only filled when all its images passed."""
        audit = self.audit
        states = [audit["slots"].get(s, {}) for s in slots]
        if any(st.get("status") != "done" for st in states) or audit.get("placed", {}).get(field):
            return
        files = [st["file"] for st in states]
        pid = self.p["id"]
        if field == "media":
            if audit.get("detach"):
                self.s.detach_from_product(pid, audit["detach"])  # images being replaced (--redo)
                audit["detach"] = []
            self.s.attach_to_product(pid, [f for f in files if f not in self.attached_media()])
            self.s.move_media_to_front(pid, files)
        elif field == "fitting":
            self.s.set_metafields([{"ownerId": pid, "namespace": "custom", "key": config.FIELD_KEYS["fitting"], "type": "file_reference", "value": files[0]}])
        else:
            self.s.set_metafields([{"ownerId": pid, "namespace": "custom", "key": config.FIELD_KEYS[field], "type": "list.file_reference", "value": json.dumps(files)}])
        audit.setdefault("placed", {})[field] = True
        self.save()

    def attached_media(self):
        return {m["id"] for m in self.p["media"]["nodes"]}

    def front_image(self, reference):
        """Colours are made from the new front image (1.1) so every colour has the same pose; the reference photo otherwise."""
        hero = self.audit["slots"].get("hero-1", {})
        if self.hero_jpeg:
            return self.hero_jpeg
        if self.out and hero.get("local") and (self.out / hero["local"]).exists():
            return (self.out / hero["local"]).read_bytes()
        if hero.get("status") == "done" and not str(hero.get("file", "")).startswith("dry-run"):
            url = self.s.file_url(hero["file"])
            if url:
                return fetch(url, 2048)
        return reference

    def colours(self, colour_opts, reference):
        audit, p = self.audit, self.p
        hero = audit["slots"].get("hero-1", {})
        base = None  # the image the colours are made from: the new front image if there is one, else the reference photo
        for option in colour_opts:
            for value in option["values"]:
                state = audit["colours"].get(value, {})
                if state.get("status") in ("done", "existing_photo", "had_image", "main_image", "manual") or state.get("status", "").startswith("skipped"):
                    continue
                kind = classify_colour(option["name"], value, p["productType"])
                variants = [v for v in p["variants"]["nodes"] if any(o["name"] == option["name"] and o["value"] == value for o in v["selectedOptions"])]
                if kind != "single":
                    audit["colours"][value] = {"status": f"skipped_{kind}"}
                    continue
                if any(v["media"]["nodes"] for v in variants):
                    audit["colours"][value] = {"status": "had_image"}
                    continue
                media_id, status = None, None
                if value in audit["choice"]["colour_photos"]:
                    media_id, status = audit["choice"]["colour_photos"][value], "existing_photo"
                elif value == audit["choice"]["input_colour"] and hero.get("status") == "done" and audit.get("placed", {}).get("media"):
                    media_id, status = hero["file"], "main_image"
                else:
                    if base is None:
                        base = self.front_image(reference)
                    self.log(f"  colour {value}: generating")
                    jpeg, problems = self.make(base, COLOUR_PROMPT.format(colour=value), "4:5", f"the same abaya recoloured to {value}, everything else unchanged", value, None, f"colour-{slug(value)}")
                    if jpeg is None:
                        audit["colours"][value] = {"status": "manual", "problems": problems[:4]}
                        self.save()
                        continue
                    media_id = self.s.upload_image(jpeg, f"{p['handle']}-colour-{slug(value)}.jpg", f"{p['title']} - {value}")
                    self.s.attach_to_product(p["id"], [media_id])
                    status = "done"
                self.s.link_variants(p["id"], [(v["id"], media_id) for v in variants])
                audit["colours"][value] = {"status": status, "file": media_id}
                self.save()
