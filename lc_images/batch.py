"""Batch mode on Vertex AI: about half the price per image, results within 24 hours (usually much sooner).

The work goes in rounds:
  submit   collect the images still to make (listing images; colour images once a product's Media section is
           finished), put each product's photo and the request list in your storage bucket, start one Batch job
  collect  when the job is finished, read every answer, check each image exactly as in one-by-one runs, then
           upload it, count a failed try, or (after the last try) mark it "do manually" with a placeholder
  auto     collect, then submit the next round if no job is still running (what GitHub runs every few hours)

Which images are in a job is kept in Shopify (shop field "image_batches") so a PC and GitHub see the same state,
and in output/batches.json. Nothing here deletes product photos; only the program's own files in the bucket's
lc-batches/ folder are removed after a job has been read.
"""
import base64
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .pipeline import ProductRun, to_jpeg

STATE_KEY = "image_batches"
DONE_STATES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
FAILED_STATES = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
TEMPORARY_WORDS = ("resource_exhausted", "unavailable", "internal", "deadline", "429", "500", "503")


class Storage:
    """The few Cloud Storage calls Batch needs, signed in the same way as Vertex AI."""

    def __init__(self, bucket, session=None):
        self.bucket = bucket
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            session = AuthorizedSession(creds)
        self.http = session

    @staticmethod
    def _ok(r):
        if r.status_code >= 300:
            raise RuntimeError(f"Cloud Storage {r.status_code}: {r.text[:300]}")
        return r

    def upload(self, name, data, content_type):
        self._ok(self.http.post(
            f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o",
            params={"uploadType": "media", "name": name}, data=data, headers={"Content-Type": content_type},
        ))
        return f"gs://{self.bucket}/{name}"

    def list(self, prefix):
        names, token = [], None
        while True:
            params = {"prefix": prefix, "fields": "items(name),nextPageToken"}
            if token:
                params["pageToken"] = token
            data = self._ok(self.http.get(f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o", params=params)).json()
            names += [i["name"] for i in data.get("items", [])]
            token = data.get("nextPageToken")
            if not token:
                return names

    def download(self, name):
        from urllib.parse import quote

        return self._ok(self.http.get(f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}", params={"alt": "media"})).content

    def delete(self, name):
        from urllib.parse import quote

        r = self.http.delete(f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}")
        if r.status_code not in (200, 204, 404):
            self._ok(r)


class BatchRunner:
    def __init__(self, gemini, shopify, storage=None, out_dir="output", dry_run=False, log=print):
        if not config.VERTEX_BUCKET:
            raise RuntimeError("Set VERTEX_BUCKET (your Cloud Storage bucket name) for Batch mode")
        self.g, self.s = gemini, shopify
        self.storage = storage or Storage(config.VERTEX_BUCKET)
        self.out = Path(out_dir)
        self.dry_run = dry_run
        self.log = log
        self.local_state = self.out / "batches.json"
        self.state = self._load_state()
        self.counts = {"submitted": 0, "passed": 0, "retry": 0, "manual": 0, "again": 0}

    # ---------- state ----------
    def _load_state(self):
        state = None
        if not self.dry_run:
            state = self.s.get_shop_json(STATE_KEY)
        if state is None and self.local_state.exists():
            state = json.loads(self.local_state.read_text())
        return state or {"jobs": []}

    def _save_state(self):
        self.out.mkdir(parents=True, exist_ok=True)
        self.local_state.write_text(json.dumps(self.state, indent=2))
        if not self.dry_run:
            self.s.set_shop_json(STATE_KEY, self.state)

    def running(self):
        return [j for j in self.state["jobs"] if j.get("state") not in DONE_STATES | FAILED_STATES]

    def _run_for(self, product):
        return ProductRun(product, self.g, self.s, out_dir=self.out if self.dry_run else None, log=self.log)

    # ---------- submit ----------
    def submit(self, limit_products, handles=None):
        """Start one Batch job with the images still to make. Returns the job name, or None if there was nothing to do."""
        size = config.VERTEX_BATCH_SIZE
        price = config.VERTEX_BATCH_PRICE_PER_IMAGE
        room = int((config.VERTEX_BUDGET_USD - self.g.spend.data["usd"]) / price) if price else size
        size = min(size, max(room, 0))
        if size <= 0:
            self.log("Spending cap reached; nothing submitted.")
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        prefix = f"lc-batches/{stamp}"
        lines, mapping, runs, products = [], {}, [], 0
        for product in self.s.iter_products(handles or None):
            if len(lines) >= size or products >= limit_products:
                break
            if product["productType"] not in config.PRODUCT_TYPES:
                continue
            audit = json.loads((product.get("audit") or {}).get("value") or "{}")
            if audit.get("status") in ("done", "manual", "needs_photo", "skipped") and not handles:
                continue
            run = self._run_for(product)
            try:
                if run.prepare():
                    continue
                jobs = run.pending()[: size - len(lines)]
            except Exception as error:  # one product must not stop the round
                self.log(f"  {product['title']}: skipped this round ({str(error)[:150]})")
                continue
            if not jobs:
                run.finish()
                continue
            products += 1
            number = product["id"].rsplit("/", 1)[-1]
            uris = {}
            for job in jobs:
                kind = "ref" if job.base is run.reference else "hero"
                if kind not in uris:
                    uris[kind] = self.storage.upload(f"{prefix}/photos/{number}-{kind}.jpg", to_jpeg(job.base, max_side=2048), "image/jpeg")
                lines.append(json.dumps({"request": {
                    "contents": [{"role": "user", "parts": [{"fileData": {"fileUri": uris[kind], "mimeType": "image/jpeg"}}, {"text": job.prompt}]}],
                    "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": job.ratio}},
                }}))
                mapping[f"{uris[kind]}\n{job.prompt}"] = {"product": product["id"], "name": job.name, "kind": job.kind, "value": job.value}
            runs.append((run, jobs))
            self.log(f"  {product['title']}: {len(jobs)} image(s)")
        if not lines:
            self.log("Nothing to submit.")
            return None

        src = self.storage.upload(f"{prefix}/input.jsonl", ("\n".join(lines) + "\n").encode(), "application/jsonl")
        self.storage.upload(f"{prefix}/map.json", json.dumps(mapping).encode(), "application/json")
        job = self.g.create_batch(src, f"gs://{config.VERTEX_BUCKET}/{prefix}/output", f"lc-images-{stamp}")
        for run, jobs in runs:
            for j in jobs:
                run.mark_queued(j, job)
            run.save()
        self.g.spend.add(price * len(lines), "images", count=len(lines))
        self.state["jobs"].append({"name": job, "prefix": prefix, "created": stamp, "count": len(lines), "state": "JOB_STATE_PENDING"})
        self._save_state()
        self.counts["submitted"] += len(lines)
        self.log(f"Submitted {len(lines)} image(s) for {len(runs)} product(s) as {job}")
        return job

    # ---------- collect ----------
    def collect(self):
        """Read every finished job and act on its images. Returns how many jobs are still running."""
        for entry in list(self.state["jobs"]):
            state = self.g.batch_state(entry["name"])
            entry["state"] = state
            if state not in DONE_STATES | FAILED_STATES:
                self.log(f"  {entry['name']}: {state.replace('JOB_STATE_', '').lower()}, {entry['count']} image(s)")
                continue
            mapping = json.loads(self.storage.download(f"{entry['prefix']}/map.json"))
            answers = self._read_answers(entry) if state in DONE_STATES else {}
            if state in FAILED_STATES:
                self.log(f"  {entry['name']}: {state}; its images go into the next round (no try counted)")
            self._apply(mapping, answers)
            for name in self.storage.list(entry["prefix"] + "/"):
                self.storage.delete(name)
            self.state["jobs"].remove(entry)
            self._save_state()
        self._save_state()
        return len(self.running())

    def _read_answers(self, entry):
        answers = {}
        for name in self.storage.list(f"{entry['prefix']}/output/"):
            if not name.endswith(".jsonl"):
                continue
            for line in self.storage.download(name).decode("utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                parts = (((item.get("request") or {}).get("contents") or [{}])[0]).get("parts") or []
                uri = next((p["fileData"]["fileUri"] for p in parts if p.get("fileData")), "")
                text = next((p["text"] for p in parts if "text" in p), "")
                answers[f"{uri}\n{text}"] = item
        return answers

    @staticmethod
    def _image_of(item):
        """(image bytes or None, problem, temporary) from one answer line."""
        status = item.get("status") or ""
        if status:
            return None, f"Batch error: {str(status)[:200]}", any(w in str(status).lower() for w in TEMPORARY_WORDS)
        response = item.get("response") or {}
        for candidate in response.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"]), None, False
        reason = ((response.get("candidates") or [{}])[0]).get("finishReason") or (response.get("promptFeedback") or {}).get("blockReason") or "no image returned"
        return None, f"Gemini gave no image ({reason})", False

    def _apply(self, mapping, answers):
        by_product = {}
        for key, ref in mapping.items():
            by_product.setdefault(ref["product"], []).append((key, ref))
        for pid, refs in by_product.items():
            product = self.s.product_by_id(pid)
            if not product:
                continue
            run = self._run_for(product)
            try:
                run.prepare()
                for key, ref in refs:
                    job = run.job_for(ref)
                    if job is None:
                        continue  # no longer queued (redone or changed by hand since)
                    item = answers.get(key)
                    if item is None:
                        raw, problem, temporary = None, "no answer in the Batch results", True
                    else:
                        raw, problem, temporary = self._image_of(item)
                    outcome = run.batch_result(job, raw, problem, temporary)
                    self.counts[outcome] += 1
                    self.log(f"    {job.name}: {outcome}" + (f" ({problem[:120]})" if problem and outcome != "passed" else ""))
                status = run.finish()
                self.log(f"  {product['title']}: {status}")
            except Exception as error:
                self.log(f"  {product['title']}: could not finish this round ({str(error)[:200]}); its images stay queued")

    def auto(self, limit_products, handles=None):
        """Collect what is finished, then submit the next round unless a job is still running."""
        still = self.collect()
        if still:
            self.log(f"{still} Batch job(s) still running; the next round starts once they are read.")
            return None
        return self.submit(limit_products, handles)
