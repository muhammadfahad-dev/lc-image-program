"""The progress report: one row per product, from what is really in Shopify, written to a Google Sheet
(and always to output/report.csv).

Columns: product, links, template, each image section, variant images, each text field, and what is left to do.
Each image or text column says Done, Retry (something failed and must be made again or by hand),
In progress (Batch work still out), or Not started.

Google Sheet access (one time; see README "Reports"):
  - The Sheet is shared (Editor) with the service account lc-images@<project>.iam.gserviceaccount.com.
  - On your PC, REPORT_SERVICE_ACCOUNT in .env names that account; your own gcloud sign-in may act as it
    (role "Service Account Token Creator"). On GitHub the run signs in as it directly.
  - REPORT_SHEET_ID is the long part of the Sheet's address between /d/ and /edit.
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .pipeline import classify_colour, listing_plan

TEXT_FIELDS = [  # key, column name, how many list items or lines it must have (None: just not empty)
    ("speciality", "Speciality", None),
    ("fabric_construction", "Fabric & construction", None),
    ("closer_description", "A little closer description", 2),
    ("fitting_titles", "Fitting titles", 5),
    ("fitting_subtexts", "Fitting subtexts", 5),
    ("fitting_extra_texts", "Fitting extra texts", 5),
]
IMAGE_FIELDS = [("fitting", "Fitting image"), ("multi", "Multi display images"), ("closer", "A little closer images")]

HEADER = (
    ["Product", "Admin link", "Store link", "Template", "Media images (1.1-1.4)"]
    + [name for _, name in IMAGE_FIELDS]
    + ["Variant images"]
    + [name for _, name, _ in TEXT_FIELDS]
    + ["Text content", "Image status", "To do", "Checked"]
)

REPORT_FIELDS = """
  id title handle productType status templateSuffix onlineStoreUrl
  options { name values }
  variants(first: 250) { nodes { selectedOptions { name value } media(first: 1) { nodes { id } } } }
  audit: metafield(namespace: "custom", key: "image_audit") { value }
  fitting: metafield(namespace: "custom", key: "fitting_image") { value }
  multi: metafield(namespace: "custom", key: "multi_display_images") { value }
  closer: metafield(namespace: "custom", key: "closer_images") { value }
  listing: metafield(namespace: "custom", key: "listing_audit") { value }
""" + "".join(f'  t_{key}: metafield(namespace: "custom", key: "{key}") {{ value }}\n' for key, _, _ in TEXT_FIELDS)


def _value(product, alias):
    return ((product.get(alias) or {}).get("value") or "").strip()


def _ids(raw):
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [raw]


def _text_status(raw, expected):
    if not raw:
        return "Retry (empty)"
    if expected is None:
        return "Done"
    if raw.startswith("["):
        try:
            count = len([x for x in json.loads(raw) if str(x).strip()])
        except json.JSONDecodeError:
            count = 0
    else:
        count = len([line for line in raw.splitlines() if line.strip()])
    return "Done" if count == expected else f"Retry ({count} of {expected})"


def image_section_status(field, raw, audit, placeholders):
    """Done / Retry / In progress / Not started for one image section."""
    ids = _ids(raw)
    slots = {}
    if audit.get("choice"):
        plan = listing_plan(audit["choice"]["front_open"], audit["choice"]["has_design_work"])
        slots = {s: audit["slots"].get(s, {}) for s in plan[field]}
    waiting = [s for s, st in slots.items() if st.get("status") in ("queued", "pending")]
    holders = [i for i in ids if i in placeholders]
    if ids and not holders:
        return "Done"
    if holders:
        todo = [s for s, st in slots.items() if st.get("status") == "manual"] or [f"{len(holders)} placeholder(s)"]
        return f"Retry ({len(holders)} of {len(ids)}: {', '.join(todo)})"
    if waiting:
        return "In progress"
    if any(st.get("status") == "manual" for st in slots.values()):
        return "Retry"
    return "Not started"


def media_status(audit):
    if audit.get("status") == "needs_photo":
        return "Needs photo"
    if not audit.get("choice"):
        return "Not started"
    plan = listing_plan(audit["choice"]["front_open"], audit["choice"]["has_design_work"])
    states = [audit["slots"].get(s, {}).get("status") for s in plan["media"]]
    if all(st == "existing" for st in states):
        return "Done (by hand)"
    if all(st == "done" for st in states):
        return "Done"
    manual = [s for s in plan["media"] if audit["slots"].get(s, {}).get("status") == "manual"]
    if manual:
        return f"Retry ({', '.join(manual)})"
    if any(st in ("queued", "pending") for st in states):
        return "In progress"
    return "In progress" if any(states) else "Not started"


def variant_status(product, audit=None):
    """Done, In progress (colour images still being made), or Not completed with the colours left to do."""
    audit = audit or {}
    colour_opts = [o for o in product["options"] if o["name"].strip().lower() in config.COLOUR_OPTION_NAMES]
    if not colour_opts:
        return "No colours"
    # the program is still on this product: single colours without an image yet are on their way
    # (they are made once the Media images are finished)
    working = audit.get("status") == "in_progress"
    missing, waiting, total = [], [], 0
    for option in colour_opts:
        for value in option["values"]:
            total += 1
            variants = [v for v in product["variants"]["nodes"] if any(o["name"] == option["name"] and o["value"] == value for o in v["selectedOptions"])]
            if not variants or not all(v["media"]["nodes"] for v in variants):
                kind = classify_colour(option["name"], value, product["productType"])
                state = (audit.get("colours") or {}).get(value, {}).get("status", "")
                if kind == "single" and (state in ("queued", "pending") or (working and not state)):
                    waiting.append(value)
                else:
                    missing.append(value if kind == "single" else f"{value} ({kind}, by hand)")
    done = total - len(missing) - len(waiting)
    if not missing and not waiting:
        return "Done"
    if not missing:
        return f"In progress ({done} of {total} done)"
    return f"Not completed ({done} of {total}): {', '.join(missing)}" + (f"; in progress: {len(waiting)}" if waiting else "")


def row_for(product, placeholders, store_slug):
    audit = json.loads(_value(product, "audit") or "{}")
    audit.setdefault("slots", {})
    numeric = product["id"].rsplit("/", 1)[-1]
    admin = f"https://admin.shopify.com/store/{store_slug}/products/{numeric}"
    store = product.get("onlineStoreUrl") or f"https://{config.STORE_URL}/products/{product['handle']}"
    is_abaya = product["productType"] in config.PRODUCT_TYPES

    media = media_status(audit) if is_abaya else "Skipped (not an abaya)"
    sections = [image_section_status(f, _value(product, f), audit, placeholders) if is_abaya else "Skipped" for f, _ in IMAGE_FIELDS]
    texts = [_text_status(_value(product, f"t_{key}"), expected) for key, _, expected in TEXT_FIELDS]
    text_overall = "Done" if all(t == "Done" for t in texts) else "Retry"
    listing = json.loads(_value(product, "listing") or "{}")
    if listing.get("status") == "Needs info":
        text_overall = "Retry (needs info)"

    if not is_abaya:
        return (
            [product["title"], f'=HYPERLINK("{admin}","Admin")', f'=HYPERLINK("{store}","Store")', product.get("templateSuffix") or "Default product", media]
            + sections + ["Skipped"] + ["Skipped"] * len(TEXT_FIELDS)
            + ["Skipped", "skipped", "", datetime.now(timezone.utc).strftime("%Y-%m-%d")]
        )
    todo = []
    if media == "Needs photo":
        todo.append("Needs a clear full-length front photo: " + "; ".join(audit.get("notes", [])))
    for name, status in [("Media", media)] + list(zip([n for _, n in IMAGE_FIELDS], sections)):
        if status.startswith("Retry"):
            todo.append(f"{name}: {status[6:].strip('()') or 'make again'}")
    variants = variant_status(product, audit)
    if variants.startswith("Not completed"):
        todo.append("Variant images: " + variants.split(": ", 1)[1])
    bad_texts = [name for (_, name, _), t in zip(TEXT_FIELDS, texts) if t != "Done"]
    if bad_texts:
        todo.append("Text: " + ", ".join(bad_texts))
    if listing.get("missing"):
        todo.append("Listing: " + listing["missing"])

    return (
        [product["title"], f'=HYPERLINK("{admin}","Admin")', f'=HYPERLINK("{store}","Store")', product.get("templateSuffix") or "Default product", media]
        + sections + [variants] + texts
        + [text_overall, IMAGE_STATUS.get(audit.get("status", ""), audit.get("status") or "Not started"), "; ".join(todo), datetime.now(timezone.utc).strftime("%Y-%m-%d")]
    )


# The program's own status for the product, in plain words (the "Image status" column)
IMAGE_STATUS = {
    "in_progress": "In progress",
    "done": "Done",
    "manual": "Done, some images by hand",
    "needs_photo": "Needs photo",
    "skipped": "Skipped",
    "": "Not started",
}


def worked_on(product):
    """True once the image program has started on the product (it has an image automation record)."""
    return bool(_value(product, "audit"))


def build_rows(shopify, log=print):
    placeholders = set((shopify.get_shop_json("image_placeholders", {}) or {}).values())
    store_slug = shopify.domain.split(".")[0]
    rows, after = [], None
    while True:
        d = shopify.gql(
            f"query($after: String) {{ products(first: 3, after: $after, sortKey: TITLE, query: \"status:active\") {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {REPORT_FIELDS} }} }} }}",
            {"after": after},
        )
        page = d["products"]
        rows.extend(row_for(p, placeholders, store_slug) for p in page["nodes"] if config.REPORT_ALL_PRODUCTS or worked_on(p))
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    log(f"Report: {len(rows)} products" + ("" if config.REPORT_ALL_PRODUCTS else " the program has worked on"))
    return rows


def write_csv(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig so Excel shows the text correctly
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow([c.split('"')[1] if str(c).startswith("=HYPERLINK") else c for c in row])


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def sheet_session():
    """A signed-in connection for Google Sheets.
    On your PC, gcloud cannot ask for Sheets permission (Google blocks it), so the program writes as the service
    account in REPORT_SERVICE_ACCOUNT instead: your own sign-in is only allowed to act as that account, and the
    Sheet is shared with it. On GitHub the run is already signed in as that service account."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    if config.REPORT_SERVICE_ACCOUNT:
        from google.auth import impersonated_credentials

        source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds = impersonated_credentials.Credentials(
            source_credentials=source, target_principal=config.REPORT_SERVICE_ACCOUNT, target_scopes=[SHEETS_SCOPE], lifetime=3600,
        )
    else:
        creds, _ = google.auth.default(scopes=[SHEETS_SCOPE])
    return AuthorizedSession(creds)


def write_sheet(rows, sheet_id=None, tab=None, session=None):
    """Replace the tab's contents with the header and rows. The tab is created (header frozen) if it does not exist."""
    sheet_id = sheet_id or config.REPORT_SHEET_ID
    tab = tab or config.REPORT_SHEET_TAB
    if session is None:
        session = sheet_session()
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"

    def ok(r):
        if r.status_code >= 300:
            raise RuntimeError(f"Google Sheets {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    info = ok(session.get(base, params={"fields": "sheets.properties"}))
    names = {s["properties"]["title"]: s["properties"]["sheetId"] for s in info.get("sheets", [])}
    if tab not in names:
        added = ok(session.post(f"{base}:batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": tab, "gridProperties": {"frozenRowCount": 1}}}}]}))
        names[tab] = added["replies"][0]["addSheet"]["properties"]["sheetId"]
        _add_colours(session, base, names[tab], ok)
    rng = f"'{tab}'"
    ok(session.post(f"{base}/values/{rng}!A:Z:clear", json={}))
    ok(session.put(f"{base}/values/{rng}!A1", params={"valueInputOption": "USER_ENTERED"}, json={"values": [HEADER] + rows}))
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}"


def _add_colours(session, base, tab_id, ok):
    """Green for Done, red for Retry and Not completed, amber for In progress (set once, when the tab is made)."""
    rules = []
    for text, colour in (("Done", (0.85, 0.93, 0.83)), ("Retry", (0.96, 0.8, 0.78)), ("Not completed", (0.96, 0.8, 0.78)), ("In progress", (1, 0.93, 0.75))):
        rules.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": 3, "endColumnIndex": len(HEADER)}],
            "booleanRule": {"condition": {"type": "TEXT_STARTS_WITH", "values": [{"userEnteredValue": text}]},
                            "format": {"backgroundColor": {"red": colour[0], "green": colour[1], "blue": colour[2]}}},
        }}})
    ok(session.post(f"{base}:batchUpdate", json={"requests": rules}))


def make_report(shopify, out_dir="output", log=print):
    """Build the rows, save the CSV, and update the Google Sheet if one is set. Returns where it went."""
    rows = build_rows(shopify, log)
    csv_path = Path(out_dir) / "report.csv"
    write_csv(rows, csv_path)
    where = [str(csv_path)]
    if config.REPORT_SHEET_ID:
        try:
            where.append(write_sheet(rows))
        except Exception as error:
            log(f"The CSV is saved, but the Google Sheet could not be updated: {sheet_error_hint(error)}")
    return where


def sheet_error_hint(error):
    """A short, plain reason for a Google Sheet error, with what to do."""
    text = str(error)
    if "SERVICE_DISABLED" in text or "has not been used in project" in text:
        api = "IAM Service Account Credentials API" if "iamcredentials" in text else ("Google Sheets API" if "sheets" in text else "a Google API")
        return f"the {api} is not switched on. Run: gcloud services enable iamcredentials.googleapis.com sheets.googleapis.com, wait 2-3 minutes, then try again."
    if "getAccessToken" in text or "iam.serviceAccounts" in text:
        return "you are not allowed to act as the service account yet. Run the add-iam-policy-binding command from the README (Reports), wait 2 minutes, then try again."
    if "Google Sheets 403" in text or "Google Sheets 404" in text:
        return "the Sheet is not shared (Editor) with the service account, or REPORT_SHEET_ID in .env is wrong."
    return text[:300]
