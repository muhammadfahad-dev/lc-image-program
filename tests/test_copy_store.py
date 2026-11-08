"""Offline tests for copy_store.py: a fake International store and a fake LCM Pak store."""
import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import copy_store  # noqa: E402

copy_store.time.sleep = lambda s: None


def page(nodes):
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


class FakeShop:
    def __init__(self, name, files=(), products=(), collections=(), pages=(), menus=(), definitions=(), redirects=()):
        self.name = name
        self.files_ = list(files)
        self.products = list(products)
        self.collections_ = list(collections)
        self.pages_ = list(pages)
        self.menus_ = list(menus)
        self.definitions = list(definitions)
        self.redirects = list(redirects)
        self.writes = []

    def gql(self, query, variables=None):
        v = variables or {}
        q = " ".join(query.split())
        if q.startswith("mutation"):
            name = q.split("{", 1)[1].split("(", 1)[0].strip()
            self.writes.append((name, v))
            if name == "fileCreate":
                bad = [f for f in v["f"] if f.get("filename", "").endswith(".png") and "jpg" in f["originalSource"]]
                if bad:  # Shopify refuses the whole group when one name's extension differs from the original
                    return {name: {"files": [], "userErrors": [{"field": ["files"], "message": "Provided filename extension must match original source."}]}}
                for f in v["f"]:
                    self.files_.append({"id": f"gid://shopify/MediaImage/T{len(self.files_)}", "alt": f["alt"], "image": {"url": f"https://cdn/{f.get('filename') or 'shopify-named.jpg'}?v=2"}})
                return {name: {"files": [], "userErrors": []}}
            if name == "collectionCreate":
                return {name: {"collection": {"id": "gid://shopify/Collection/T1"}, "userErrors": []}}
            return {name: {"userErrors": []}}
        if "files(" in q:
            return {"files": page(self.files_)}
        if "metaobjectDefinitions(" in q:
            return {"metaobjectDefinitions": page([])}
        if "metafieldDefinitions(" in q:
            return {"metafieldDefinitions": page([d for d in self.definitions if d["owner"] == v.get("owner")])}
        if "publications(" in q:
            return {"publications": {"nodes": [{"id": "gid://shopify/Publication/1", "name": "Online Store"}]}}
        if "collection(id:" in q:
            c = next(c for c in self.collections_ if c["id"] == v["id"])
            return {"collection": {"products": page([{"handle": h} for h in c["product_handles"]])}}
        if "collections(" in q:
            return {"collections": page(self.collections_)}
        if "products(" in q:
            return {"products": page(self.products)}
        if "pages(" in q:
            return {"pages": page(self.pages_)}
        if "blogs(" in q:
            return {"blogs": page([])}
        if "menus(" in q:
            return {"menus": page(self.menus_)}
        if "urlRedirects(" in q:
            return {"urlRedirects": page(self.redirects)}
        raise AssertionError(f"unexpected query {q[:80]}")


def image(fid, name):
    return {"id": fid, "alt": "", "image": {"url": f"https://cdn.shopify.com/s/files/1/{name}?v=1"}}


def stores():
    source = FakeShop(
        "source",
        files=[image("gid://shopify/MediaImage/1", "fit-1.jpg"), image("gid://shopify/MediaImage/2", "closer-1.jpg"),
               image("gid://shopify/MediaImage/3", "hero banner.png")],
        products=[{"id": "gid://shopify/Product/1", "handle": "black-abaya", "metafields": {"nodes": [
            {"namespace": "custom", "key": "fitting_image", "type": "file_reference", "value": "gid://shopify/MediaImage/1"},
            {"namespace": "custom", "key": "closer_images", "type": "list.file_reference", "value": json.dumps(["gid://shopify/MediaImage/2"])},
            {"namespace": "shopify", "key": "color-pattern", "type": "list.metaobject_reference", "value": "[]"}]}}],
        collections=[{"id": "gid://shopify/Collection/1", "handle": "plain-abaya", "title": "Plain Abaya", "descriptionHtml": "", "sortOrder": "MANUAL",
                      "templateSuffix": None, "seo": {"title": None, "description": None}, "image": None, "ruleSet": None,
                      "metafields": {"nodes": []}, "product_handles": ["black-abaya", "not-imported"]}],
        pages=[{"id": "gid://shopify/Page/1", "handle": "about-us", "title": "About", "body": "<p>x</p>", "isPublished": True, "templateSuffix": "about-us"}],
        menus=[{"id": "gid://shopify/Menu/1", "handle": "main-menu", "title": "Main", "items": [
            {"title": "Plain", "type": "COLLECTION", "url": "/collections/plain-abaya", "resourceId": "gid://shopify/Collection/1", "tags": [], "items": []},
            {"title": "About", "type": "PAGE", "url": "/pages/about-us", "resourceId": "gid://shopify/Page/1", "tags": [], "items": []}]}],
        definitions=[{"owner": "PRODUCT", "name": "Fitting", "namespace": "custom", "key": "fitting_image", "description": "",
                      "type": {"name": "file_reference"}, "validations": []}],
        redirects=[{"path": "/old", "target": "/new"}],
    )
    target = FakeShop(
        "target",
        files=[image("gid://shopify/MediaImage/T0", "hero banner.png")],
        products=[{"id": "gid://shopify/Product/T1", "handle": "black-abaya", "metafields": {"nodes": []}}],
        pages=[], menus=[{"id": "gid://shopify/Menu/T1", "handle": "main-menu", "title": "Main menu", "items": []}],
    )
    return source, target


def run(source, target, step, apply):
    with tempfile.TemporaryDirectory() as tmp:
        copy_store.REPORT = Path(tmp) / "r.csv"
        copy_store.Copier(source, target, apply=apply, log=lambda *a: None).run(step)
        return list(csv.reader(open(copy_store.REPORT, encoding="utf-8-sig")))


def test_test_mode_writes_nothing():
    source, target = stores()
    rows = run(source, target, "setup", apply=False)
    run(source, target, "content", apply=False)
    assert target.writes == [] and source.writes == []
    assert ["File", "fit-1.jpg", "Would copy", ""] in rows and not any(r[1] == "hero banner.png" for r in rows)  # already there


def test_setup_copies_missing_files_with_their_names_and_definitions():
    source, target = stores()
    run(source, target, "setup", apply=True)
    files = [w for w in target.writes if w[0] == "fileCreate"][0][1]["f"]
    assert sorted(f["filename"] for f in files) == ["closer-1.jpg", "fit-1.jpg"]
    assert files[0]["originalSource"].startswith("https://cdn.shopify.com/")
    defs = [w for w in target.writes if w[0] == "metafieldDefinitionCreate"]
    assert defs[0][1]["d"]["key"] == "fitting_image" and defs[0][1]["d"]["ownerType"] == "PRODUCT"
    assert source.writes == []  # the International store is only read


def test_content_points_metafields_collections_and_menus_at_lcm_pak():
    source, target = stores()
    run(source, target, "setup", apply=True)
    run(source, target, "content", apply=True)
    sets = [w for w in target.writes if w[0] == "metafieldsSet"][0][1]["m"]
    by_key = {m["key"]: m for m in sets}
    assert set(by_key) == {"fitting_image", "closer_images"}  # Shopify's own field left out
    assert by_key["fitting_image"]["ownerId"] == "gid://shopify/Product/T1"
    new_ids = {f["id"] for f in target.files_}
    assert by_key["fitting_image"]["value"] in new_ids and json.loads(by_key["closer_images"]["value"])[0] in new_ids
    collection = [w for w in target.writes if w[0] == "collectionCreate"][0][1]["c"]
    assert collection["handle"] == "plain-abaya" and collection["products"] == ["gid://shopify/Product/T1"]
    assert any(w[0] == "publishablePublish" for w in target.writes)
    page_made = [w for w in target.writes if w[0] == "pageCreate"][0][1]["p"]
    assert page_made["handle"] == "about-us" and page_made["templateSuffix"] == "about-us"
    menu = [w for w in target.writes if w[0] == "menuUpdate"][0][1]
    assert menu["id"] == "gid://shopify/Menu/T1"
    # collection created in this run, page created in this run: the fake store does not list them, so they become links
    assert [i["type"] for i in menu["i"]] == ["HTTP", "HTTP"] and menu["i"][0]["url"] == "/collections/plain-abaya"
    assert [w for w in target.writes if w[0] == "urlRedirectCreate"][0][1]["r"] == {"path": "/old", "target": "/new"}


def test_a_bad_file_does_not_stop_its_group():
    source, target = stores()
    source.files_.append({"id": "gid://shopify/MediaImage/9", "alt": "", "image": {"url": "https://cdn.shopify.com/s/files/1/odd.png?v=1"}})
    source.files_[-1]["image"]["url"] = "https://cdn.shopify.com/s/files/1/odd.png?v=1&src=jpg"
    rows = run(source, target, "setup", apply=True)
    results = {r[1]: r[2] for r in rows if r[0] == "File"}
    assert results == {"fit-1.jpg": "Copied", "closer-1.jpg": "Copied", "odd.png": "Copied"}
    assert any("filename" not in f for w in target.writes if w[0] == "fileCreate" for f in w[1]["f"])


def test_refuses_to_copy_into_the_international_store():
    copy_store.TARGET_STORE, copy_store.TARGET_CLIENT_ID, copy_store.TARGET_CLIENT_SECRET = copy_store.config.SHOP_DOMAIN, "x", "y"
    sys.argv = ["copy_store.py", "--step", "setup"]
    try:
        copy_store.main()
    except SystemExit as stop:
        assert "International store itself" in str(stop)
    else:
        raise AssertionError("expected a refusal")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
