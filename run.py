"""Run the LC image program.

  python run.py --test-key                     check that the Gemini key can make images
  python run.py --sheet                        rebuild output/index.html (USED / REJECTED marks) from earlier pilots
  python run.py --dry-run --handles a,b,c      pilot: make images into ./output, change nothing in Shopify
  python run.py --limit 25                     live run: up to 25 products, then stop
  python run.py --retry-manual ...             also try again the images marked "do manually"
  python run.py --redo closer-1,closer-3 --handles X   make these images of product X again
  python run.py --vertex --recheck             check images already made against the current rules; remake failures
  python run.py --vertex --test-key            Vertex AI (Google Cloud credit): make ONE test image
  python run.py --vertex --list-models         the model IDs your project can use
  python run.py --vertex --batch auto          Batch round: collect finished results, then submit the next images
  python run.py --vertex --batch status        show the Batch jobs that are out
  python run.py --vertex --shard 0/3 --minutes 45 --no-report   worker 0 of 3 (GitHub runs 3 at once)
  python run.py --report                       write the progress report (output/report.csv and the Google Sheet)
  python run.py --vertex ...                   make the images through Vertex AI in your Google Cloud project
  python run.py --web ...                      make the images on the Gemini website (your PC, signed in with python web.py login)
"""
import argparse
import html
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from lc_images import config
from lc_images.gemini import Gemini, GenerationFailed, QuotaExhausted
from lc_images.pipeline import ProductRun, now, to_jpeg
from lc_images.shopify import Shopify

try:
    from playwright.sync_api import Error as BrowserError

    from lc_images.gemini_web import ControlMissing, NotSignedIn

    # Website problems stop the run (every product would fail the same way): sign-out, a missing control,
    # or the browser itself going away (Chrome closed, PC asleep, internet down)
    WEB_STOPS = (ControlMissing, NotSignedIn, BrowserError)
except ImportError:
    WEB_STOPS = ()

OUT = Path("output")
# A specific, original scene: very plain prompts can be refused as too close to existing images (IMAGE_RECITATION)
TEST_PROMPT = (
    "A watercolour illustration of a small hand-thrown ivory ceramic cup with a thin gold rim, standing on a "
    "folded sand-coloured linen cloth beside a single olive sprig, soft morning window light. No text."
)


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
            data = g.generate([], TEST_PROMPT, "1:1")
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


def test_vertex():
    from lc_images.vertex import GeminiVertex

    g = GeminiVertex(out_dir=OUT)
    print(f"Project: {g.project} | location: {g.location} | image model: {g.image_model} | check model: {g.check_model}")
    try:
        data = g.generate([], TEST_PROMPT, "1:1")
    except (QuotaExhausted, GenerationFailed, RuntimeError) as error:
        print(f"REFUSED: {error}")
        return 1
    OUT.mkdir(exist_ok=True)
    (OUT / "vertex-test.png").write_bytes(data)
    print(f"OK: Vertex AI made a test image ({len(data)} bytes), saved as output/vertex-test.png.")
    try:
        # one check, with the same picture detail and thinking settings the real checks use
        answer = g.ask_json([to_jpeg(data, max_side=1024)], 'Is there a picture? Reply as JSON: {"ok": true or false, "seen": "a few words"}')
        print(f"Check model {g.check_model}: {'OK' if answer.get('ok') else 'unexpected answer'} ({answer})")
    except (QuotaExhausted, RuntimeError) as error:
        print(f"Check model {g.check_model} REFUSED: {str(error)[:800]}")
        return 1
    print(f"Estimated spend so far: ${g.spend.data['usd']:.2f}. Tomorrow, check in Google Cloud: Billing > Reports, that it was paid by the credit.")
    return 0


def contact_sheet(results):
    """A simple page showing every image made in a dry run, for review."""
    rows = []
    for handle, status in results:
        folder = OUT / handle
        report = json.loads((folder / "report.json").read_text()) if (folder / "report.json").exists() else {}
        used = {v.get("local") for v in {**report.get("slots", {}), **report.get("colours", {})}.values() if v.get("status") == "done"}

        def tile(f):
            if f.name == "reference.jpg":
                label, cls = "PRODUCT PHOTO (reference)", "ref"
            elif f.name in used:
                label, cls = "USED: passed the check", "ok"
            else:
                label, cls = "REJECTED: failed the check, not used", "no"
            return f'<figure class={cls}><img src="{handle}/{f.name}" loading="lazy"><figcaption><b>{label}</b><br>{html.escape(f.stem)}</figcaption></figure>'

        files = sorted(folder.glob("*.jpg"), key=lambda f: (f.name != "reference.jpg", f.name not in used, f.name))
        tiles = "".join(tile(f) for f in files)
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
        "figure{margin:0;padding:6px;border:3px solid #E7DED2}figure.ok{border-color:#6D7464}figure.no{border-color:#b5523b;opacity:.75}figure.ref{border-color:#C6A77B}"
        "img{width:100%;height:auto;background:#E7DED2}figcaption{font-size:12px;color:#5F6456;margin-top:4px}figure.no b{color:#b5523b}figure.ok b{color:#5F6456}li{color:#7a3b2e}</style>"
        "<h1>Pilot images</h1><p>Green frame: USED (passed the check). Red frame: REJECTED (failed the check, never uploaded). "
        "Gold frame: the product photo Gemini worked from. try2 / try3 are re-dos after a failed check.</p>"
        + "".join(rows)
    )
    (OUT / "index.html").write_text(page, encoding="utf-8")


def make_generator(args, s):
    """The image maker for this run: Gemini website, Vertex AI, or the Gemini API key."""
    if args.web:
        from lc_images.gemini_web import GeminiWeb

        api = None
        if config.CHECK_SOURCE == "api":
            api = Gemini()
            print("Check model:", api.choose_models(require_image=False)[1], "| images: Gemini website")
        else:
            print("Images, photo choice and checks: Gemini website")
        g = GeminiWeb(api=api, out_dir=OUT).start()
        if not g.signed_in() and (g.open_app() or not g.signed_in()):
            g.close()
            raise SystemExit("Gemini is not signed in. Run: python web.py login")
        return g
    if args.vertex:
        from lc_images.vertex import GeminiVertex

        g = GeminiVertex(out_dir=OUT, shopify=s)
        print(f"Vertex AI, project {g.project}: images {g.image_model}, checks {g.check_model}; spend so far about ${g.spend.data['usd']:.2f} of ${config.VERTEX_BUDGET_USD:.0f}")
        return g
    g = Gemini()
    print("Models:", *g.choose_models())
    return g


def run_products(args, g, s, handles):
    """One-by-one: work through the products. Returns (results, counts, stopped, images passed)."""
    results, counts, stopped, images_made = [], {}, False, 0
    started = time.monotonic()
    for product in s.iter_products(handles or None):
        if len(results) >= args.limit:
            break
        if args.minutes and time.monotonic() - started >= args.minutes * 60:
            print(f"\n{args.minutes} minutes used: no new product is started; the next round carries on")
            break
        if product["productType"] not in config.PRODUCT_TYPES:
            continue
        if not in_shard(product, args.shard):
            continue  # another worker's product
        audit = json.loads((product.get("audit") or {}).get("value") or "{}")
        if args.recheck:
            if not audit.get("choice") or audit.get("status") in ("needs_photo", "skipped"):
                continue  # nothing made yet to recheck
        else:
            skip = ("done", "needs_photo", "skipped") if args.retry_manual else ("done", "manual", "needs_photo", "skipped")
            if audit.get("status") in skip and not handles:
                continue
            if waiting_on_batch(audit) and not handles:
                continue  # its images are out in a Batch job; the run's places go to products with work to do
        print(f"\n{product['title']}")
        run = ProductRun(product, g, s, out_dir=OUT if args.dry_run else None)
        if args.retry_manual:
            run.reopen_manual()
        if args.redo:
            redone = run.reopen([n.strip() for n in args.redo.split(",") if n.strip()])
            print(f"  making again: {', '.join(redone) or 'nothing (no such images yet)'}")
        try:
            if args.recheck:
                run.prepare(force=True)
                failed = run.recheck()
                print(f"  recheck: {len(failed)} image(s) to make again" + (f": {', '.join(failed)}" if failed else ""))
            status = run.run()
        except QuotaExhausted as error:
            print(f"  Limit reached: {str(error)[:300]}\n  Everything made so far is kept; run the same command again later.")
            status, stopped = "paused (limit)", True
        except KeyboardInterrupt:
            print("  Stopped by you (Ctrl+C). Everything made so far is kept; run the same command again to continue.")
            status, stopped = "paused (stopped by you)", True
        except WEB_STOPS as error:  # website problem: stop, every product would fail the same way
            print(f"  Stopped: {str(error)[:300]}\n  Everything made so far is kept; run the same command again to continue.")
            status, stopped = "paused (website problem)", True
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
        if stopped:
            break
    return results, counts, stopped, images_made


def in_shard(product, shard):
    """With --shard K/N (parallel workers), True for the products worker K looks after: product number modulo N is K."""
    if not shard:
        return True
    k, n = (int(x) for x in shard.split("/"))
    return int(product["id"].rsplit("/", 1)[-1]) % n == k


def waiting_on_batch(audit):
    """True when all the product's open images are out in Batch jobs, so there is nothing to make now."""
    states = [v.get("status") for group in ("slots", "colours") for v in (audit.get(group) or {}).values()]
    return "queued" in states and "pending" not in states


def run_batch(args, g, s, handles):
    """Batch mode: submit, collect or auto (collect, then submit the next round)."""
    from lc_images.batch import BatchRunner

    runner = BatchRunner(g, s, out_dir=OUT, dry_run=args.dry_run)
    stopped = False
    redo = [n.strip() for n in args.redo.split(",") if n.strip()] or None
    try:
        if args.batch == "submit":
            runner.submit(args.limit, handles, args.retry_manual, redo)
        elif args.batch == "collect":
            runner.collect()
        elif args.batch == "status":
            for job in runner.state["jobs"]:
                detail = g.batch_detail(job["name"]) if hasattr(g, "batch_detail") else g.batch_state(job["name"])
                print(f"  {job['name']}: {detail}; {job['count']} image(s), started {job['created']} UTC")
            if not runner.state["jobs"]:
                print("  No Batch jobs out.")
        else:
            runner.auto(args.limit, handles, args.retry_manual, redo)
    except QuotaExhausted as error:
        print(f"Stopped: {str(error)[:300]}")
        stopped = True
    c = runner.counts
    print(f"Batch: {c['submitted']} submitted, {c['passed']} passed, {c['retry']} to try again, {c['manual']} do manually, {c['again']} back in the queue")
    return [], {"batch": 1}, stopped, c["passed"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="make images into ./output only; change nothing in Shopify")
    ap.add_argument("--limit", type=int, default=config.MAX_PRODUCTS_PER_RUN)
    ap.add_argument("--handles", default="", help="comma-separated product handles to process")
    ap.add_argument("--test-key", action="store_true")
    ap.add_argument("--list-models", action="store_true", help="with --vertex: list the image and Gemini models your project can use")
    ap.add_argument("--sheet", action="store_true", help="rebuild output/index.html from the pilot folders already in output")
    ap.add_argument("--report", action="store_true", help="write the progress report (output/report.csv and the Google Sheet)")
    ap.add_argument("--retry-manual", action="store_true", help="try again the images marked 'do manually'")
    ap.add_argument("--recheck", action="store_true", help="check images already made against the current rules and remake the ones that fail")
    ap.add_argument("--redo", default="", help="make these images again, e.g. closer-1,closer-3 or closer (use with --handles)")
    ap.add_argument("--shard", default="", help="K/N: work only on worker K's share of the products (for N parallel workers)")
    ap.add_argument("--minutes", type=int, default=0, help="start no new product after this many minutes (0 = no time limit)")
    ap.add_argument("--no-report", action="store_true", help="do not update the report at the end (another step does it once)")
    ap.add_argument("--vertex", action="store_true", help="use Vertex AI in your Google Cloud project (signed in with gcloud, no API key)")
    ap.add_argument("--batch", choices=["submit", "collect", "auto", "status"], help="with --vertex: Batch mode (half price, results within 24h)")
    ap.add_argument("--web", action="store_true", help="make images on the Gemini website instead of the API (photo choice and checks too, unless CHECK_SOURCE=api)")
    args = ap.parse_args()

    if args.list_models and args.vertex:
        from lc_images.vertex import GeminiVertex

        try:
            models = GeminiVertex(out_dir=OUT).list_models()
        except Exception as error:
            print(f"Could not list models: {str(error)[:300]}")
            return 1
        images = [(n, d) for n, d in models if "image" in n.lower() or "image" in d.lower() or "banana" in (n + d).lower()]
        print("Image models:")
        for n, d in images:
            print(f"  {n}    {d}")
        print("\nGemini models (for checks):")
        for n, d in models:
            if n.lower().startswith("gemini") and (n, d) not in images:
                print(f"  {n}    {d}")
        return 0
    if args.sheet:
        folders = [(f.name, json.loads((f / "report.json").read_text()).get("status", "")) for f in sorted(OUT.iterdir()) if (f / "report.json").exists()]
        contact_sheet(folders)
        print(f"Rebuilt {OUT / 'index.html'} for {len(folders)} product(s).")
        return 0
    if args.report:
        from lc_images.report import make_report

        for where in make_report(Shopify(dry_run=True), OUT):
            print("Report written:", where)
        return 0
    if args.test_key:
        return test_vertex() if args.vertex else test_key()
    if args.batch and not args.vertex:
        raise SystemExit("Batch mode needs --vertex")
    if args.batch and args.dry_run:
        raise SystemExit("Batch jobs are real (and paid) even as a pilot. For a pilot use --vertex --dry-run without --batch.")
    if args.redo and not args.handles:
        raise SystemExit("--redo needs --handles, so only the products you name are made again")

    OUT.mkdir(exist_ok=True)
    s = Shopify(dry_run=args.dry_run)
    g = make_generator(args, s)
    handles = [h.strip() for h in args.handles.split(",") if h.strip()]

    started = datetime.now(timezone.utc)
    try:
        if args.batch:
            results, counts, stopped, images_made = run_batch(args, g, s, handles)
        else:
            results, counts, stopped, images_made = run_products(args, g, s, handles)
    finally:
        if args.web:
            try:
                g.close()
            except Exception:
                pass
        if args.vertex:
            g.spend.flush()  # always add this run's spending to the shared figure
    summary = {
        "date": started.strftime("%Y-%m-%d %H:%M UTC"),
        "mode": ("pilot (dry run)" if args.dry_run else "live") + (f", batch {args.batch}" if args.batch else ""),
        "products_worked_on": len(results),
        "statuses": counts,
        "images_passed": images_made,
        "gemini_image_calls": g.calls["image"],
        "gemini_check_calls": g.calls["check"],
        "image_source": "website" if args.web else ("vertex" if args.vertex else "api"),
        "estimated_vertex_spend_usd": g.spend.data["usd"] if args.vertex else None,
        "seconds_per_website_image": round(sum(g.timings) / len(g.timings), 1) if args.web and g.timings else None,
        "limit_reached": stopped,
        "products": [{"handle": h, "status": st} for h, st in results],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    if args.dry_run:
        if results:
            contact_sheet(results)
    else:
        try:
            s.set_metafields([{"ownerId": s.shop_id(), "namespace": "custom", "key": config.SUMMARY_KEY, "type": "json", "value": json.dumps({k: v for k, v in summary.items() if k != "products"})}])
        except Exception as error:  # the summary is a convenience; the products' own progress is already saved
            print(f"Could not save the run summary to Shopify ({str(error)[:150]}); it is in output/summary.json")
        if config.REPORT_SHEET_ID and not args.no_report:
            from lc_images.report import make_report

            try:
                for where in make_report(s, OUT):
                    print("Report updated:", where)
            except Exception as error:
                print(f"Could not update the report ({str(error)[:200]}); run python run.py --report later")
    print("\nSummary:", json.dumps({k: v for k, v in summary.items() if k != "products"}, indent=2))
    # Exit code for GitHub: 1 when a product had an error or the website stopped the run. Reaching a limit or the
    # spending cap is expected and not a failure (the summary says limit_reached).
    return 1 if counts.get("error") or counts.get("paused (website problem)") else 0


if __name__ == "__main__":
    sys.exit(main())
