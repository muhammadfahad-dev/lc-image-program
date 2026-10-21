"""Report rows from fake products (no Shopify or Google needed)."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lc_images import report

PH = "gid://shopify/MediaImage/ph"
CHOICE = {"front_open": False, "has_design_work": False}


def product(audit=None, closer=None, texts=True, variants_with_image=("Black",), template="npdp", ptype="Plain Abaya"):
    variants = []
    for colour in ("Black", "White", "Black-Pink"):
        for size in ("S", "M"):
            media = [{"id": "m"}] if colour in variants_with_image else []
            variants.append({"selectedOptions": [{"name": "Color", "value": colour}, {"name": "Size", "value": size}], "media": {"nodes": media}})
    p = {
        "id": "gid://shopify/Product/42", "title": "Test Abaya", "handle": "test-abaya", "productType": ptype, "templateSuffix": template,
        "onlineStoreUrl": None, "options": [{"name": "Color", "values": ["Black", "White", "Black-Pink"]}], "variants": {"nodes": variants},
        "audit": {"value": json.dumps(audit)} if audit else None, "closer": {"value": json.dumps(closer)} if closer else None,
        "fitting": None, "multi": None, "listing": None,
    }
    if texts:
        p.update({
            "t_speciality": {"value": "x"}, "t_fabric_construction": {"value": "x"}, "t_closer_description": {"value": "a\nb"},
            "t_fitting_titles": {"value": json.dumps(list("abcde"))}, "t_fitting_subtexts": {"value": json.dumps(list("abcde"))},
            "t_fitting_extra_texts": {"value": json.dumps(list("abcd"))},
        })
    return p


def cell(row, name):
    return row[report.HEADER.index(name)]


def test_not_started_product():
    row = report.row_for(product(texts=False), set(), "shop")
    assert cell(row, "Media images (1.1-1.4)") == "Not started" and cell(row, "A little closer images") == "Not started"
    assert cell(row, "Template") == "npdp" and cell(row, "Text content") == "Retry"
    assert cell(row, "Speciality") == "Retry (empty)"
    assert "admin.shopify.com/store/shop/products/42" in cell(row, "Admin link")
    assert "wearlcm.com/products/test-abaya" in cell(row, "Store link")


def test_placeholders_and_variants_and_texts():
    audit = {"status": "manual", "choice": CHOICE, "slots": {
        "hero-1": {"status": "done"}, "hero-2": {"status": "done"}, "hero-4": {"status": "done"},
        "closer-1": {"status": "done"}, "closer-2": {"status": "manual", "placeholder": PH}, "closer-3": {"status": "done"}, "closer-4": {"status": "done"}}}
    row = report.row_for(product(audit, closer=["a", PH, "c", "d"]), {PH}, "shop")
    assert cell(row, "Media images (1.1-1.4)") == "Done"
    assert cell(row, "A little closer images") == "Retry (1 of 4: closer-2)"
    assert cell(row, "Variant images") == "Not completed (1 of 3): White, Black-Pink (mixed, by hand)"
    assert cell(row, "Fitting extra texts") == "Retry (4 of 5)" and cell(row, "Fitting titles") == "Done"
    todo = cell(row, "To do")
    assert "A little closer images: 1 of 4: closer-2" in todo and "Variant images: White" in todo and "Text: Fitting extra texts" in todo


def test_done_section_and_batch_in_progress():
    audit = {"status": "in_progress", "choice": CHOICE, "slots": {"multi-1": {"status": "queued"}}}
    row = report.row_for(product(audit, closer=["a", "b", "c", "d"]), set(), "shop")
    assert cell(row, "A little closer images") == "Done" and cell(row, "Multi display images") == "In progress"


def test_csv_and_sheet_calls():
    rows = [report.row_for(product(), set(), "shop")]
    with tempfile.TemporaryDirectory() as tmp:
        report.write_csv(rows, Path(tmp) / "r.csv")
        text = (Path(tmp) / "r.csv").read_text(encoding="utf-8-sig")
        assert text.startswith("Product,Admin link") and "https://admin.shopify.com/store/shop/products/42" in text and "HYPERLINK" not in text

    class Resp:
        def __init__(self, data, code=200):
            self.data, self.status_code, self.content, self.text = data, code, b"x", ""

        def json(self):
            return self.data

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, params=None):
            self.calls.append(("get", url))
            return Resp({"sheets": [{"properties": {"title": "Sheet1", "sheetId": 0}}]})

        def post(self, url, json=None, params=None):
            self.calls.append(("post", url, json))
            return Resp({"replies": [{"addSheet": {"properties": {"sheetId": 7}}}]})

        def put(self, url, params=None, json=None):
            self.calls.append(("put", url, json))
            return Resp({})

    session = Session()
    link = report.write_sheet(rows, "SHEET", "Image report", session)
    assert link.endswith("/SHEET")
    kinds = [c[0] for c in session.calls]
    assert kinds == ["get", "post", "post", "post", "put"]  # read tabs, add tab, colours, clear, write
    assert session.calls[-1][2]["values"][0] == report.HEADER


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
