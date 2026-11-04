"""Google Drive calls for the Drive upload program (drive_upload.py).

The program signs in as a person's Gmail account (OAuth, one-time approval saved as a refresh token): a normal Gmail
Drive does not accept files from program (service) accounts. It only lists folders and creates folders and files;
there is no delete or update call in this file.
"""
import json
import time
import uuid

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
SCOPE = "https://www.googleapis.com/auth/drive"  # needed to see the client's existing folders; the code only adds


def _q(text):
    """A value inside a Drive search query: backslashes and single quotes escaped."""
    return text.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, client_id, client_secret, refresh_token, session=None):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.http = session or requests.Session()
        self.token, self.expires = None, 0

    def _auth(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        r = self.http.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        }, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Google refused the Drive sign-in ({r.status_code}): {r.text[:300]}. Run python drive_upload.py --authorize again.")
        data = r.json()
        self.token, self.expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self.token

    def _call(self, method, url, **kwargs):
        for attempt in range(7):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self._auth()}"
            try:
                r = self.http.request(method, url, headers=headers, timeout=180, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 6:
                    raise
                time.sleep(3 + attempt * 5)
                continue
            if r.status_code in (429, 500, 502, 503) or (r.status_code == 403 and "rateLimitExceeded" in r.text):
                time.sleep(2 ** attempt)  # Drive asks to slow down: wait and try again
                continue
            if r.status_code >= 300:
                raise RuntimeError(f"Google Drive {r.status_code}: {r.text[:400]}")
            return r.json() if r.content else {}
        raise RuntimeError("Google Drive kept refusing (too many requests); run again later")

    def _list(self, query, fields="files(id,name,appProperties)"):
        found, token = [], None
        while True:
            params = {"q": query, "fields": f"nextPageToken,{fields}", "pageSize": 1000,
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            data = self._call("GET", API, params=params)
            found.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
                return found

    def folder_named(self, parent_id, name):
        """The folder with exactly this name directly inside parent_id, or None."""
        hits = self._list(f"'{_q(parent_id)}' in parents and name = '{_q(name)}' and mimeType = '{FOLDER}' and trashed = false")
        return hits[0]["id"] if hits else None

    def product_folder(self, parent_id, name, product_id, create=True):
        """The product's own folder inside a type folder: found by the product's ID (so a renamed product keeps its
        folder), else by name, else made (only when create is True)."""
        hits = self._list(f"'{_q(parent_id)}' in parents and mimeType = '{FOLDER}' and trashed = false and appProperties has {{ key='lc_product' and value='{_q(product_id)}' }}")
        if not hits:
            hits = self._list(f"'{_q(parent_id)}' in parents and name = '{_q(name)}' and mimeType = '{FOLDER}' and trashed = false")
        if hits:
            return hits[0]["id"]
        if not create:
            return None
        made = self._call("POST", API, params={"supportsAllDrives": "true", "fields": "id"}, json={
            "name": name, "mimeType": FOLDER, "parents": [parent_id], "appProperties": {"lc_product": product_id},
        })
        return made["id"]

    def uploaded_media(self, folder_id):
        """Shopify image IDs already uploaded into this folder (kept on each file as appProperties)."""
        files = self._list(f"'{_q(folder_id)}' in parents and trashed = false")
        return {f["appProperties"]["lc_media"] for f in files if (f.get("appProperties") or {}).get("lc_media")}

    def upload(self, folder_id, name, data, mime, media_id):
        """Create one file (never replaces or deletes)."""
        boundary = uuid.uuid4().hex
        meta = json.dumps({"name": name, "parents": [folder_id], "appProperties": {"lc_media": media_id}})
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--".encode()
        return self._call("POST", UPLOAD, params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"},
                          data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})["id"]
