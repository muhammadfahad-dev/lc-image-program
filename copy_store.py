"""Copy the International store's content into the Pakistan store (LCM Pak), keeping the same handles and file names
so the copied theme finds everything.

Products themselves are copied with Shopify's own product CSV (Products > Export / Import), so the client can set PKR
prices in the file. This program copies what that file does not carry, in two steps:

    python copy_store.py --step setup            (test: lists what would be copied, copies nothing)
    python copy_store.py --step setup --apply    before the product CSV is imported:
                                                 metafield definitions, metaobject definitions and entries, all files
    python copy_store.py --step content --apply  after the product CSV is imported:
                                                 product metafields (fitting, multi display, close-up images...),
                                                 collections, pages, blogs and articles, menus, URL redirects

It only adds to the Pakistan store and never changes or deletes anything in the International store (which is only
read). Something that already exists in the Pakistan store (same handle, same file name) is left as it is, so it is safe
to run again. Each run writes output/copy_report.csv.
"""
import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from lc_images import config
from lc_images.shopify import Shopify

REPORT = Path("output/copy_report.csv")
TARGET_STORE = config.env("TARGET_SHOPIFY_STORE")
TARGET_CLIENT_ID = config.env("TARGET_SHOPIFY_CLIENT_ID")
TARGET_CLIENT_SECRET = config.env("TARGET_SHOPIFY_CLIENT_SECRET")

METAFIELD_OWNERS = ("PRODUCT", "PRODUCTVARIANT", "COLLECTION", "PAGE", "ARTICLE", "BLOG")
SKIP_NAMESPACE = re.compile(r"^(shopify|app--|\$app|reviews$)")  # Shopify's own and app-owned fields


def client_token(domain, client_id, client_secret):
    r = requests.post(f"https://{domain}/admin/oauth/access_token", timeout=60, data={
        "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    if r.status_code != 200 or "access_token" not in r.text:
        raise RuntimeError(f"The Pakistan store refused the app's client ID and secret ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


def paged(shop, query, path, variables=None, size=100):
    """Every node of a connection, following pages. path: the connection's field name in the query result."""
    after = None
    while True:
        data = shop.gql(query, {**(variables or {}), "first": size, "after": after})
        conn = data
        for key in path.split("."):
            conn = conn[key]
        yield from conn["nodes"]
        if not conn["pageInfo"]["hasNextPage"]:
            return
        after = conn["pageInfo"]["endCursor"]


def check(result, name):
    errors = result.get("userErrors") or []
    if errors:
        raise RuntimeError(f"{name}: " + "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errors))
    return result


def file_name(url):
    return unquote(urlparse(url or "").path.rsplit("/", 1)[-1])


class Copier:
    def __init__(self, source, target, apply=False, log=print):
        self.source, self.target, self.apply, self.log = source, target, apply, log
        self.rows = []
        self._maps = {}

    def note(self, kind, name, result, detail=""):
        self.rows.append([kind, name, result, detail])

    # ---------- lookups: same handle / same file name in both stores ----------
    def handles(self, shop, kind):
        """{id: handle} for products, collections, pages, blogs, metaobjects (by type/handle)."""
        key = (id(shop), kind)
        if key in self._maps:
            return self._maps[key]
        if kind == "metaobject":
            found = {}
            for definition in paged(shop, "query($first: Int!, $after: String) { metaobjectDefinitions(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { type } } }", "metaobjectDefinitions"):
                for node in paged(shop, "query($type: String!, $first: Int!, $after: String) { metaobjects(type: $type, first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { id handle } } }", "metaobjects", {"type": definition["type"]}):
                    found[node["id"]] = f"{definition['type']}/{node['handle']}"
        else:
            field = {"product": "products", "collection": "collections", "page": "pages", "blog": "blogs"}[kind]
            found = {n["id"]: n["handle"] for n in paged(shop, f"query($first: Int!, $after: String) {{ {field}(first: $first, after: $after) {{ pageInfo {{ hasNextPage endCursor }} nodes {{ id handle }} }} }}", field, size=250)}
        self._maps[key] = found
        return found

    def same(self, kind, source_id):
        """The Pakistan store's id for a source id, matched by handle (or file name for files)."""
        if not source_id:
            return None
        if kind == "file":
            name = self.files(self.source).get(source_id)
            return self.reverse(self.target, "file", self.files(self.target)).get(name) if name else None
        handle = self.handles(self.source, kind).get(source_id)
        if not handle:
            return None
        return self.reverse(self.target, kind, self.handles(self.target, kind)).get(handle)

    def reverse(self, shop, kind, forward):
        """{handle or file name: id}, kept until the forward map is refreshed"""
        key = (id(shop), kind, "reverse", id(forward))
        if key not in self._maps:
            self._maps[key] = {v: k for k, v in forward.items()}
        return self._maps[key]

    def files(self, shop, refresh=False):
        """{file id: file name} of every file in Content > Files."""
        key = (id(shop), "file")
        if key in self._maps and not refresh:
            return self._maps[key]
        query = """query($first: Int!, $after: String) { files(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes {
          id alt
          ... on MediaImage { image { url } }
          ... on Video { filename originalSource { url } sources { url mimeType height } }
          ... on GenericFile { url } } } }"""
        found, self._sources = {}, getattr(self, "_sources", {})
        for node in paged(shop, query, "files", size=100):
            if node.get("filename"):
                # Shopify downloads a video from a plain MP4 address: the largest MP4 rendition
                mp4 = sorted((v for v in node.get("sources") or [] if "mp4" in (v.get("mimeType") or "")), key=lambda v: v.get("height") or 0)
                url = mp4[-1]["url"] if mp4 else (node.get("originalSource") or {}).get("url")
                name, kind = node["filename"], "VIDEO"
            elif node.get("image"):
                url = node["image"]["url"]
                name, kind = file_name(url), "IMAGE"
            elif node.get("url"):
                url = node["url"]
                name, kind = file_name(url), "FILE"
            else:
                continue
            found[node["id"]] = name
            if shop is self.source:
                self._sources[node["id"]] = (url, kind, node.get("alt") or "")
        self._maps[key] = found
        return found

    # ---------- step 1: before the product CSV ----------
    def metafield_definitions(self):
        query = """query($owner: MetafieldOwnerType!, $first: Int!, $after: String) { metafieldDefinitions(ownerType: $owner, first: $first, after: $after) {
          pageInfo { hasNextPage endCursor } nodes { name namespace key description type { name } validations { name value } } } }"""
        for owner in METAFIELD_OWNERS:
            have = {(d["namespace"], d["key"]) for d in paged(self.target, query, "metafieldDefinitions", {"owner": owner})}
            for d in paged(self.source, query, "metafieldDefinitions", {"owner": owner}):
                name = f"{owner.lower()}.{d['namespace']}.{d['key']}"
                if SKIP_NAMESPACE.match(d["namespace"]):
                    continue
                if (d["namespace"], d["key"]) in have:
                    self.note("Metafield definition", name, "Already there")
                    continue
                validations = [{"name": v["name"], "value": self.remap_validation(v["value"])} for v in d["validations"]]
                if self.apply:
                    check(self.target.gql("mutation($d: MetafieldDefinitionInput!) { metafieldDefinitionCreate(definition: $d) { createdDefinition { id } userErrors { field message } } }", {"d": {
                        "name": d["name"], "namespace": d["namespace"], "key": d["key"], "description": d["description"],
                        "type": d["type"]["name"], "ownerType": owner, "validations": validations}})["metafieldDefinitionCreate"], name)
                self.note("Metafield definition", name, "Created" if self.apply else "Would create")

    def remap_validation(self, value):
        """A validation pointing at a metaobject definition (by id) points at the same type in the Pakistan store."""
        if "MetaobjectDefinition/" not in str(value):
            return value
        source_types = {d["id"]: d["type"] for d in paged(self.source, "query($first: Int!, $after: String) { metaobjectDefinitions(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { id type } } }", "metaobjectDefinitions")}
        target_ids = {d["type"]: d["id"] for d in paged(self.target, "query($first: Int!, $after: String) { metaobjectDefinitions(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { id type } } }", "metaobjectDefinitions")}
        return re.sub(r"gid://shopify/MetaobjectDefinition/\d+", lambda m: target_ids.get(source_types.get(m.group(0), ""), m.group(0)), str(value))

    def metaobjects(self):
        query = """query($first: Int!, $after: String) { metaobjectDefinitions(first: $first, after: $after) { pageInfo { hasNextPage endCursor }
          nodes { type name description displayNameKey fieldDefinitions { key name description required type { name } validations { name value } } } } }"""
        have_types = {d["type"] for d in paged(self.target, "query($first: Int!, $after: String) { metaobjectDefinitions(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { type } } }", "metaobjectDefinitions")}
        definitions = list(paged(self.source, query, "metaobjectDefinitions"))
        for d in definitions:
            if d["type"].startswith(("shopify--", "app--", "$app")):
                continue
            if d["type"] in have_types:
                self.note("Metaobject definition", d["type"], "Already there")
            else:
                if self.apply:
                    check(self.target.gql("mutation($d: MetaobjectDefinitionCreateInput!) { metaobjectDefinitionCreate(definition: $d) { metaobjectDefinition { id } userErrors { field message } } }", {"d": {
                        "type": d["type"], "name": d["name"], "description": d["description"], "displayNameKey": d["displayNameKey"],
                        "fieldDefinitions": [{"key": f["key"], "name": f["name"], "description": f["description"], "required": f["required"],
                                              "type": f["type"]["name"], "validations": [{"name": v["name"], "value": self.remap_validation(v["value"])} for v in f["validations"]]}
                                             for f in d["fieldDefinitions"]]}})["metaobjectDefinitionCreate"], d["type"])
                self.note("Metaobject definition", d["type"], "Created" if self.apply else "Would create")
        self._maps.pop((id(self.target), "metaobject"), None)
        target_entries = set(self.handles(self.target, "metaobject").values())
        for d in definitions:
            if d["type"].startswith(("shopify--", "app--", "$app")):
                continue
            entries = paged(self.source, "query($type: String!, $first: Int!, $after: String) { metaobjects(type: $type, first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { handle fields { key type value } } } }", "metaobjects", {"type": d["type"]})
            for entry in entries:
                name = f"{d['type']}/{entry['handle']}"
                if name in target_entries:
                    self.note("Metaobject", name, "Already there")
                    continue
                fields = [{"key": f["key"], "value": self.remap_value(f["type"], f["value"])} for f in entry["fields"] if f["value"] is not None]
                if self.apply:
                    check(self.target.gql("mutation($m: MetaobjectCreateInput!) { metaobjectCreate(metaobject: $m) { metaobject { id } userErrors { field message } } }", {"m": {
                        "type": d["type"], "handle": entry["handle"], "fields": [f for f in fields if f["value"] is not None]}})["metaobjectCreate"], name)
                self.note("Metaobject", name, "Created" if self.apply else "Would create")

    def copy_files(self):
        source = self.files(self.source)
        have = set(self.files(self.target).values())
        todo = [(fid, name) for fid, name in source.items() if name not in have]
        self.log(f"Files: {len(source)} in the International store, {len(source) - len(todo)} already in LCM Pak, {len(todo)} to copy")
        batch = []
        for fid, name in todo:
            url, kind, alt = self._sources[fid]
            if not url:
                self.note("File", name, "Skipped", "no download address")
                continue
            batch.append(({"originalSource": url, "contentType": kind, "alt": alt[:500], "filename": name}, name))
            if len(batch) == 20:
                self._create_files(batch)
                batch = []
        if batch:
            self._create_files(batch)
        self._maps.pop((id(self.target), "file"), None)

    def _upload_video(self, item, name):
        """Shopify only takes a video as an uploaded file, not from a web address: download it, then upload it."""
        source = requests.get(item["originalSource"], timeout=600)
        source.raise_for_status()
        data = source.content
        target = check(self.target.gql(
            "mutation($i: [StagedUploadInput!]!) { stagedUploadsCreate(input: $i) { stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }",
            {"i": [{"resource": "VIDEO", "filename": name, "mimeType": "video/mp4", "httpMethod": "POST", "fileSize": str(len(data))}]},
        )["stagedUploadsCreate"], "stagedUploadsCreate")["stagedTargets"][0]
        form = {p["name"]: p["value"] for p in target["parameters"]}
        sent = requests.post(target["url"], data=form, files={"file": (name, data, "video/mp4")}, timeout=900)
        if sent.status_code not in (200, 201, 204):
            return [{"message": f"video upload refused ({sent.status_code}): {sent.text[:200]}"}]
        return self.target.gql("mutation($f: [FileCreateInput!]!) { fileCreate(files: $f) { files { id } userErrors { field message } } }",
                               {"f": [{**item, "originalSource": target["resourceUrl"]}]})["fileCreate"]["userErrors"]

    def _create_files(self, batch):
        if not self.apply:
            for _, name in batch:
                self.note("File", name, "Would copy")
            return
        videos = [(item, name) for item, name in batch if item["contentType"] == "VIDEO"]
        batch = [(item, name) for item, name in batch if item["contentType"] != "VIDEO"]
        for item, name in videos:
            try:
                errors = self._upload_video(item, name)
            except (requests.RequestException, RuntimeError) as error:
                errors = [{"message": str(error)[:300]}]
            self.note("File", name, "Failed" if errors else "Copied", "; ".join(e["message"] for e in errors))
        if not batch:
            return
        create = "mutation($f: [FileCreateInput!]!) { fileCreate(files: $f) { files { id } userErrors { field message } } }"
        data = self.target.gql(create, {"f": [item for item, _ in batch]})["fileCreate"]
        if not data["userErrors"]:
            for _, name in batch:
                self.note("File", name, "Copied")
            time.sleep(1)
            return
        # One bad file fails the whole group: copy them one by one
        for item, name in batch:
            errors = self.target.gql(create, {"f": [item]})["fileCreate"]["userErrors"]
            if errors and any("extension" in e["message"].lower() for e in errors):
                # Its name ends differently from the stored original: copy it under the name Shopify gives it
                plain = {k: v for k, v in item.items() if k != "filename"}
                errors = self.target.gql(create, {"f": [plain]})["fileCreate"]["userErrors"]
                if not errors:
                    self.note("File", name, "Copied", "under Shopify's own name (its extension differs from the original)")
                    continue
            message = "; ".join(e["message"] for e in errors)
            self.note("File", name, "Failed" if errors else "Copied", message)
            time.sleep(0.3)

    # ---------- step 2: after the product CSV ----------
    def remap_value(self, kind, value):
        """A metafield value with references to the International store, pointing at the same items in LCM Pak."""
        refs = {"file_reference": "file", "product_reference": "product", "collection_reference": "collection",
                "page_reference": "page", "metaobject_reference": "metaobject"}
        base = kind.replace("list.", "")
        if base not in refs or value in (None, ""):
            return value
        if kind.startswith("list."):
            mapped = [self.same(refs[base], v) for v in json.loads(value)]
            mapped = [m for m in mapped if m]
            return json.dumps(mapped) if mapped else None
        return self.same(refs[base], value)

    def product_metafields(self):
        query = """query($first: Int!, $after: String) { products(first: $first, after: $after) { pageInfo { hasNextPage endCursor }
          nodes { handle metafields(first: 40) { nodes { namespace key type value } } } } }"""
        target_ids = {v: k for k, v in self.handles(self.target, "product").items()}
        sets, missing = [], 0
        for product in paged(self.source, query, "products", size=10):
            target_id = target_ids.get(product["handle"])
            fields = [m for m in product["metafields"]["nodes"] if not SKIP_NAMESPACE.match(m["namespace"])]
            if not fields:
                continue
            if not target_id:
                missing += 1
                self.note("Product metafields", product["handle"], "Skipped", "product not in LCM Pak (import the product CSV first)")
                continue
            for m in fields:
                value = self.remap_value(m["type"], m["value"])
                if value is None:
                    self.note("Product metafield", f"{product['handle']} {m['namespace']}.{m['key']}", "Skipped", "the file or item it points to is not in LCM Pak")
                    continue
                sets.append({"ownerId": target_id, "namespace": m["namespace"], "key": m["key"], "type": m["type"], "value": value})
            self.note("Product metafields", product["handle"], "Set" if self.apply else "Would set", f"{len(fields)} fields")
        for i in range(0, len(sets), 25):
            if self.apply:
                check(self.target.gql("mutation($m: [MetafieldsSetInput!]!) { metafieldsSet(metafields: $m) { metafields { id } userErrors { field message } } }",
                                      {"m": sets[i:i + 25]})["metafieldsSet"], "metafieldsSet")
                time.sleep(0.5)
        if missing:
            self.log(f"{missing} products with metafields are not in LCM Pak yet: import the product CSV, then run this step again")

    def online_store(self):
        for node in self.target.gql("{ publications(first: 20) { nodes { id name } } }")["publications"]["nodes"]:
            if node["name"] == "Online Store":
                return node["id"]
        return None

    def collections(self):
        query = """query($first: Int!, $after: String) { collections(first: $first, after: $after) { pageInfo { hasNextPage endCursor }
          nodes { id handle title descriptionHtml sortOrder templateSuffix seo { title description } image { url altText }
            ruleSet { appliedDisjunctively rules { column relation condition conditionObjectId } }
            metafields(first: 30) { nodes { namespace key type value } } } } }"""
        have = set(self.handles(self.target, "collection").values())
        publication = self.online_store() if self.apply else None
        target_products = {v: k for k, v in self.handles(self.target, "product").items()}
        for c in paged(self.source, query, "collections", size=10):
            if c["handle"] in have or c["handle"] == "frontpage":
                self.note("Collection", c["handle"], "Already there")
                continue
            data = {"handle": c["handle"], "title": c["title"], "descriptionHtml": c["descriptionHtml"], "sortOrder": c["sortOrder"],
                    "templateSuffix": c["templateSuffix"], "seo": c["seo"]}
            if c["image"]:
                data["image"] = {"src": c["image"]["url"], "altText": c["image"]["altText"]}
            if c["ruleSet"]:
                rules = []
                for r in c["ruleSet"]["rules"]:
                    rule = {"column": r["column"], "relation": r["relation"], "condition": r["condition"]}
                    if r.get("conditionObjectId"):
                        rule["conditionObjectId"] = self.same_definition(r["conditionObjectId"])
                    rules.append(rule)
                data["ruleSet"] = {"appliedDisjunctively": c["ruleSet"]["appliedDisjunctively"], "rules": rules}
            else:
                handles = [p["handle"] for p in paged(self.source, "query($id: ID!, $first: Int!, $after: String) { collection(id: $id) { products(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { handle } } } }", "collection.products", {"id": c["id"]}, size=250)]
                data["products"] = [target_products[h] for h in handles if h in target_products]
            fields = []
            for m in c["metafields"]["nodes"]:
                value = None if SKIP_NAMESPACE.match(m["namespace"]) else self.remap_value(m["type"], m["value"])
                if value is not None:
                    fields.append({"namespace": m["namespace"], "key": m["key"], "type": m["type"], "value": value})
            if fields:
                data["metafields"] = fields
            if self.apply:
                made = check(self.target.gql("mutation($c: CollectionInput!) { collectionCreate(input: $c) { collection { id } userErrors { field message } } }", {"c": data})["collectionCreate"], c["handle"])
                if publication:
                    self.target.gql("mutation($id: ID!, $p: ID!) { publishablePublish(id: $id, input: [{publicationId: $p}]) { userErrors { field message } } }",
                                    {"id": made["collection"]["id"], "p": publication})
            kind = "smart" if c["ruleSet"] else f"{len(data.get('products', []))} products"
            self.note("Collection", c["handle"], "Created" if self.apply else "Would create", kind)
        self._maps.pop((id(self.target), "collection"), None)

    def same_definition(self, source_id):
        query = "query($id: ID!) { node(id: $id) { ... on MetafieldDefinition { namespace key ownerType } } }"
        node = self.source.gql(query, {"id": source_id})["node"] or {}
        if not node:
            return source_id
        found = self.target.gql("query($o: MetafieldOwnerType!, $n: String!, $k: String!) { metafieldDefinitions(ownerType: $o, namespace: $n, key: $k, first: 5) { nodes { id namespace key } } }",
                                {"o": node["ownerType"], "n": node["namespace"], "k": node["key"]})["metafieldDefinitions"]["nodes"]
        match = [d for d in found if d["namespace"] == node["namespace"] and d["key"] == node["key"]]
        return match[0]["id"] if match else source_id

    def pages(self):
        query = "query($first: Int!, $after: String) { pages(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { handle title body isPublished templateSuffix } } }"
        have = set(self.handles(self.target, "page").values())
        for p in paged(self.source, query, "pages", size=100):
            if p["handle"] in have:
                self.note("Page", p["handle"], "Already there")
                continue
            if self.apply:
                check(self.target.gql("mutation($p: PageCreateInput!) { pageCreate(page: $p) { page { id } userErrors { field message } } }", {"p": {
                    "handle": p["handle"], "title": p["title"], "body": p["body"], "isPublished": p["isPublished"], "templateSuffix": p["templateSuffix"]}})["pageCreate"], p["handle"])
            self.note("Page", p["handle"], "Created" if self.apply else "Would create")
        self._maps.pop((id(self.target), "page"), None)

    def blogs(self):
        have = {v: k for k, v in self.handles(self.target, "blog").items()}
        query = """query($first: Int!, $after: String) { blogs(first: $first, after: $after) { pageInfo { hasNextPage endCursor }
          nodes { id handle title templateSuffix } } }"""
        for b in paged(self.source, query, "blogs", size=50):
            blog_id = have.get(b["handle"])
            if blog_id:
                self.note("Blog", b["handle"], "Already there")
            else:
                if self.apply:
                    blog_id = check(self.target.gql("mutation($b: BlogCreateInput!) { blogCreate(blog: $b) { blog { id } userErrors { field message } } }", {"b": {
                        "handle": b["handle"], "title": b["title"], "templateSuffix": b["templateSuffix"]}})["blogCreate"], b["handle"])["blog"]["id"]
                self.note("Blog", b["handle"], "Created" if self.apply else "Would create")
            existing = set()
            if blog_id:
                existing = {a["handle"] for a in paged(self.target, "query($id: ID!, $first: Int!, $after: String) { blog(id: $id) { articles(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { handle } } } }", "blog.articles", {"id": blog_id})}
            articles = paged(self.source, """query($id: ID!, $first: Int!, $after: String) { blog(id: $id) { articles(first: $first, after: $after) { pageInfo { hasNextPage endCursor }
              nodes { handle title body summary tags isPublished publishedAt author { name } image { url altText } } } } }""", "blog.articles", {"id": b["id"]})
            for a in articles:
                if a["handle"] in existing:
                    self.note("Article", f"{b['handle']}/{a['handle']}", "Already there")
                    continue
                if self.apply and blog_id:
                    data = {"blogId": blog_id, "handle": a["handle"], "title": a["title"], "body": a["body"], "summary": a["summary"], "tags": a["tags"],
                            "isPublished": a["isPublished"], "author": {"name": (a.get("author") or {}).get("name") or "LC Modest Wear"}}
                    if a["image"]:
                        data["image"] = {"url": a["image"]["url"], "altText": a["image"]["altText"]}
                    check(self.target.gql("mutation($a: ArticleCreateInput!) { articleCreate(article: $a) { article { id } userErrors { field message } } }", {"a": data})["articleCreate"], a["handle"])
                self.note("Article", f"{b['handle']}/{a['handle']}", "Created" if self.apply else "Would create")

    def menus(self):
        item = "title type url resourceId tags"
        query = f"""query($first: Int!, $after: String) {{ menus(first: $first, after: $after) {{ pageInfo {{ hasNextPage endCursor }}
          nodes {{ id handle title items {{ {item} items {{ {item} items {{ {item} }} }} }} }} }} }}"""
        target = {m["handle"]: m["id"] for m in paged(self.target, query, "menus", size=50)}
        kinds = {"PRODUCT": "product", "COLLECTION": "collection", "PAGE": "page", "BLOG": "blog", "METAOBJECT": "metaobject"}

        def convert(items):
            out = []
            for i in items:
                entry = {"title": i["title"], "type": i["type"], "url": i["url"], "tags": i.get("tags") or []}
                if i["type"] in kinds:
                    entry["resourceId"] = self.same(kinds[i["type"]], i["resourceId"])
                    if not entry["resourceId"]:  # not in LCM Pak: keep the link as a plain web address
                        entry["type"] = "HTTP"
                        entry["url"] = urlparse(i["url"] or "/").path or "/"
                elif i["type"] in ("ARTICLE", "SHOP_POLICY", "CUSTOMER_ACCOUNT_PAGE"):
                    entry["type"], entry["url"] = "HTTP", urlparse(i["url"] or "/").path or "/"
                if entry["type"] == "HTTP" and not entry["url"]:
                    entry["url"] = "/"
                if i.get("items"):
                    entry["items"] = convert(i["items"])
                out.append(entry)
            return out

        for m in paged(self.source, query, "menus", size=50):
            items = convert(m["items"])
            existing = target.get(m["handle"])
            if self.apply:
                if existing:
                    check(self.target.gql("mutation($id: ID!, $t: String!, $h: String, $i: [MenuItemUpdateInput!]!) { menuUpdate(id: $id, title: $t, handle: $h, items: $i) { menu { id } userErrors { field message } } }",
                                          {"id": existing, "t": m["title"], "h": m["handle"], "i": items})["menuUpdate"], m["handle"])
                else:
                    check(self.target.gql("mutation($t: String!, $h: String!, $i: [MenuItemCreateInput!]!) { menuCreate(title: $t, handle: $h, items: $i) { menu { id } userErrors { field message } } }",
                                          {"t": m["title"], "h": m["handle"], "i": items})["menuCreate"], m["handle"])
            verb = ("Updated" if existing else "Created") if self.apply else ("Would update" if existing else "Would create")
            self.note("Menu", m["handle"], verb, f"{len(items)} items")

    def redirects(self):
        query = "query($first: Int!, $after: String) { urlRedirects(first: $first, after: $after) { pageInfo { hasNextPage endCursor } nodes { path target } } }"
        have = {r["path"] for r in paged(self.target, query, "urlRedirects", size=250)}
        for r in paged(self.source, query, "urlRedirects", size=250):
            if r["path"] in have:
                continue
            if self.apply:
                check(self.target.gql("mutation($r: UrlRedirectInput!) { urlRedirectCreate(urlRedirect: $r) { urlRedirect { id } userErrors { field message } } }", {"r": r})["urlRedirectCreate"], r["path"])
            self.note("Redirect", r["path"], "Created" if self.apply else "Would create", r["target"])

    def compare_products(self):
        """Products of the International store that are not in LCM Pak (matched by handle), with why they may be missing."""
        query = """query($first: Int!, $after: String) { products(first: $first, after: $after) { pageInfo { hasNextPage endCursor }
          nodes { handle title status variantsCount { count } media(first: 1) { nodes { id } } } } }"""
        have = set(self.handles(self.target, "product").values())
        total = missing = 0
        for p in paged(self.source, query, "products", size=100):
            total += 1
            if p["handle"] in have:
                continue
            missing += 1
            self.note("Missing product", p["handle"], p["status"].title(),
                      f"{p['title']} | {p['variantsCount']['count']} variants | {'has images' if p['media']['nodes'] else 'no images'}")
        self.log(f"Products: {total} in the International store, {len(have)} in LCM Pak, {missing} not in LCM Pak")

    # ---------- running ----------
    def run(self, step):
        parts = {
            # Files first (metaobjects may point at them), metaobject types before the field definitions that use them
            "setup": [("Files", self.copy_files), ("Metaobjects", self.metaobjects), ("Metafield definitions", self.metafield_definitions)],
            "compare": [("Compare products", self.compare_products)],
            "content": [("Product metafields", self.product_metafields), ("Collections", self.collections), ("Pages", self.pages),
                        ("Blogs", self.blogs), ("Menus", self.menus), ("Redirects", self.redirects)],
        }[step]
        for title, part in parts:
            self.log(f"--- {title}")
            try:
                part()
            except RuntimeError as error:
                self.note(title, "", "Failed", str(error)[:500])
                self.log(f"{title} stopped: {error}")
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        with open(REPORT, "w", newline="", encoding="utf-8-sig") as f:
            out = csv.writer(f)
            out.writerow(["What", "Name", "Result", "Detail"])
            out.writerows(sorted(self.rows, key=lambda r: (r[2] != "Failed", r[0])))
        counts = {}
        for row in self.rows:
            counts[(row[0], row[2])] = counts.get((row[0], row[2]), 0) + 1
        for (kind, result), n in sorted(counts.items()):
            self.log(f"{kind}: {n} {result.lower()}")
        self.log(f"Report: {REPORT}")
        return self.rows


def main():
    ap = argparse.ArgumentParser(description="Copy the International store's content into LCM Pak")
    ap.add_argument("--step", choices=["setup", "content", "compare"], required=True)
    ap.add_argument("--apply", action="store_true", help="copy (without it: test only, copies nothing)")
    args = ap.parse_args()
    if not (TARGET_STORE and TARGET_CLIENT_ID and TARGET_CLIENT_SECRET):
        sys.exit("Add TARGET_SHOPIFY_STORE, TARGET_SHOPIFY_CLIENT_ID and TARGET_SHOPIFY_CLIENT_SECRET (the LCM Pak app)")
    if TARGET_STORE.strip().lower() == config.SHOP_DOMAIN.strip().lower():
        sys.exit("The target store is the International store itself: check TARGET_SHOPIFY_STORE")
    started = time.time()
    source = Shopify()
    target = Shopify(domain=TARGET_STORE, token=client_token(TARGET_STORE, TARGET_CLIENT_ID, TARGET_CLIENT_SECRET))
    Copier(source, target, apply=args.apply).run(args.step)
    print(f"Finished in {int(time.time() - started)} s.")


if __name__ == "__main__":
    main()
