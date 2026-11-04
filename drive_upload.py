"""Upload every abaya's product images from Shopify to the client's Google Drive.

For each product in the seven abaya collections, all images uploaded on the product are downloaded at full quality,
in this order: the Media gallery, the colour (variant) images, then the Fitting, Multi display and A little closer
images, each once. They go into
    ABAYA PICTURE EDITED / <type folder> / <product name> / <product name> 1.jpg, 2.jpg ...
A product in two collections gets a folder in both type folders.

A product with any black image (a "do manually" placeholder, or any image that is all black) is skipped entirely,
in every folder, and marked in the report; nothing of it is uploaded until the black image is replaced.

Safe to run again: images already uploaded (recognised by their Shopify image ID) are skipped, so a second run only
adds what is new. Nothing is ever deleted or changed, in Drive or in Shopify (Shopify is only read).

  python drive_upload.py --authorize        one-time Google sign-in (on your PC); prints the refresh token to save
  python drive_upload.py --dry-run          list what would be uploaded, upload nothing
  python drive_upload.py --limit 3          upload the first 3 products only (a test)
  python drive_upload.py                    upload everything
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import io

import requests

from lc_images import config
from lc_images.gdrive import SCOPE, Drive
from lc_images.shopify import Shopify

# Shopify collection handle -> folder name inside ABAYA PICTURE EDITED (names exactly as in Drive)
FOLDERS = {
    "stonework-embroidery-abaya": "Embroidery + StoneWork",
    "embroidery-abaya": "Embroidery Abaya",
    "handcrafted-cutdana-abaya": "Hand Work Abaya",
    "plain-abaya": "Plain Abaya",
    "printed-abaya": "Printed Abaya",
    "stonework-abaya": "Stone Work Abaya",
    "kids": "Kids Abaya",
}
PARENT_FOLDER_ID = config.env("DRIVE_PARENT_FOLDER_ID", "1rFNWDmDwLs8QpWnY7zvXN_jacqGWjYrR")  # ABAYA PICTURE EDITED
GOOGLE_CLIENT_ID = config.env("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_CLIENT_SECRET = config.env("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = config.env("GOOGLE_OAUTH_REFRESH_TOKEN")
REPORT = Path("output") / "drive_report.csv"

IMAGE_REF = "... on MediaImage { id image { url } }"
COLLECTION_QUERY = f"""
query($handle: String!, $after: String) {{
  collectionByIdentifier(identifier: {{handle: $handle}}) {{
    products(first: 4, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        id title handle status
        media(first: 100) {{ nodes {{ {IMAGE_REF} }} }}
        variants(first: 60) {{ nodes {{ media(first: 1) {{ nodes {{ {IMAGE_REF} }} }} }} }}
        fitting: metafield(namespace: "custom", key: "fitting_image") {{ reference {{ {IMAGE_REF} }} }}
        multi: metafield(namespace: "custom", key: "multi_display_images") {{ references(first: 20) {{ nodes {{ {IMAGE_REF} }} }} }}
        closer: metafield(namespace: "custom", key: "closer_images") {{ references(first: 20) {{ nodes {{ {IMAGE_REF} }} }} }}
      }}
    }}
  }}
}}"""

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif"}


def safe_name(title):
    """A product title as a Drive / Windows file name: '|' becomes '-', characters Windows refuses are dropped."""
    name = title.replace("|", "-")
    name = re.sub(r'[\\/:*?"<>]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")
    return name[:180] or "Untitled product"


SECTIONS = (("Media", "media"), ("Colour", "variants"), ("Fitting", "fitting"), ("Multi display", "multi"), ("A little closer", "closer"))


def product_images(product):
    """The product's images in page order (Media gallery, colour images, Fitting, Multi display, A little closer),
    each once. Returns [(shopify image id, url, section name)]."""
    found = []
    for section, key in SECTIONS:
        value = product.get(key) or {}
        if key == "media":
            nodes = value.get("nodes", [])
        elif key == "variants":
            nodes = [m for v in value.get("nodes", []) for m in ((v.get("media") or {}).get("nodes", []))]
        elif key == "fitting":
            nodes = [value.get("reference")]
        else:
            nodes = ((value.get("references") or {}).get("nodes", []))
        found.extend((n, section) for n in nodes if n)
    images, seen = [], set()
    for node, section in found:
        media_id, url = node.get("id"), ((node.get("image") or {}).get("url"))
        if not media_id or not url or media_id in seen:  # videos have no image; a colour image is already in Media
            continue
        seen.add(media_id)
        images.append((media_id, url, section))
    return images


def is_black(data):
    """True when an image is all (or almost all) black: the program's placeholders, or a black image put in by hand."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            histogram = im.convert("L").resize((48, 48)).histogram()
    except Exception:
        return False  # not readable as an image: not judged black (it is still uploaded as it is)
    # Real photos, even close-ups of black fabric, have well over half their pixels above this level (checked on the
    # store's black embroidered abayas: 64-91%); the placeholders have none
    return sum(histogram[25:]) <= sum(histogram) * 0.01


def black_images(images, placeholders, files):
    """The positions of black images: known placeholders, files named lc-PLACEHOLDER, or all-black pixels."""
    found = []
    for position, (media_id, url, section) in enumerate(images, start=1):
        if media_id in placeholders or "lc-placeholder" in url.lower() or is_black(files[media_id]):
            found.append(f"{section} (image {position})")
    return found


def collection_products(shopify, handle):
    after = None
    while True:
        data = shopify.gql(COLLECTION_QUERY, {"handle": handle, "after": after})["collectionByIdentifier"]
        if data is None:
            raise RuntimeError(f"Shopify has no collection with the handle {handle}")
        page = data["products"]
        for product in page["nodes"]:
            if product["status"] == "ACTIVE":
                yield product
        if not page["pageInfo"]["hasNextPage"]:
            return
        after = page["pageInfo"]["endCursor"]


def download(url, http):
    for attempt in range(4):
        try:
            r = http.get(url, timeout=180)
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(5 + attempt * 10)


def extension(url):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in MIME else ".jpg"


def run(shopify, drive, limit=None, handles=None, dry_run=False, http=None, log=print):
    http = http or requests.Session()
    placeholders = set((shopify.get_shop_json("image_placeholders", {}) or {}).values())
    store_slug = getattr(shopify, "domain", "").split(".")[0]

    # The seven type folders must already exist; the program never makes or renames them
    type_folders = {}
    for handle, folder_name in FOLDERS.items():
        folder_id = drive.folder_named(PARENT_FOLDER_ID, folder_name)
        if not folder_id:
            raise RuntimeError(f'No folder named "{folder_name}" inside ABAYA PICTURE EDITED. Check the name in Drive (it must match exactly).')
        type_folders[handle] = folder_id

    rows, done_products, black = [], set(), {}
    totals = {"uploaded": 0, "already there": 0, "skipped": 0}
    for handle, folder_name in FOLDERS.items():
        for product in collection_products(shopify, handle):
            if handles and product["handle"] not in handles:
                continue
            if limit and product["id"] not in done_products and len(done_products) >= limit:
                continue
            done_products.add(product["id"])
            numeric = product["id"].rsplit("/", 1)[-1]
            admin = f"https://admin.shopify.com/store/{store_slug}/products/{numeric}"
            name = safe_name(product["title"])
            images = product_images(product)
            if not images:
                rows.append([folder_name, product["title"], admin, "Skipped", 0, 0, "No images"])
                log(f"{folder_name} / {name}: no images, skipped")
                continue

            folder_id = drive.product_folder(type_folders[handle], name, numeric, create=False)
            have = drive.uploaded_media(folder_id) if folder_id else set()
            # Every image is downloaded and looked at before anything of the product is uploaded
            if product["id"] in black:
                files = None
            else:
                files = {media_id: download(url, http) for media_id, url, _ in images}
                black[product["id"]] = black_images(images, placeholders, files)
            if black[product["id"]]:
                totals["skipped"] += 1
                reason = "Black image in " + ", ".join(black[product["id"]])
                rows.append([folder_name, product["title"], admin, "Skipped - black image", 0, len(have), reason])
                log(f"{folder_name} / {name}: SKIPPED, {reason}")
                continue

            new = [(media_id, url, position) for position, (media_id, url, _) in enumerate(images, start=1) if media_id not in have]
            if new and not dry_run:
                folder_id = folder_id or drive.product_folder(type_folders[handle], name, numeric)
                for media_id, url, position in new:
                    data = files[media_id] if files else download(url, http)
                    drive.upload(folder_id, f"{name} {position}{extension(url)}", data, MIME[extension(url)], media_id)
            totals["uploaded"] += len(new)
            totals["already there"] += len(images) - len(new)
            status = "Would upload" if dry_run else "Uploaded"
            if not new:
                status = "Already in Drive"
            rows.append([folder_name, product["title"], admin, status, len(new), len(images) - len(new), ""])
            log(f"{folder_name} / {name}: {len(new)} {'to upload' if dry_run else 'uploaded'}, {len(images) - len(new)} already there")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Folder", "Product", "Admin link", "Status", "Images to upload" if dry_run else "Images uploaded", "Already in Drive", "Reason"])
        # skipped products first, so they are easy to find
        w.writerows(sorted(rows, key=lambda row: not row[3].startswith("Skipped")))
    skipped = sorted({row[1] for row in rows if row[3] == "Skipped - black image"})
    log(f"\n{len(done_products)} products, {totals['uploaded']} images {'to upload' if dry_run else 'uploaded'}, "
        f"{totals['already there']} already there. Report: {REPORT}")
    if skipped:
        log(f"{len(skipped)} products skipped because of a black image (fix them in Shopify, then run again):")
        for title in skipped:
            log(f"  - {title}")
    return rows


def authorize():
    """One-time sign-in on your own PC: opens Google's approval page and prints the refresh token to save."""
    import http.server
    import secrets
    import urllib.parse
    import webbrowser

    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        sys.exit("Add GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET to the .env file first (see docs/DRIVE-UPLOAD.md).")
    state, result = secrets.token_urlsafe(16), {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<p>Done. You can close this tab and go back to the program.</p>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
    })
    print("A browser window opens: sign in with the Gmail that owns ABAYA PICTURE EDITED (the client) and allow.")
    print(f"If it does not open, copy this link into the browser:\n{url}\n")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if result.get("state") != state or "code" not in result:
        sys.exit(f"Sign-in did not finish: {result.get('error', 'wrong reply')}")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "code": result["code"], "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }, timeout=60)
    token = r.json().get("refresh_token")
    if not token:
        sys.exit(f"Google gave no refresh token: {r.text[:300]}")
    print("Signed in. Save this as GOOGLE_OAUTH_REFRESH_TOKEN (in .env, and as a GitHub secret). Keep it private:\n")
    print(token)


def main():
    ap = argparse.ArgumentParser(description="Upload abaya images from Shopify to Google Drive")
    ap.add_argument("--authorize", action="store_true", help="one-time Google sign-in; prints the refresh token")
    ap.add_argument("--dry-run", action="store_true", help="list what would be uploaded; upload nothing")
    ap.add_argument("--limit", type=int, default=0, help="most products to do (a test run)")
    ap.add_argument("--handles", default="", help="only these product handles, comma-separated")
    args = ap.parse_args()
    if args.authorize:
        return authorize()
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN):
        sys.exit("Google Drive sign-in is missing: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN.")
    started = datetime.now(timezone.utc)
    handles = {h.strip() for h in args.handles.split(",") if h.strip()} or None
    run(Shopify(), Drive(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN), args.limit or None, handles, args.dry_run)
    print(f"Finished in {int((datetime.now(timezone.utc) - started).total_seconds() // 60)} min.")


if __name__ == "__main__":
    main()
