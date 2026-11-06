"""Set the shipping weight of every abaya in Shopify by its collection.

    python set_weights.py            test: lists what would change, changes nothing
    python set_weights.py --apply    writes the new weights

| Collection                  | Weight |
|-----------------------------|--------|
| Plain Abaya                 | 700 g  |
| Printed Abaya               | 700 g  |
| Stonework Abaya             | 990 g  |
| Embroidery Abaya            | 990 g  |
| Handcrafted Cutdana Abaya   | 990 g  |
| Stonework Embroidery Abaya  | 990 g  |

Every variant (colour / size) of a product gets the weight. Only the weight changes: nothing else on the product is
touched. A product in two collections with different weights (e.g. Plain and Stonework) is not changed and is listed as
a conflict; so is a product that is also in the Kids collection (kids abayas are lighter). Each run writes
output/weights_report.csv with every variant's old and new weight, so any change can be put back by hand.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

from lc_images.shopify import Shopify

# Shopify collection handle -> (collection name, weight in grams)
WEIGHTS = {
    "plain-abaya": ("Plain Abaya", 700),
    "printed-abaya": ("Printed Abaya", 700),
    "stonework-abaya": ("Stonework Abaya", 990),
    "embroidery-abaya": ("Embroidery Abaya", 990),
    "handcrafted-cutdana-abaya": ("Handcrafted Cutdana Abaya", 990),
    "stonework-embroidery-abaya": ("Stonework Embroidery Abaya", 990),
}
KIDS = "kids"
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
    # Which weights each product would get, from every collection it is in
    products, wanted = {}, {}
    for handle, (name, weight) in WEIGHTS.items():
        count = 0
        for product in collection_products(shopify, handle):
            products[product["id"]] = product
            wanted.setdefault(product["id"], {})[name] = weight
            count += 1
        log(f"{name}: {count} products")
    kids = {p["id"] for p in collection_products(shopify, KIDS)} if KIDS else set()

    rows, counts = [], {"changed": 0, "already": 0, "conflict": 0, "kids": 0}
    for product_id, by_collection in wanted.items():
        product = products[product_id]
        link = admin_link(shopify, product_id)
        collections = ", ".join(by_collection)
        weights = set(by_collection.values())
        reason = ""
        if len(weights) > 1:
            reason = "Conflict: in collections with different weights (" + ", ".join(f"{n} {w} g" for n, w in by_collection.items()) + ")"
            counts["conflict"] += 1
        elif product_id in kids:
            reason = "Also in Kids collection: kids abayas are lighter, set by hand"
            counts["kids"] += 1
        if reason:
            for variant in product["variants"]["nodes"]:
                weight = ((variant.get("inventoryItem") or {}).get("measurement") or {}).get("weight")
                rows.append([product["title"], variant["title"], collections, product["status"], shown(weight), "", "Not changed", reason, link])
            continue

        target = weights.pop()
        to_change = []
        for variant in product["variants"]["nodes"]:
            weight = ((variant.get("inventoryItem") or {}).get("measurement") or {}).get("weight")
            if grams(weight) == target:
                rows.append([product["title"], variant["title"], collections, product["status"], shown(weight), f"{target} g", "Already right", "", link])
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
            rows.append([product["title"], variant["title"], collections, product["status"], shown(weight), f"{target} g", status, "", link])
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
        f"{counts['conflict']} conflicts, {counts['kids']} also in Kids (not changed). Report: {REPORT}")
    skipped = [r for r in rows if r[6] == "Not changed"]
    if skipped:
        log("Not changed (set these by hand if needed):")
        for title in dict.fromkeys(r[0] for r in skipped):
            reason = next(r[7] for r in skipped if r[0] == title)
            log(f"  - {title}: {reason}")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Set abaya shipping weights by collection")
    ap.add_argument("--apply", action="store_true", help="write the weights (without it: test only, changes nothing)")
    args = ap.parse_args()
    started = time.time()
    run(Shopify(), apply=args.apply)
    print(f"Finished in {int(time.time() - started)} s.")


if __name__ == "__main__":
    sys.exit(main())
