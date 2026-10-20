"""Run the LC image program.

  python run.py --test-key                     check that the Gemini key can make images
  python run.py --dry-run --handles a,b,c      pilot: make images into ./output, change nothing in Shopify
  python run.py --limit 25                     live run: up to 25 products, then stop
  python run.py --web ...                      make the images on the Gemini website (your PC, signed in with python web.py login)
"""
import argparse
import html
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from lc_images import config
from lc_images.gemini import Gemini, GenerationFailed, QuotaExhausted
from lc_images.pipeline import ProductRun, now
from lc_images.shopify import Shopify

try:
    from lc_images.gemini_web import ControlMissing, NotSignedIn

    WEB_STOPS = (ControlMissing, NotSignedIn)
except ImportError:
    WEB_STOPS = ()

OUT = Path("output")


def test_key():
    g = Gemini()
    image_model, check_model = g.choose_models()
    print(f"Check model: {check_model}")
    image_models = sorted(m for m in g.available if "image" in m)
    print("Image models this key can see:", ", ".join(image_models) or "none")
    ok = False
    for model in [m for m in config.IMAGE_MODELS if m in g.available] + [m for m in image_models if m not in config.IMAGE_MODELS]:
        g.image_model = model
        try:
            data = g.generate([], "A plain ivory square with soft light. No text.", "1:1")
            print(f"OK {model}: made a test image ({len(data)} bytes).")
            ok = True
            break
        except QuotaExhausted as error:
            print(f"REFUSED {model} (limit): {error}")
        except (GenerationFailed, RuntimeError) as error:
            print(f"REFUSED {model}: {str(error)[:1500]}")
    try:
        answer = g.ask_json([], 'Reply with {"ok": true}')
        print(f"Check model {check_model}: {'OK' if answer.get('ok') else answer}")
    except Exception as error:
        print(f"Check model {check_model} REFUSED: {str(error)[:800]}")
    try:
        shop = Shopify()
        d = shop.gql("{ shop { name myshopifyDomain } products(first: 1) { nodes { title } } }")
        print(f"Shopify OK: logged in to {d['shop']['name']} ({d['shop']['myshopifyDomain']}), can read products.")
    except Exception as error:
        print(f"Shopify REFUSED: {str(error)[:800]}")
    return 0 if ok else 1


def contact_sheet(results):
    """A simple page showing every image made in a dry run, for review."""
    rows = []
    for handle, status in results:
        folder = OUT / handle
        report = json.loads((folder / "report.json").read_text()) if (folder / "report.json").exists() else {}
        tiles = "".join(
            f'<figure><img src="{handle}/{f.name}" loading="lazy"><figcaption>{html.escape(f.stem)}</figcaption></figure>'
            for f in sorted(folder.glob("*.jpg"))
        )
        issues = [f"{k}: {'; '.join(v.get('problems', []))}" for k, v in {**report.get("slots", {}), **report.get("colours", {})}.items() if v.get("status") == "manual"]
        rows.append(
            f"<section><h2>{html.escape(handle)} <small>{html.escape(status)}</small></h2>"
            f"<p>{html.escape((report.get('choice') or {}).get('reason', ''))}</p>"
            + (f"<ul>{''.join(f'<li>{html.escape(i)}</li>' for i in issues)}</ul>" if issues else "")
            + f"<div class=g>{tiles}</div></section>"
        )
    page = (
        "<!doctype html><meta charset=utf-8><title>Pilot images</title><style>body{font-family:Inter,system-ui,sans-serif;background:#F7F3ED;color:#25231F;margin:0;padding:24px}"
        "h2{font-weight:500}small{color:#5F6456;font-weight:400}.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}"
        "figure{margin:0}img{width:100%;height:auto;background:#E7DED2}figcaption{font-size:12px;color:#5F6456;margin-top:4px}li{color:#7a3b2e}</style>"
        "<h1>Pilot images</h1><p>Files ending in try2 or try3 are re-dos after a failed check. reference.jpg is the product photo Gemini worked from.</p>"
        + "".join(rows)
    )
    (OUT / "index.html").write_text(page, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="make images into ./output only; change nothing in Shopify")
    ap.add_argument("--limit", type=int, default=config.MAX_PRODUCTS_PER_RUN)
    ap.add_argument("--handles", default="", help="comma-separated product handles to process")
    ap.add_argument("--test-key", action="store_true")
    ap.add_argument("--web", action="store_true", help="make images on the Gemini website instead of the API (checks still use the free API model)")
    args = ap.parse_args()

    if args.test_key:
        return test_key()

    OUT.mkdir(exist_ok=True)
    api = Gemini()
    if args.web:
        from lc_images.gemini_web import GeminiWeb

        print("Check model:", api.choose_models(require_image=False)[1], "| images: Gemini website")
        g = GeminiWeb(api=api, out_dir=OUT).start()
        if not g.signed_in() and (g.open_app() or not g.signed_in()):
            g.close()
            print("Gemini is not signed in. Run: python web.py login")
            return 1
    else:
        g = api
        print("Models:", *g.choose_models())
    s = Shopify(dry_run=args.dry_run)
    handles = [h.strip() for h in args.handles.split(",") if h.strip()]

    started = datetime.now(timezone.utc)
    results, counts, quota_hit, images_made = [], {}, False, 0
    for product in s.iter_products(handles or None):
        if len(results) >= args.limit:
            break
        if product["productType"] not in config.PRODUCT_TYPES:
            continue
        audit = json.loads((product.get("audit") or {}).get("value") or "{}")
        if audit.get("status") in ("done", "manual", "needs_photo", "skipped") and not handles:
            continue
        print(f"\n{product['title']}")
        run = ProductRun(product, g, s, out_dir=OUT if args.dry_run else None)
        try:
            status = run.run()
        except QuotaExhausted as error:
            print(f"  Daily limit reached: {str(error)[:200]}")
            status, quota_hit = "paused (daily limit)", True
        except WEB_STOPS as error:  # website problem: stop, every product would fail the same way
            print(f"  Stopped: {error}")
            status, quota_hit = "paused (website problem)", True
        except Exception as error:  # one bad product must not stop the run
            traceback.print_exc()
            status = "error"
            run.audit.setdefault("notes", []).append(f"Error {now()}: {str(error)[:300]}")
            try:
                run.save()
            except Exception:
                pass
        images_made += run.made
        results.append((product["handle"], status))
        counts[status] = counts.get(status, 0) + 1
        print(f"  -> {status} ({run.made} images passed)")
        if quota_hit:
            break

    if args.web:
        g.close()
    summary = {
        "date": started.strftime("%Y-%m-%d %H:%M UTC"),
        "mode": "pilot (dry run)" if args.dry_run else "live",
        "products_worked_on": len(results),
        "statuses": counts,
        "images_passed": images_made,
        "gemini_image_calls": g.calls["image"],
        "gemini_check_calls": g.calls["check"],
        "image_source": "website" if args.web else "api",
        "seconds_per_website_image": round(sum(g.timings) / len(g.timings), 1) if args.web and g.timings else None,
        "daily_limit_reached": quota_hit,
        "products": [{"handle": h, "status": st} for h, st in results],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    if args.dry_run:
        contact_sheet(results)
    else:
        s.set_metafields([{"ownerId": s.shop_id(), "namespace": "custom", "key": config.SUMMARY_KEY, "type": "json", "value": json.dumps({k: v for k, v in summary.items() if k != "products"})}])
    print("\nSummary:", json.dumps({k: v for k, v in summary.items() if k != "products"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
