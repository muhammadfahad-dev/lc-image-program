"""Offline tests for set_weights.py with a fake Shopify."""
import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import set_weights  # noqa: E402


def product(pid, title, weights, unit="KILOGRAMS"):
    return {
        "id": f"gid://shopify/Product/{pid}", "title": title, "handle": f"p{pid}", "status": "ACTIVE",
        "variants": {"nodes": [
            {"id": f"gid://shopify/ProductVariant/{pid}{i}", "title": f"Size {i}",
             "inventoryItem": {"measurement": {"weight": {"unit": unit, "value": w}}}}
            for i, w in enumerate(weights)
        ]},
    }


class FakeShopify:
    domain = "0anyrv-ej.myshopify.com"

    def __init__(self, by_collection):
        self.by_collection = by_collection
        self.updates = []

    def gql(self, query, variables):
        if "productVariantsBulkUpdate" in query:
            self.updates.append(variables)
            return {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}
        nodes = self.by_collection.get(variables["handle"], [])
        return {"collectionByIdentifier": {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}


def run(shop, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        set_weights.REPORT = Path(tmp) / "r.csv"
        set_weights.run(shop, log=lambda *a: None, **kw)
        return list(csv.reader(open(set_weights.REPORT, encoding="utf-8-sig")))


def test_weights_by_collection_only_with_apply():
    plain, stone = product(1, "Plain A", [0.5, 0.5]), product(2, "Stone A", [0.5])
    shop = FakeShopify({"plain-abaya": [plain], "stonework-abaya": [stone]})
    rows = run(shop)
    assert shop.updates == [] and {r[6] for r in rows[1:]} == {"Would change"}
    run(shop, apply=True)
    by_product = {u["productId"]: u["variants"] for u in shop.updates}
    assert [v["inventoryItem"]["measurement"]["weight"] for v in by_product["gid://shopify/Product/1"]] == [{"unit": "GRAMS", "value": 700}] * 2
    assert by_product["gid://shopify/Product/2"][0]["inventoryItem"]["measurement"]["weight"]["value"] == 990


def test_same_weight_twice_is_fine_but_different_weights_is_a_conflict():
    both_990 = product(1, "Stone Embroidery", [0.5])
    clash = product(2, "Plain Stone", [0.5])
    shop = FakeShopify({"stonework-abaya": [both_990, clash], "embroidery-abaya": [both_990], "plain-abaya": [clash]})
    rows = run(shop, apply=True)
    assert [u["productId"] for u in shop.updates] == ["gid://shopify/Product/1"]
    assert rows[1][0] == "Plain Stone" and rows[1][6] == "Not changed" and "Conflict" in rows[1][7]


def test_kids_products_are_left_alone_and_right_weights_are_skipped():
    kid, done = product(1, "Kids Stone", [0.5]), product(2, "Done", [700], unit="GRAMS")
    shop = FakeShopify({"stonework-abaya": [kid], "plain-abaya": [done], "kids": [kid]})
    rows = run(shop, apply=True)
    assert shop.updates == []
    assert rows[1][6] == "Not changed" and "Kids" in rows[1][7] and rows[2][6] == "Already right"
    assert rows[1][4] == "0.5 kg"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
