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
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .gemini import QuotaExhausted
from .pipeline import ProductRun, to_jpeg

STATE_KEY = "image_batches"
DONE_STATES = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
FAILED_STATES = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
# Google's words for a passing problem (not counted as a try); codes only as whole numbers
TEMPORARY = re.compile(r"resource_exhausted|unavailable|internal error|deadline_exceeded|\b(429|500|503)\b", re.I)


class NotFound(RuntimeError):
    """The file is not in the bucket."""


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
        if r.status_code == 404:
            raise NotFound(f"Cloud Storage 404: {r.text[:200]}")
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
        self.removed = set()  # jobs read and cleared in this run
        self._sample = None  # first answer line of the last job read (for the log)
        self.created = set()  # jobs started in this run
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
        """Write the job list, merged with what is in Shopify now, so a PC and GitHub never wipe each other's jobs."""
        if not self.dry_run:
            fresh = self.s.get_shop_json(STATE_KEY) or {"jobs": []}
            jobs = {j["name"]: j for j in fresh.get("jobs", [])}
            for j in self.state["jobs"]:
                # update the jobs still listed; add back only jobs this run created (not ones another machine cleared)
                if j["name"] in jobs or j["name"] in self.created:
                    jobs[j["name"]] = j
            self.state = {"jobs": [j for name, j in jobs.items() if name not in self.removed]}
        self.out.mkdir(parents=True, exist_ok=True)
        self.local_state.write_text(json.dumps(self.state, indent=2))
        if not self.dry_run:
            self.s.set_shop_json(STATE_KEY, self.state)

    def running(self):
        return [j for j in self.state["jobs"] if j.get("state") not in DONE_STATES | FAILED_STATES]

    def _run_for(self, product):
        return ProductRun(product, self.g, self.s, out_dir=self.out if self.dry_run else None, log=self.log)

    # ---------- submit ----------
    def submit(self, limit_products, handles=None, retry_manual=False, redo=None):
        """Start one Batch job with the images still to make. Returns the job name, or None if there was nothing to do.
        retry_manual: also try again the images marked "do manually". redo: image or section names to make again
        (only with handles)."""
        self.state = self._load_state()
        size = config.VERTEX_BATCH_SIZE
        price = config.VERTEX_BATCH_PRICE_PER_IMAGE
        each = price + config.VERTEX_PRICE_PER_CHECK  # the check at collect time is paid too: keep room for it
        room = int((config.VERTEX_BUDGET_USD - self.g.spend.data["usd"]) / each) if each else size
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
            final = ("done", "needs_photo", "skipped") if retry_manual else ("done", "manual", "needs_photo", "skipped")
            if audit.get("status") in final and not handles:
                continue
            run = self._run_for(product)
            if retry_manual:
                run.reopen_manual()
            if redo and handles:
                run.reopen(redo)
            try:
                if run.prepare():
                    continue
                # read the job list again right now, so a job another machine has just started is not mistaken for a lost one
                if any(st.get("status") == "queued" for g in ("slots", "colours") for st in run.audit[g].values()):
                    live = ({j["name"] for j in self._load_state()["jobs"]} | self.created) - self.removed
                    run.release_orphans(live)
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
                mapping[f"{uris[kind]}\n{job.prompt}"] = {"product": product["id"], "name": job.name, "kind": job.kind, "value": job.value, "base": kind}
            runs.append((run, jobs))
            self.log(f"  {product['title']}: {len(jobs)} image(s)")
        if not lines:
            self.log("Nothing to submit.")
            return None

        src = self.storage.upload(f"{prefix}/input.jsonl", ("\n".join(lines) + "\n").encode(), "application/jsonl")
        self.storage.upload(f"{prefix}/map.json", json.dumps(mapping).encode(), "application/json")
        job = self.g.create_batch(src, f"gs://{config.VERTEX_BUCKET}/{prefix}/output", f"lc-images-{stamp}")
        # Record the job straight away (it is running and paid for); then mark its images
        self.g.spend.add(price * len(lines), "images", count=len(lines))
        self.created.add(job)
        self.state["jobs"].append({"name": job, "prefix": prefix, "created": stamp, "count": len(lines), "state": "JOB_STATE_PENDING"})
        self._save_state()
        for run, jobs in runs:
            for j in jobs:
                run.mark_queued(j, job)
            run.save()
        self.counts["submitted"] += len(lines)
        self.log(f"Submitted {len(lines)} image(s) for {len(runs)} product(s) as {job}")
        return job

    # ---------- collect ----------
    def collect(self):
        """Read every finished job and act on its images. Returns how many jobs are still running."""
        self.state = self._load_state()
        for entry in list(self.state["jobs"]):
            state = self.g.batch_state(entry["name"])
            entry["state"] = state
            if state not in DONE_STATES | FAILED_STATES:
                self.log(f"  {entry['name']}: {state.replace('JOB_STATE_', '').lower()}, {entry['count']} image(s)")
                continue
            try:
                mapping = json.loads(self.storage.download(f"{entry['prefix']}/map.json"))
            except NotFound:
                mapping = None  # already read and cleared (an earlier run stopped while cleaning up)
            if mapping is not None:
                # a failed job (e.g. stopped by Google after 24 hours) can still hold finished images: use them
                answers = self._read_answers(entry)
                matched = sum(1 for key in mapping if self._key(*key.split("\n", 1)) in answers)
                if state in DONE_STATES and mapping and not matched and entry.get("reads", 0) + 1 < config.VERTEX_BATCH_MAX_READS:
                    # finished but no answer could be matched: keep its files and images as they are, so nothing is
                    # paid for twice while this is looked into
                    entry["reads"] = entry.get("reads", 0) + 1
                    self.log(f"  {entry['name']}: none of its {len(mapping)} answers could be matched; kept to be read again. "
                             f"First answer: {self._describe(self._sample) if self._sample else 'none'}")
                    self._save_state()
                    continue
                if state in FAILED_STATES:
                    detail = self.g.batch_detail(entry["name"]) if hasattr(self.g, "batch_detail") else state
                    self.log(f"  {entry['name']}: {detail}; {matched} finished image(s) used, the rest go into the next round (no try counted)")
                unanswered = len(mapping) - matched
                if unanswered > 0 and not entry.get("refunded"):
                    # images Google never made are not charged: take them back out of the spending estimate
                    entry["refunded"] = True
                    self.g.spend.add(-config.VERTEX_BATCH_PRICE_PER_IMAGE * unanswered, "images", count=-unanswered)
                if not self._apply(mapping, answers, entry["name"]):
                    entry["reads"] = entry.get("reads", 0) + 1
                    if entry["reads"] < config.VERTEX_BATCH_MAX_READS:
                        self.log(f"  {entry['name']}: kept, to be read again next time (some products could not be finished)")
                        self._save_state()
                        continue
                    self.log(f"  {entry['name']}: read {entry['reads']} times; dropped. Its unfinished images go back into the queue")
            for name in self.storage.list(entry["prefix"] + "/"):
                self.storage.delete(name)
            self.removed.add(entry["name"])
            self.state["jobs"].remove(entry)
            self._save_state()
        self._save_state()
        return len(self.running())

    @staticmethod
    def _key(uri, text):
        """How an answer is matched to its image: the photo it was made from and the prompt (spacing ignored)."""
        return f"{(uri or '').strip()}\n{' '.join((text or '').split())}"

    @staticmethod
    def _request_key(item):
        """The key of the request an answer line repeats (Google may write it in camelCase or snake_case)."""
        request = item.get("request") or {}
        if isinstance(request, str):
            try:
                request = json.loads(request)
            except json.JSONDecodeError:
                request = {}
        uri, texts = "", []
        for content in request.get("contents") or []:
            for part in content.get("parts") or []:
                data = part.get("fileData") or part.get("file_data") or {}
                uri = uri or data.get("fileUri") or data.get("file_uri") or ""
                if part.get("text"):
                    texts.append(part["text"])
        return BatchRunner._key(uri, "\n".join(texts))

    def _read_answers(self, entry):
        answers, files, lines, sample = {}, 0, 0, None
        for name in self.storage.list(f"{entry['prefix']}/output/"):
            if not name.endswith(".jsonl"):
                continue
            files += 1
            for line in self.storage.download(name).decode("utf-8").splitlines():
                if not line.strip():
                    continue
                lines += 1
                item = json.loads(line)
                answers[self._request_key(item)] = item
                if sample is None:
                    sample = item
        self.log(f"  {entry['name']}: {lines} answer(s) in {files} result file(s)")
        self._sample = sample
        return answers

    @staticmethod
    def _describe(item):
        """A short outline of an answer line for the log (image data left out)."""
        def outline(value, depth=0):
            if isinstance(value, dict):
                return {k: outline(v, depth + 1) for k, v in value.items()} if depth < 4 else "{...}"
            if isinstance(value, list):
                return [outline(v, depth + 1) for v in value[:3]]
            if isinstance(value, str) and len(value) > 80:
                return value[:80] + "..."
            return value
        return json.dumps(outline(item))[:900]

    @staticmethod
    def _image_of(item):
        """(image bytes or None, problem, temporary) from one answer line."""
        status = item.get("status") or ""
        if status:
            return None, f"Batch error: {str(status)[:200]}", bool(TEMPORARY.search(str(status)))
        response = item.get("response") or {}
        for candidate in response.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"]), None, False
        reason = ((response.get("candidates") or [{}])[0]).get("finishReason") or (response.get("promptFeedback") or {}).get("blockReason") or "no image returned"
        return None, f"Gemini gave no image ({reason})", False

    def _apply(self, mapping, answers, batch_name):
        """Act on every answer. Returns False if a product could not be finished (the job is then read again later;
        images already handled are skipped then). The spending cap stops it at once (QuotaExhausted)."""
        complete = True
        by_product = {}
        for key, ref in mapping.items():
            by_product.setdefault(ref["product"], []).append((key, ref))
        for pid, refs in by_product.items():
            product = self.s.product_by_id(pid)
            if not product:
                continue  # the product was deleted
            run = self._run_for(product)
            try:
                run.prepare()
                for key, ref in refs:
                    job = run.job_for(ref, batch_name)
                    if job is None:
                        continue  # no longer queued (redone or changed by hand since)
                    item = answers.get(self._key(*key.split("\n", 1)))
                    if item is None:
                        raw, problem, temporary = None, "no answer in the Batch results", True
                    else:
                        raw, problem, temporary = self._image_of(item)
                    outcome = run.batch_result(job, raw, problem, temporary)
                    self.counts[outcome] += 1
                    self.log(f"    {job.name}: {outcome}" + (f" ({problem[:120]})" if problem and outcome != "passed" else ""))
                status = run.finish()
                self.log(f"  {product['title']}: {status}")
            except QuotaExhausted:
                raise  # the job stays in the list and is read again once there is room
            except Exception as error:
                complete = False
                self.log(f"  {product['title']}: could not finish this round ({str(error)[:200]}); will be read again")
        return complete

    def auto(self, limit_products, handles=None, retry_manual=False, redo=None):
        """Collect what is finished, then submit the next round unless VERTEX_BATCH_MAX_JOBS jobs are still out
        (images already out in a job are never sent twice)."""
        self.collect()
        out = len(self.state["jobs"])
        if out >= config.VERTEX_BATCH_MAX_JOBS:
            self.log(f"{out} Batch job(s) still out; the next round starts once one of them is read.")
            return None
        return self.submit(limit_products, handles, retry_manual, redo)
