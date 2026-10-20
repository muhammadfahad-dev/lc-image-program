"""Gemini website tools. Run these on your own PC.

  python web.py login                       open Chrome and wait while you sign in to Gemini yourself
  python web.py inspect                     check that the program can find Gemini's buttons (prints no page text)
  python web.py test --handle HANDLE        make ONE image (Media 1.1) from a product's first photo; changes nothing in Shopify
  python web.py test --image photo.jpg      the same, from a photo on your PC (no Shopify or API key needed)

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
    if config.GEMINI_KEY:
        from lc_images.gemini import Gemini

        api = Gemini()
        api.choose_models(require_image=False)
    web = GeminiWeb(api=api, out_dir=OUT).start()
    try:
        started = time.monotonic()
        data = web.generate([reference], prompts[args.slot]["prompt"], prompts[args.slot]["ratio"])
        seconds = round(time.monotonic() - started, 1)
    except NotSignedIn as error:
        print("NOT SIGNED IN:", error)
        return 1
    except QuotaExhausted as error:
        print("LIMIT REACHED:", error)
        return 1
    except (GenerationFailed, ControlMissing) as error:
        print("FAILED:", error)
        return 1
    finally:
        web.close()

    from PIL import Image
    import io

    with Image.open(io.BytesIO(data)) as im:
        size, fmt = im.size, im.format
    out = folder / f"{args.slot}.{(fmt or 'png').lower().replace('jpeg', 'jpg')}"
    out.write_bytes(data)
    print(f"OK: image made in {seconds}s, {size[0]}x{size[1]} {fmt}, {len(data) // 1024} KB")
    print(f"Saved: {out}  (reference photo: {folder / 'reference.jpg'})")
    print(f"Website images made today: {web.usage.today()}")
    if api:
        from lc_images.pipeline import to_jpeg

        answer = api.ask_json([reference, to_jpeg(data, max_side=1024)], "Image 0 is a product photo of an abaya. Image 1 was generated from it. "
                              'Answer JSON {"same_garment": true/false, "problems": [short strings]}: is Image 1 the same garment (colour, cut, details)?')
        print("Check model says:", json.dumps(answer))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login")
    p.add_argument("--minutes", type=int, default=10)
    p = sub.add_parser("inspect")
    p.add_argument("--save", action="store_true", help="also save a debug bundle")
    p = sub.add_parser("test")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--handle")
    g.add_argument("--image")
    p.add_argument("--slot", default="hero-1")
    args = ap.parse_args()
    return {"login": cmd_login, "inspect": cmd_inspect, "test": cmd_test}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
