"""Upload every abaya's product images from Shopify to the client's Google Drive.

For each product in the seven abaya collections, all images uploaded on the product are downloaded at full quality,
in this order: the Media gallery, then the Fitting, Multi display and A little closer images (each once; the black
placeholders are left out). They go into
    ABAYA PICTURE EDITED / <type folder> / <product name> / <product name> 1.jpg, 2.jpg ...
A product in two collections gets a folder in both type folders.

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
    products(first: 20, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        id title handle status
        media(first: 100) {{ nodes {{ {IMAGE_REF} }} }}
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


def product_images(product, placeholders):
    """The product's images in page order (Media, Fitting, Multi display, A little closer), each once,
    without the black placeholders. Returns [(shopify image id, url)]."""
    found = [n for n in (product.get("media") or {}).get("nodes", []) if n]
    fitting = ((product.get("fitting") or {}).get("reference"))
    if fitting:
        found.append(fitting)
    for key in ("multi", "closer"):
        found.extend(n for n in (((product.get(key) or {}).get("references") or {}).get("nodes", [])) if n)
    images, seen = [], set()
    for node in found:
        media_id, url = node.get("id"), ((node.get("image") or {}).get("url"))
        if not media_id or not url or media_id in placeholders or media_id in seen:
            continue
        seen.add(media_id)
        images.append((media_id, url))
    return images


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

    # The seven type folders must already exist; the program never makes or renames them
    type_folders = {}
    for handle, folder_name in FOLDERS.items():
        folder_id = drive.folder_named(PARENT_FOLDER_ID, folder_name)
        if not folder_id:
            raise RuntimeError(f'No folder named "{folder_name}" inside ABAYA PICTURE EDITED. Check the name in Drive (it must match exactly).')
        type_folders[handle] = folder_id

    rows, done_products = [], set()
    totals = {"uploaded": 0, "already there": 0}
    for handle, folder_name in FOLDERS.items():
        for product in collection_products(shopify, handle):
            if handles and product["handle"] not in handles:
                continue
            if limit and product["id"] not in done_products and len(done_products) >= limit:
                continue
            done_products.add(product["id"])
            numeric = product["id"].rsplit("/", 1)[-1]
            name = safe_name(product["title"])
            images = product_images(product, placeholders)
            if not images:
                rows.append([folder_name, product["title"], 0, 0, "no images"])
                log(f"{folder_name} / {name}: no images, skipped")
                continue
            folder_id = drive.product_folder(type_folders[handle], name, numeric, create=not dry_run)
            have = drive.uploaded_media(folder_id) if folder_id else set()
            uploaded = 0
            for position, (media_id, url) in enumerate(images, start=1):
                if media_id in have:
                    continue
                file_name = f"{name} {position}{extension(url)}"
                if dry_run:
                    uploaded += 1
                    continue
                data = download(url, http)
                drive.upload(folder_id, file_name, data, MIME[extension(url)], media_id)
                uploaded += 1
            skipped = len(images) - uploaded
            totals["uploaded"] += uploaded
            totals["already there"] += skipped
            rows.append([folder_name, product["title"], uploaded, skipped, "would upload" if dry_run else "done"])
            log(f"{folder_name} / {name}: {uploaded} {'to upload' if dry_run else 'uploaded'}, {skipped} already there")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Folder", "Product", "Images uploaded" if not dry_run else "Images to upload", "Already there", "Result"])
        w.writerows(rows)
    log(f"\n{len(done_products)} products, {totals['uploaded']} images {'to upload' if dry_run else 'uploaded'}, "
        f"{totals['already there']} already there. Report: {REPORT}")
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
    print("A browser window opens: sign in with the Gmail that has Editor access to ABAYA PICTURE EDITED and allow.")
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
