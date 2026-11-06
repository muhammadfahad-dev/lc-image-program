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


def test_work_wins_over_plain_and_titles_count_as_work():
    plain_stone = product(1, "Plain Stone", [0.5])  # in Plain and Stonework collections
    titled = product(2, "Printed Abaya with Embroidered Cuffs", [0.5])  # only in Printed, but has embroidery
    printed = product(3, "Floral Printed Abaya", [0.5])
    shop = FakeShopify({"stonework-abaya": [plain_stone], "plain-abaya": [plain_stone], "printed-abaya": [titled, printed]})
    rows = run(shop, apply=True)
    grams = {u["productId"][-1]: u["variants"][0]["inventoryItem"]["measurement"]["weight"]["value"] for u in shop.updates}
    assert grams == {"1": 990, "2": 990, "3": 700}
    why = {r[0]: r[7] for r in rows[1:]}
    assert why["Plain Stone"] == "Work: in Stonework Abaya" and why["Printed Abaya with Embroidered Cuffs"] == "Work: title mentions embroider"


def test_kids_abayas_by_work_other_kids_products_alone_and_right_weights_skipped():
    kid_stone = product(1, "Kids Stone Abaya", [0.5])  # in Stonework and Kids
    kid_printed = product(4, "Kids Printed Abaya with Belt", [0.5])  # Kids only, no work
    kid_embroidered = product(5, "Kids Bow Embroidered Abaya", [0.5])  # Kids only, work in the title
    prayer = product(6, "Kids One-Piece Printed Prayer Dress", [0.5])
    done = product(2, "Done", [700], unit="GRAMS")
    shop = FakeShopify({"stonework-abaya": [kid_stone], "plain-abaya": [done], "kids": [kid_stone, kid_printed, kid_embroidered, prayer]})
    rows = run(shop, apply=True)
    grams = {u["productId"][-1]: u["variants"][0]["inventoryItem"]["measurement"]["weight"]["value"] for u in shop.updates}
    assert grams == {"1": 990, "4": 700, "5": 990}
    assert rows[1][0] == "Kids One-Piece Printed Prayer Dress" and rows[1][6] == "Not changed" and rows[1][4] == "0.5 kg"
    assert [r[6] for r in rows if r[0] == "Done"] == ["Already right"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
