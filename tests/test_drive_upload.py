"""Offline tests for drive_upload.py: fake Shopify, fake Drive, fake image downloads."""
import csv
import io
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import drive_upload  # noqa: E402

PH = "gid://shopify/MediaImage/placeholder"


def jpeg(colour):
    out = io.BytesIO()
    Image.new("RGB", (60, 80), colour).save(out, "JPEG")
    return out.getvalue()


PHOTO, BLACK = jpeg((180, 150, 120)), jpeg((0, 0, 0))


def image(n):
    if n == "ph":  # the image program's placeholder
        return {"id": PH, "image": {"url": "https://cdn.shopify.com/files/lc-PLACEHOLDER-closer-3.jpg"}}
    return {"id": f"gid://shopify/MediaImage/{n}", "image": {"url": f"https://cdn.shopify.com/files/img{n}.jpg?v=1"}}


def product(pid, title, media=(), fitting=None, multi=(), closer=(), variants=(), status="ACTIVE"):
    return {
        "id": f"gid://shopify/Product/{pid}", "title": title, "handle": f"p{pid}", "status": status,
        "media": {"nodes": [image(n) for n in media] + [{}]},  # {} = a video (not an image)
        "variants": {"nodes": [{"media": {"nodes": [image(n)]}} for n in variants] + [{"media": {"nodes": []}}]},
        "fitting": {"reference": image(fitting)} if fitting else None,
        "multi": {"references": {"nodes": [image(n) for n in multi]}} if multi else None,
        "closer": {"references": {"nodes": [image(n) for n in closer]}} if closer else None,
    }


class FakeShopify:
    def __init__(self, by_collection):
        self.by_collection = by_collection

    domain = "0anyrv-ej.myshopify.com"

    def get_shop_json(self, key, default=None):
        return {"closer-3": PH}

    def gql(self, query, variables):
        nodes = self.by_collection.get(variables["handle"], [])
        return {"collectionByIdentifier": {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}


class FakeDrive:
    def __init__(self, missing=()):
        self.folders = {name: f"type-{name}" for name in drive_upload.FOLDERS.values() if name not in missing}
        self.products = {}  # (parent, product id) -> folder id
        self.files = {}  # folder id -> [(name, media id)]
        self.uploads = 0

    def folder_named(self, parent_id, name):
        assert parent_id == drive_upload.PARENT_FOLDER_ID
        return self.folders.get(name)

    def product_folder(self, parent_id, name, product_id, create=True):
        key = (parent_id, product_id)
        if key not in self.products:
            if not create:
                return None
            self.products[key] = f"{parent_id}/{name}"
        return self.products[key]

    def uploaded_media(self, folder_id):
        return {media for _, media in self.files.get(folder_id, [])}

    def upload(self, folder_id, name, data, mime, media_id):
        assert data == PHOTO and mime == "image/jpeg"
        self.files.setdefault(folder_id, []).append((name, media_id))
        self.uploads += 1
        return f"file{self.uploads}"


class FakeHttp:
    black = {"img66.jpg"}  # a black image put in by hand
    calls = 0

    def get(self, url, timeout=None):
        FakeHttp.calls += 1
        content = BLACK if any(b in url for b in self.black) or "PLACEHOLDER" in url else PHOTO

        class R:
            def raise_for_status(self):
                pass
        R.content = content
        return R()


def quiet(*args):
    pass


LAST_REPORT = []


def run(shop, drive, **kw):
    with tempfile.TemporaryDirectory() as tmp:
        drive_upload.REPORT = Path(tmp) / "report.csv"
        rows = drive_upload.run(shop, drive, http=FakeHttp(), log=quiet, **kw)
        LAST_REPORT[:] = list(csv.reader(open(drive_upload.REPORT, encoding="utf-8-sig")))
        return rows


def test_all_images_in_page_order_named_after_the_product():
    p = product(1, "Floral Abaya | Premium Nida", media=(1, 2), variants=(2, 8), fitting=3, multi=(4, 2), closer=(5,))
    drive = FakeDrive()
    run(FakeShopify({"printed-abaya": [p]}), drive)
    files = drive.files["type-Printed Abaya/Floral Abaya - Premium Nida"]
    # gallery, colour images, fitting, multi display, close-ups; image 2 only once; the video left out
    assert [name for name, _ in files] == [f"Floral Abaya - Premium Nida {i}.jpg" for i in range(1, 7)]
    assert [m.rsplit("/", 1)[-1] for _, m in files] == ["1", "2", "8", "3", "4", "5"]
    assert LAST_REPORT[1][3] == "Uploaded" and LAST_REPORT[1][2].endswith("/store/0anyrv-ej/products/1")


def test_product_with_a_placeholder_is_skipped_everywhere_and_reported():
    bad = product(1, "Half Done Abaya", media=(1, 2), closer=(5, "ph"))
    good = product(2, "Done Abaya", media=(3,))
    drive = FakeDrive()
    run(FakeShopify({"plain-abaya": [good, bad], "kids": [bad]}), drive)
    assert set(drive.files) == {"type-Plain Abaya/Done Abaya"}  # nothing of the half-done product, in either folder
    skipped = [row for row in LAST_REPORT[1:] if row[3] == "Skipped - black image"]
    assert [row[0] for row in skipped] == ["Plain Abaya", "Kids Abaya"] and skipped[0][1] == "Half Done Abaya"
    assert skipped[0][6] == "Black image in A little closer (image 4)"
    assert LAST_REPORT[1][3].startswith("Skipped")  # skipped products listed first


def test_an_all_black_image_put_in_by_hand_also_skips_the_product():
    drive = FakeDrive()
    run(FakeShopify({"plain-abaya": [product(1, "Odd Abaya", media=(1, 66))]}), drive)
    assert drive.uploads == 0 and LAST_REPORT[1][6] == "Black image in Media (image 2)"


def test_once_the_black_image_is_replaced_the_product_is_uploaded():
    p = product(1, "Half Done Abaya", media=(1,), closer=("ph",))
    drive, shop = FakeDrive(), FakeShopify({"plain-abaya": [p]})
    run(shop, drive)
    assert drive.uploads == 0
    p["closer"]["references"]["nodes"] = [image(7)]
    run(shop, drive)
    assert drive.uploads == 2


def test_product_in_two_collections_goes_in_both_folders():
    p = product(7, "Kids Stone Abaya", media=(1,))
    drive = FakeDrive()
    run(FakeShopify({"kids": [p], "stonework-abaya": [p]}), drive)
    assert set(drive.files) == {"type-Stone Work Abaya/Kids Stone Abaya", "type-Kids Abaya/Kids Stone Abaya"}


def test_second_run_uploads_only_what_is_new():
    p = product(1, "Plain Abaya", media=(1, 2))
    drive, shop = FakeDrive(), FakeShopify({"plain-abaya": [p]})
    run(shop, drive)
    assert drive.uploads == 2
    run(shop, drive)
    assert drive.uploads == 2  # nothing uploaded twice
    p["media"]["nodes"].append(image(9))
    run(shop, drive)
    assert drive.uploads == 3 and drive.files["type-Plain Abaya/Plain Abaya"][-1] == ("Plain Abaya 3.jpg", "gid://shopify/MediaImage/9")


def test_dry_run_uploads_and_creates_nothing():
    drive = FakeDrive()
    rows = run(FakeShopify({"plain-abaya": [product(1, "Plain Abaya", media=(1, 2))]}), drive, dry_run=True)
    assert drive.uploads == 0 and drive.products == {} and rows[0][3] == "Would upload" and rows[0][4] == 2


def test_limit_counts_products_not_folders_and_drafts_are_skipped():
    a, b, c = product(1, "A", media=(1,)), product(2, "B", media=(2,)), product(3, "C", media=(3,), status="DRAFT")
    drive = FakeDrive()
    run(FakeShopify({"plain-abaya": [c, a, b], "kids": [a]}), drive, limit=1)
    assert set(drive.files) == {"type-Plain Abaya/A", "type-Kids Abaya/A"}


def test_missing_type_folder_stops_before_uploading():
    drive = FakeDrive(missing=("Hand Work Abaya",))
    try:
        run(FakeShopify({"plain-abaya": [product(1, "A", media=(1,))]}), drive)
    except RuntimeError as error:
        assert "Hand Work Abaya" in str(error)
    else:
        raise AssertionError("expected a missing-folder error")
    assert drive.uploads == 0


def test_safe_names():
    assert drive_upload.safe_name('Abaya | Premium: "Nida"/Black?') == "Abaya - Premium NidaBlack"
    assert drive_upload.safe_name("  Abaya.  ") == "Abaya"


def test_drive_client_only_lists_and_creates():
    from lc_images.gdrive import Drive

    class Resp:
        def __init__(self, data, code=200):
            self.data, self.status_code, self.content, self.text = data, code, b"x", ""

        def json(self):
            return self.data

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, data=None, timeout=None):
            return Resp({"access_token": "t", "expires_in": 3600})

        def request(self, method, url, headers=None, timeout=None, params=None, json=None, data=None):
            self.calls.append((method, url, params, json, data, headers))
            if method == "GET":
                return Resp({"files": []})
            return Resp({"id": "new"})

    s = Session()
    d = Drive("id", "secret", "refresh", session=s)
    assert d.folder_named("parent", "Kid's Abaya") is None
    assert "name = 'Kid\\'s Abaya'" in s.calls[0][2]["q"]  # quotes escaped in the search
    assert d.product_folder("type", "A", "42") == "new"
    assert s.calls[-1][3]["appProperties"] == {"lc_product": "42"}
    assert d.upload("folder", "A 1.jpg", b"img", "image/jpeg", "gid://m/1") == "new"
    assert b'"lc_media": "gid://m/1"' in s.calls[-1][4] and s.calls[-1][4].count(b"img") == 1
    assert {c[0] for c in s.calls} <= {"GET", "POST"}  # never PATCH or DELETE


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
