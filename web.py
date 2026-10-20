"""Gemini website tools. Run these on your own PC.

  python web.py login                       open Chrome and wait while you sign in to Gemini yourself
  python web.py inspect                     check that the program can find Gemini's buttons (prints no page text)
  python web.py test --handle HANDLE        make ONE image (Media 1.1) from a product's first photo; changes nothing in Shopify
  python web.py test --image photo.jpg      the same, from a photo on your PC (no Shopify or API key needed)
  python web.py upload-check                attach a test photo (sends nothing) and print the names of Gemini's upload
                                            menu and photo preview, so the program can be matched to them

For a full pilot through the website:  python run.py --web --dry-run --handles HANDLE
"""
import argparse
import json
import sys
import time
from pathlib import Path

from lc_images import config
from lc_images.gemini_web import ControlMissing, GeminiWeb, NotSignedIn
from lc_images.gemini import GenerationFailed, QuotaExhausted

OUT = Path("output")


def cmd_login(args):
    web = GeminiWeb(out_dir=OUT).start()
    try:
        if web.login(args.minutes):
            print("OK: signed in to Gemini. The sign-in is kept in the folder", web.profile)
            return 0
        print("Not signed in after %d minutes. Run 'python web.py login' again." % args.minutes)
        return 1
    finally:
        web.close()


def cmd_inspect(args):
    web = GeminiWeb(out_dir=OUT).start()
    try:
        web.open_app()
        print("Signed in:", web.signed_in())
        for key, rows in web.inspect().items():
            best = next((r for r in rows if r["visible"]), None) or next((r for r in rows if r["found"] > 0), None)
            mark = "OK  " if best else "--  "
            print(f"{mark}{key:20} " + (f"{best['selector']}  (found {best['found']}, visible {best['visible']})" if best else "not on this page"))
        print("\n'--' is normal on an empty chat for attachment_preview, image_tool, stop, response, response_text, result_image, download and signed_out.")
        if args.save:
            print("Debug bundle (screenshot, HTML, accessibility tree; keep it private):", web.snapshot("inspect"))
        return 0
    finally:
        web.close()


# Walks up from the chat box to the area that also holds the upload button, and lists that area's elements
# that are controls or pictures: tag, data-test-id, aria-label, first classes. No chat or account text.
PROBE_JS = r"""(box) => {
  // Up to the box that holds the upload button, then at most two more levels if the photo is not inside yet
  let area = box, extra = 0;
  for (let i = 0; i < 12 && area.parentElement; i++) {
    area = area.parentElement;
    const hasButton = area.querySelector('button[aria-label*="upload" i], button[aria-label*="Add files" i]');
    if (hasButton && (area.querySelector('img') || ++extra > 2)) break;
  }
  const lines = [];
  for (const el of area.querySelectorAll('*')) {
    const label = el.getAttribute('aria-label') || '';
    const isPic = el.tagName === 'IMG';
    const custom = el.tagName.includes('-');
    if (!label && !isPic && !custom) continue;
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 3).join('.') : '';
    const test = el.getAttribute('data-test-id');
    lines.push(el.tagName.toLowerCase() + (test ? `[data-test-id=${test}]` : '') + (cls ? '.' + cls : '')
               + (label ? `  "${label.slice(0, 60)}"` : '') + (isPic ? `  (picture ${el.naturalWidth}x${el.naturalHeight})` : ''));
  }
  return [...new Set(lines)].slice(0, 120).join('\n');
}"""


def cmd_upload_check(args):
    """Prints only control names from the chat box area: no chats, no account details."""
    import tempfile
    from PIL import Image

    web = GeminiWeb(out_dir=OUT).start()
    try:
        web.open_app()
        menu = web.find("upload_menu")
        print("Upload button:", (menu.get_attribute("aria-label") or menu.inner_text()) if menu else "NOT FOUND")
        if menu:
            menu.click()
            time.sleep(2)
            items = []
            for role in ("menuitem", "option", "button"):
                loc = web.page.get_by_role(role)
                for i in range(min(loc.count(), 60)):
                    item = loc.nth(i)
                    try:
                        if item.is_visible():
                            label = (item.get_attribute("aria-label") or item.inner_text() or "").strip().replace("\n", " ")
                            if label and len(label) < 60:
                                items.append(f"{role}: {label}")
                    except Exception:
                        pass
            print("Visible after clicking it:")
            for line in dict.fromkeys(items):
                print("   ", line)
            web.page.keyboard.press("Escape")
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "upload-check.jpg"
            Image.new("RGB", (400, 500), (198, 167, 123)).save(photo, "JPEG")
            web._attach([str(photo)])
            time.sleep(3)
            box = web.need("prompt_box")
            print("\nChat box area after attaching (tags and control names only):")
            print(box.evaluate(PROBE_JS))
        print("\nNothing was sent. The chat is left unsent; you can close the window.")
        return 0
    finally:
        web.close()


def reference_photo(args):
    from lc_images.pipeline import fetch, to_jpeg

    if args.image:
        return Path(args.image).stem, to_jpeg(Path(args.image).read_bytes(), max_side=2048)
    from lc_images.shopify import Shopify

    product = next(Shopify(dry_run=True).iter_products([args.handle]), None)
    if not product:
        raise SystemExit(f"No product with handle {args.handle}")
    photos = [m for m in product["media"]["nodes"] if m.get("mediaContentType") == "IMAGE" and m.get("image")]
    if not photos:
        raise SystemExit("This product has no photos")
    return product["handle"], to_jpeg(fetch(photos[0]["image"]["url"], 2048), max_side=2048)


def cmd_test(args):
    prompts = json.loads((Path("lc_images") / "prompts.json").read_text(encoding="utf-8"))
    if args.slot not in prompts:
        raise SystemExit(f"Unknown slot {args.slot}. Choose from: {', '.join(prompts)}")
    name, reference = reference_photo(args)
    folder = OUT / "web-test" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reference.jpg").write_bytes(reference)

    api = None
    if config.CHECK_SOURCE == "api":
        from lc_images.gemini import Gemini

        api = Gemini()
        api.choose_models(require_image=False)
    from PIL import Image
    import io
    from lc_images.pipeline import CHECK_PROMPT, to_jpeg

    web = GeminiWeb(api=api, out_dir=OUT).start()
    try:
        started = time.monotonic()
        data = web.generate([reference], prompts[args.slot]["prompt"], prompts[args.slot]["ratio"])
        seconds = round(time.monotonic() - started, 1)
        with Image.open(io.BytesIO(data)) as im:
            size, fmt = im.size, im.format
        out = folder / f"{args.slot}.{(fmt or 'png').lower().replace('jpeg', 'jpg')}"
        out.write_bytes(data)
        print(f"OK: image made in {seconds}s, {size[0]}x{size[1]} {fmt}, {len(data) // 1024} KB")
        print(f"Saved: {out}  (reference photo: {folder / 'reference.jpg'})")
        print(f"Website images made today: {web.usage.today()}")
        answer = web.ask_json(
            [reference, to_jpeg(data, max_side=1024)],
            CHECK_PROMPT.format(brief=f"{prompts[args.slot]['title']} (media image)", scope="", colour="the same as image 0", extra=""),
        )
        print(f"Check ({'API' if api else 'website'}):", "PASS" if answer.get("pass") else "FAIL", json.dumps(answer.get("problems") or []))
    except NotSignedIn as error:
        print("NOT SIGNED IN:", error)
        return 1
    except QuotaExhausted as error:
        print("LIMIT REACHED:", error)
        return 1
    except (GenerationFailed, ControlMissing, RuntimeError) as error:
        print("FAILED:", error)
        return 1
    finally:
        web.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login")
    p.add_argument("--minutes", type=int, default=10)
    p = sub.add_parser("inspect")
    p.add_argument("--save", action="store_true", help="also save a debug bundle")
    sub.add_parser("upload-check")
    p = sub.add_parser("test")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--handle")
    g.add_argument("--image")
    p.add_argument("--slot", default="hero-1")
    args = ap.parse_args()
    return {"login": cmd_login, "inspect": cmd_inspect, "test": cmd_test, "upload-check": cmd_upload_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
