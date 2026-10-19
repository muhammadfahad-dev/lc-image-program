"""Shopify Admin API calls used by the program."""
import json
import time

import requests

from . import config

PRODUCT_FIELDS = """
  id title handle productType tags status createdAt
  options { name values }
  media(first: 60) { nodes { id mediaContentType alt ... on MediaImage { image { url width height } } } }
  variants(first: 250) { nodes { id selectedOptions { name value } media(first: 1) { nodes { id } } } }
  audit: metafield(namespace: "custom", key: "image_audit") { value }
  fitting: metafield(namespace: "custom", key: "fitting_image") { value }
  multi: metafield(namespace: "custom", key: "multi_display_images") { value }
  closer: metafield(namespace: "custom", key: "closer_images") { value }
"""


class Shopify:
    def __init__(self, domain=None, token=None, session=None, dry_run=False):
        self.domain = domain or config.SHOP_DOMAIN
        self.url = f"https://{self.domain}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
        self.http = session or requests.Session()
        self.token = token or config.SHOPIFY_TOKEN or self._client_credentials_token()
        self.dry_run = dry_run

    def _client_credentials_token(self):
        """Exchange the app's client ID and secret for an access token (valid about 24 hours)."""
        if not (config.SHOPIFY_CLIENT_ID and config.SHOPIFY_CLIENT_SECRET):
            raise RuntimeError("No Shopify access: add SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET (or SHOPIFY_ADMIN_TOKEN) as secrets")
        r = self.http.post(
            f"https://{self.domain}/admin/oauth/access_token",
            data={"grant_type": "client_credentials", "client_id": config.SHOPIFY_CLIENT_ID, "client_secret": config.SHOPIFY_CLIENT_SECRET},
            timeout=60,
        )
        if r.status_code != 200 or "access_token" not in r.text:
            raise RuntimeError(f"Shopify refused the client ID and secret ({r.status_code}): {r.text[:300]}. Check the app is installed on the store.")
        return r.json()["access_token"]

    def gql(self, query, variables=None):
        for attempt in range(6):
            r = self.http.post(self.url, json={"query": query, "variables": variables or {}}, headers={"X-Shopify-Access-Token": self.token}, timeout=90)
            if r.status_code in (429, 502, 503):
                time.sleep(2 + attempt * 3)
                continue
            r.raise_for_status()
            data = r.json()
            errors = data.get("errors")
            if errors and any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                time.sleep(2 + attempt * 3)
                continue
            if errors:
                raise RuntimeError(f"Shopify: {json.dumps(errors)[:500]}")
            return data["data"]
        raise RuntimeError("Shopify kept throttling")

    @staticmethod
    def _check(payload, name):
        problems = payload.get("userErrors") or []
        if problems:
            raise RuntimeError(f"{name}: {json.dumps(problems)[:500]}")
        return payload

    # ---------- reading ----------
    def iter_products(self, handles=None):
        """Active products oldest first, or only the given handles."""
        if handles:
            for handle in handles:
                d = self.gql(f"query($h: String!) {{ productByIdentifier(identifier: {{handle: $h}}) {{ {PRODUCT_FIELDS} }} }}", {"h": handle})
                if d.get("productByIdentifier"):
                    yield d["productByIdentifier"]
            return
        after = None
        while True:
            d = self.gql(
                f"query($after: String) {{ products(first: 25, after: $after, sortKey: CREATED_AT, query: \"status:active\") {{ pageInfo {{ hasNextPage endCursor }} nodes {{ {PRODUCT_FIELDS} }} }} }}",
                {"after": after},
            )
            page = d["products"]
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            after = page["pageInfo"]["endCursor"]

    def file_url(self, file_id):
        d = self.gql("query($id: ID!) { node(id: $id) { ... on MediaImage { image { url } } } }", {"id": file_id})
        return ((d.get("node") or {}).get("image") or {}).get("url")

    def shop_id(self):
        return self.gql("{ shop { id } }")["shop"]["id"]

    # ---------- writing ----------
    def upload_image(self, jpeg, filename, alt):
        """Upload a JPEG to Shopify Files and return its MediaImage id once it is ready."""
        if self.dry_run:
            return f"dry-run://{filename}"
        staged = self._check(
            self.gql(
                "mutation($i: [StagedUploadInput!]!) { stagedUploadsCreate(input: $i) { stagedTargets { url resourceUrl parameters { name value } } userErrors { field message } } }",
                {"i": [{"resource": "IMAGE", "filename": filename, "mimeType": "image/jpeg", "httpMethod": "POST", "fileSize": str(len(jpeg))}]},
            )["stagedUploadsCreate"],
            "stagedUploadsCreate",
        )["stagedTargets"][0]
        form = {p["name"]: p["value"] for p in staged["parameters"]}
        up = requests.post(staged["url"], data=form, files={"file": (filename, jpeg, "image/jpeg")}, timeout=180)
        if up.status_code not in (200, 201, 204):
            raise RuntimeError(f"Upload failed {up.status_code}: {up.text[:300]}")
        created = self._check(
            self.gql(
                "mutation($f: [FileCreateInput!]!) { fileCreate(files: $f) { files { id fileStatus } userErrors { field message } } }",
                {"f": [{"originalSource": staged["resourceUrl"], "contentType": "IMAGE", "alt": alt[:500], "filename": filename}]},
            )["fileCreate"],
            "fileCreate",
        )["files"][0]
        file_id = created["id"]
        for _ in range(60):
            node = self.gql("query($id: ID!) { node(id: $id) { ... on MediaImage { fileStatus fileErrors { message } } } }", {"id": file_id})["node"]
            if node["fileStatus"] == "READY":
                return file_id
            if node["fileStatus"] == "FAILED":
                raise RuntimeError(f"Shopify could not process {filename}: {node.get('fileErrors')}")
            time.sleep(3)
        raise RuntimeError(f"Shopify took too long to process {filename}")

    def attach_to_product(self, product_id, file_ids):
        """Add uploaded files to the product's media (they appear at the end)."""
        if self.dry_run or not file_ids:
            return
        self._check(
            self.gql(
                "mutation($f: [FileUpdateInput!]!) { fileUpdate(files: $f) { files { id } userErrors { field message } } }",
                {"f": [{"id": i, "referencesToAdd": [product_id]} for i in file_ids]},
            )["fileUpdate"],
            "fileUpdate",
        )
        self.wait_for_media(product_id, file_ids)

    def wait_for_media(self, product_id, media_ids):
        for _ in range(40):
            d = self.gql("query($id: ID!) { product(id: $id) { media(first: 100) { nodes { id status } } } }", {"id": product_id})
            status = {m["id"]: m["status"] for m in d["product"]["media"]["nodes"]}
            if all(status.get(i) == "READY" for i in media_ids):
                return
            time.sleep(3)

    def move_media_to_front(self, product_id, media_ids):
        """Put these media first, in this order. Everything else keeps its order after them."""
        if self.dry_run or not media_ids:
            return
        payload = self._check(
            self.gql(
                "mutation($id: ID!, $m: [MoveInput!]!) { productReorderMedia(id: $id, moves: $m) { job { id done } userErrors: mediaUserErrors { field message } } }",
                {"id": product_id, "m": [{"id": mid, "newPosition": str(pos)} for pos, mid in enumerate(media_ids)]},
            )["productReorderMedia"],
            "productReorderMedia",
        )
        job = payload.get("job")
        for _ in range(30):
            if not job or job.get("done"):
                return
            time.sleep(2)
            job = self.gql("query($id: ID!) { job(id: $id) { id done } }", {"id": job["id"]})["job"]

    def set_metafields(self, entries):
        if self.dry_run or not entries:
            return
        for start in range(0, len(entries), 25):
            self._check(
                self.gql(
                    "mutation($m: [MetafieldsSetInput!]!) { metafieldsSet(metafields: $m) { metafields { key } userErrors { field message } } }",
                    {"m": entries[start : start + 25]},
                )["metafieldsSet"],
                "metafieldsSet",
            )

    def link_variants(self, product_id, pairs):
        """pairs: [(variant_id, media_id)]. The product page then shows that image when the colour is chosen."""
        if self.dry_run or not pairs:
            return
        self._check(
            self.gql(
                "mutation($p: ID!, $v: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $p, variants: $v) { productVariants { id } userErrors { field message } } }",
                {"p": product_id, "v": [{"id": v, "mediaId": m} for v, m in pairs]},
            )["productVariantsBulkUpdate"],
            "productVariantsBulkUpdate",
        )

    def save_audit(self, product_id, audit):
        self.set_metafields([{"ownerId": product_id, "namespace": "custom", "key": config.AUDIT_KEY, "type": "json", "value": json.dumps(audit)}])
