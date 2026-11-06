"""Set the shipping weight of every abaya in Shopify by the work on it.

    python set_weights.py            test: lists what would change, changes nothing
    python set_weights.py --apply    writes the new weights

An abaya with work on it (stonework, embroidery or handcrafted cutdana) weighs 990 g; a plain or printed abaya 700 g.
An abaya has work when it is in a Stonework, Embroidery, Handcrafted Cutdana or Stonework Embroidery collection, or its
title names the work (stonework, stone, embroider, cutdana, handwork, handcrafted). So a plain or printed abaya that
also has stonework or embroidery gets 990 g.

Every variant (colour / size) of a product gets the weight. Only the weight changes: nothing else on the product is
touched. Kids abayas follow the same rule; a product in the Kids collection that is not an abaya (no "abaya" in its
title, e.g. a prayer dress) is not changed and is listed. Each run writes output/weights_report.csv with every variant's old and new weight, so any change can be put back by hand.
"""
import argparse
import csv
import re
import sys
import time
from pathlib import Path

from lc_images.shopify import Shopify

WORK_GRAMS, PLAIN_GRAMS = 990, 700
# Shopify collection handle -> collection name. Abayas with work (990 g) ...
WORK_COLLECTIONS = {
    "stonework-abaya": "Stonework Abaya",
    "embroidery-abaya": "Embroidery Abaya",
    "handcrafted-cutdana-abaya": "Handcrafted Cutdana Abaya",
    "stonework-embroidery-abaya": "Stonework Embroidery Abaya",
}
# ... and plain or printed abayas (700 g unless they also have work)
PLAIN_COLLECTIONS = {
    "plain-abaya": "Plain Abaya",
    "printed-abaya": "Printed Abaya",
}
WORK_IN_TITLE = re.compile(r"stone|embroider|cutdana|hand\s*-?\s*work|handcraft", re.I)
KIDS = ("kids", "Kids")  # kids abayas: same rule by work; other kids products are left alone
REPORT = Path("output/weights_report.csv")

COLLECTION_QUERY = """
query($handle: String!, $after: String) {
  collectionByIdentifier(identifier: {handle: $handle}) {
    products(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id title handle status
        variants(first: 250) {
          nodes { id title inventoryItem { measurement { weight { unit value } } } }
        }
      }
    }
  }
}"""

UPDATE = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id }
    userErrors { field message }
  }
}"""

TO_GRAMS = {"GRAMS": 1, "KILOGRAMS": 1000, "OUNCES": 28.349523125, "POUNDS": 453.59237}


def grams(weight):
    """A Shopify weight as grams (None when it has none)."""
    if not weight or weight.get("value") is None:
        return None
    return round(float(weight["value"]) * TO_GRAMS.get(weight.get("unit"), 1), 1)


def shown(weight):
    if not weight or weight.get("value") is None:
        return ""
    unit = {"GRAMS": "g", "KILOGRAMS": "kg", "OUNCES": "oz", "POUNDS": "lb"}.get(weight.get("unit"), weight.get("unit", ""))
    return f"{float(weight['value']):g} {unit}"


def collection_products(shopify, handle):
    after = None
    while True:
        data = shopify.gql(COLLECTION_QUERY, {"handle": handle, "after": after})["collectionByIdentifier"]
        if data is None:
            raise RuntimeError(f"Shopify has no collection with the handle {handle}")
        yield from data["products"]["nodes"]
        info = data["products"]["pageInfo"]
        if not info["hasNextPage"]:
            return
        after = info["endCursor"]


def admin_link(shopify, product_id):
    return f"https://admin.shopify.com/store/{shopify.domain.split('.')[0]}/products/{product_id.rsplit('/', 1)[-1]}"


def run(shopify, apply=False, log=print):
    # Every abaya in the six collections, with the collections it is in
    products, wanted = {}, {}
    for handle, name in {**WORK_COLLECTIONS, **PLAIN_COLLECTIONS}.items():
        count = 0
        for product in collection_products(shopify, handle):
            products[product["id"]] = product
            wanted.setdefault(product["id"], []).append(name)
            count += 1
        log(f"{name}: {count} products")
    count = 0
    for product in collection_products(shopify, KIDS[0]):
        products[product["id"]] = product
        wanted.setdefault(product["id"], []).append(KIDS[1])
        count += 1
    log(f"{KIDS[1]}: {count} products")

    rows, counts = [], {"changed": 0, "already": 0, "other": 0}
    for product_id, names in wanted.items():
        product = products[product_id]
        link = admin_link(shopify, product_id)
        collections = ", ".join(names)
        reason = ""
        if names == [KIDS[1]] and "abaya" not in product["title"].lower():
            reason = "Not an abaya (Kids collection only)"
            counts["other"] += 1
        if reason:
            for variant in product["variants"]["nodes"]:
                weight = ((variant.get("inventoryItem") or {}).get("measurement") or {}).get("weight")
                rows.append([product["title"], variant["title"], collections, product["status"], shown(weight), "", "Not changed", reason, link])
            continue

        work = [n for n in names if n in WORK_COLLECTIONS.values()]
        if work:
            target, why = WORK_GRAMS, f"Work: in {', '.join(work)}"
        elif WORK_IN_TITLE.search(product["title"]):
            target, why = WORK_GRAMS, f"Work: title mentions {WORK_IN_TITLE.search(product['title']).group(0).lower()}"
        else:
            target, why = PLAIN_GRAMS, "Plain or printed, no work"
        to_change = []
        for variant in product["variants"]["nodes"]:
            weight = ((variant.get("inventoryItem") or {}).get("measurement") or {}).get("weight")
            if grams(weight) == target:
                rows.append([product["title"], variant["title"], collections, product["status"], shown(weight), f"{target} g", "Already right", why, link])
            else:
                to_change.append((variant, weight))
        if not to_change:
            counts["already"] += 1
            continue

        status = "Would change"
        if apply:
            variants = [{"id": v["id"], "inventoryItem": {"measurement": {"weight": {"unit": "GRAMS", "value": target}}}} for v, _ in to_change]
            result = shopify.gql(UPDATE, {"productId": product_id, "variants": variants})["productVariantsBulkUpdate"]
            if result.get("userErrors"):
                status = "Failed: " + "; ".join(e.get("message", "") for e in result["userErrors"])
            else:
                status = "Changed"
            time.sleep(0.3)  # stay well inside Shopify's request limit
        if not status.startswith("Failed"):
            counts["changed"] += 1
        for variant, weight in to_change:
            rows.append([product["title"], variant["title"], collections, product["status"], shown(weight), f"{target} g", status, why, link])
        log(f"{status}: {product['title']} ({len(to_change)} variants) -> {target} g")

    order = {"Not changed": 0, "Failed": 1}
    rows.sort(key=lambda r: order.get(r[6].split(":")[0], 2))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
        out = csv.writer(f)
        out.writerow(["Product", "Variant", "Collections", "Product status", "Old weight", "New weight", "Result", "Reason", "Admin link"])
        out.writerows(rows)

    verb = "changed" if apply else "to change"
    log(f"\n{len(wanted)} products: {counts['changed']} {verb}, {counts['already']} already right, "
        f"{counts['other']} not abayas (not changed). Report: {REPORT}")
    skipped = [r for r in rows if r[6] == "Not changed"]
    if skipped:
        log("Not changed (set these by hand if needed):")
        for title in dict.fromkeys(r[0] for r in skipped):
            reason = next(r[7] for r in skipped if r[0] == title)
            log(f"  - {title}: {reason}")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Set abaya shipping weights by the work on them")
    ap.add_argument("--apply", action="store_true", help="write the weights (without it: test only, changes nothing)")
    args = ap.parse_args()
    started = time.time()
    run(Shopify(), apply=args.apply)
    print(f"Finished in {int(time.time() - started)} s.")


if __name__ == "__main__":
    sys.exit(main())
