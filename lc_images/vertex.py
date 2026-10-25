"""Image generation and checks through Vertex AI (Google Cloud), billed to your Cloud project.

This is the route the Google Cloud free-trial credit applies to. Which money pays (the free credit, a prepaid
balance or a card) is decided by Google Billing, not by this code: Google uses the credit first for services it
covers. What this code does guarantee:
  - every call goes to Vertex AI in your project (vertexai=True, no API key), signed in with your own
    Google account through Application Default Credentials;
  - only Google's own Gemini models are used (no Model Garden partner models);
  - it stops by itself before its estimated spend passes VERTEX_BUDGET_USD (default $280), and keeps a running
    total in output/vertex_spend.json.

One-time setup on your PC (Command Prompt), with the Google Cloud CLI installed (cloud.google.com/sdk):
  gcloud auth login
  gcloud config set project YOUR_PROJECT_ID
  gcloud auth application-default login
  gcloud auth application-default set-quota-project YOUR_PROJECT_ID
  gcloud services enable aiplatform.googleapis.com
and in .env:  GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .gemini import GenerationFailed, QuotaExhausted


class Spend:
    """Estimated spend so far. Kept in output/vertex_spend.json and, when a Shopify connection is given, in the shop
    field "vertex_spend" too, so the cap holds across runs and across your PC and GitHub. Each save adds this run's
    new spending to the figure freshly read from Shopify, so two machines never overwrite each other's spending."""

    def __init__(self, path, shopify=None):
        self.path = Path(path)
        self.shopify = shopify
        local = json.loads(self.path.read_text()) if self.path.exists() else {}
        shared = self._read_shared() or {}
        self.data = dict(max((local, shared), key=lambda d: d.get("usd", 0)) or {})
        self.data.setdefault("usd", 0.0)
        self._delta = {"usd": 0.0}  # spent in this run and not yet added to the shared figure

    def _read_shared(self):
        """The shared figure ({} if none yet), or None if it could not be read (then nothing is written over it)."""
        if self.shopify is None:
            return {}
        try:
            return self.shopify.get_shop_json("vertex_spend", {}) or {}
        except Exception:
            return None

    def add(self, usd, kind, count=1):
        self.data["usd"] = round(self.data["usd"] + usd, 4)
        self.data[kind] = self.data.get(kind, 0) + count
        self.data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self._delta["usd"] = round(self._delta["usd"] + usd, 4)
        self._delta[kind] = self._delta.get(kind, 0) + count
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))
        if self.shopify is not None and (self._delta.get("checks", 0) + self._delta.get("images", 0) >= 5 or count > 1):
            self.flush()

    def flush(self):
        if self.shopify is None or not self._delta["usd"]:
            return
        try:
            shared = self._read_shared()
            if shared is None:
                return  # could not read it: never overwrite the shared total with only this run's spending
            for key, value in self._delta.items():
                shared[key] = round(shared.get(key, 0) + value, 4)
            shared["updated"] = self.data.get("updated")
            self.shopify.set_shop_json("vertex_spend", shared)
            self._delta = {"usd": 0.0}
            if shared["usd"] > self.data["usd"]:
                self.data["usd"] = shared["usd"]  # another machine spent too: the cap uses the higher figure
        except Exception:
            pass  # kept in this run's figures; added next time


class GeminiVertex:
    def __init__(self, project=None, location=None, out_dir="output", client=None, log=print, shopify=None):
        self.project = project or config.VERTEX_PROJECT
        self.location = location or config.VERTEX_LOCATION
        self.log = log
        self.calls = {"image": 0, "check": 0}
        self.image_model = config.VERTEX_IMAGE_MODEL
        self.check_model = config.VERTEX_CHECK_MODEL
        self.spend = Spend(Path(out_dir) / "vertex_spend.json", shopify)
        self._last = 0.0
        if client is None:
            if not self.project:
                raise RuntimeError("Set GOOGLE_CLOUD_PROJECT in .env to your Google Cloud project ID")
            from google import genai

            from google.genai import types

            # vertexai=True: Vertex AI in your project, signed in with Application Default Credentials (no API key).
            # A time limit per request, so a stuck connection cannot hang the run.
            client = genai.Client(
                vertexai=True, project=self.project, location=self.location,
                http_options=types.HttpOptions(timeout=config.VERTEX_TIMEOUT_SECONDS * 1000),
            )
        self.client = client

    def list_models(self):
        """Google's own models this project can call, as (name, display name)."""
        found = []
        for model in self.client.models.list(config={"query_base": True, "page_size": 200}):
            name = (model.name or "").split("/")[-1]
            found.append((name, getattr(model, "display_name", "") or ""))
        return found

    # ---------- Batch ----------
    def create_batch(self, src, dest, display_name):
        """Start a Batch job reading requests from src (gs://.../input.jsonl), writing answers under dest. Returns its name."""
        from google.genai import types

        job = self.client.batches.create(model=self.image_model, src=src, config=types.CreateBatchJobConfig(dest=dest, display_name=display_name))
        return job.name

    def batch_state(self, name):
        job = self.client.batches.get(name=name)
        state = job.state
        return getattr(state, "name", None) or str(state)

    def batch_detail(self, name):
        """A line about a Batch job for the log: its state, how long it has been out, how many answers are done and
        Google's error message if it failed."""
        job = self.client.batches.get(name=name)
        state = getattr(job.state, "name", None) or str(job.state)
        parts = [state.replace("JOB_STATE_", "").lower()]
        if job.create_time:
            from datetime import datetime, timezone

            hours = (datetime.now(timezone.utc) - job.create_time).total_seconds() / 3600
            parts.append(f"out for {hours:.1f} h")
        stats = job.completion_stats
        if stats is not None:
            parts.append(f"{stats.successful_count or 0} done, {stats.failed_count or 0} failed, {stats.incomplete_count or 0} not yet done")
        if job.error is not None and (job.error.message or job.error.code):
            parts.append(f"Google's error: {job.error.code} {job.error.message}")
        return "; ".join(parts)

    def choose_models(self, require_image=True):
        return self.image_model, self.check_model

    # ---------- plumbing ----------
    def _budget_check(self, cost):
        if self.spend.data["usd"] + cost > config.VERTEX_BUDGET_USD:
            raise QuotaExhausted(
                f"Spending cap reached: about ${self.spend.data['usd']:.2f} used of the ${config.VERTEX_BUDGET_USD:.0f} cap "
                "(VERTEX_BUDGET_USD). Check the real figure in Google Cloud Billing before raising it."
            )

    def _call(self, model, contents, cfg, kind, backups=()):
        """One request. Google's "too many requests" (its shared capacity for a model is busy), server errors and
        no answer are waited out and tried again. Then the backup models are tried, each with shorter waits, and
        only after that does the run stop (and continue next time)."""
        from google.genai import errors

        reason = ""
        for index, current in enumerate([model, *backups]):
            if index:
                waits = config.VERTEX_BACKUP_WAITS
            else:
                waits = config.VERTEX_CHECK_WAITS if backups else config.VERTEX_RATE_LIMIT_WAITS
            if index:
                self.log(f"    {kind}: {model} stays busy; trying the backup model {current}")
            for attempt in range(len(waits) + 1):
                gap = time.monotonic() - self._last
                if gap < config.VERTEX_SECONDS_BETWEEN_CALLS:
                    time.sleep(config.VERTEX_SECONDS_BETWEEN_CALLS - gap)
                self._last = started = time.monotonic()
                try:
                    response = self.client.models.generate_content(model=current, contents=contents, config=cfg(current) if callable(cfg) else cfg)
                    self.log(f"    {kind} answered in {time.monotonic() - started:.0f}s" + (f" ({current})" if index else ""))
                    return response
                except errors.ClientError as error:
                    if error.code in (401, 403):
                        raise RuntimeError(f"Vertex AI refused access ({error.code}): {str(error)[:300]}. Check the setup steps in lc_images/vertex.py.")
                    if error.code == 404 and index:
                        reason = f"{current} is not available in this project"
                        break  # this backup does not exist here: try the next one
                    if error.code != 429:
                        raise RuntimeError(f"Vertex AI {error.code}: {str(error)[:400]}")
                    reason = f"Google says too many requests ({str(error)[:120]})"
                except errors.ServerError as error:
                    reason = f"Google server error {error.code}"
                except Exception as error:  # timeout or dropped connection
                    if "timeout" not in type(error).__name__.lower() and "connect" not in type(error).__name__.lower() and "timed out" not in str(error).lower():
                        raise
                    reason = f"no answer within {config.VERTEX_TIMEOUT_SECONDS}s"
                if attempt == len(waits):
                    break
                self.log(f"    {kind}: {reason}; waiting {waits[attempt]}s ({attempt + 1}/{len(waits)})")
                time.sleep(waits[attempt])
        raise QuotaExhausted(f"Vertex AI kept refusing ({reason}); stopping for now. Run the same command again later to continue.")

    @staticmethod
    def _parts(images, labels=False):
        from google.genai import types

        parts = []
        for index, jpeg in enumerate(images):
            if labels:
                parts.append(types.Part.from_text(text=f"Image {index}:"))
            parts.append(types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"))
        return parts

    # ---------- image generation ----------
    def generate(self, images, prompt, ratio):
        from google.genai import types

        self._budget_check(config.VERTEX_PRICE_PER_IMAGE)
        cfg = types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio=ratio))
        response = self._call(self.image_model, [*self._parts(images), prompt], cfg, "image")
        self.spend.add(config.VERTEX_PRICE_PER_IMAGE, "images")  # charged whether or not an image comes back
        self.calls["image"] += 1
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        candidate = (response.candidates or [None])[0]
        reason = getattr(candidate, "finish_reason", None) or getattr(response.prompt_feedback, "block_reason", None) or "no image returned"
        raise GenerationFailed(str(reason))

    # ---------- text + vision (JSON answers) ----------
    def ask_json(self, images, prompt):
        from google.genai import types

        self._budget_check(config.VERTEX_PRICE_PER_CHECK)
        resolution = getattr(types.MediaResolution, f"MEDIA_RESOLUTION_{config.VERTEX_CHECK_MEDIA_RESOLUTION.upper()}")
        budgets = {"minimal": 128, "low": 512, "medium": 2048, "high": 8192}

        def cfg(model):
            # Gemini 2.x sets thinking by a token budget, later models by a level
            if model.startswith("gemini-2"):
                thinking = types.ThinkingConfig(thinking_budget=budgets.get(config.VERTEX_CHECK_THINKING, 512))
            else:
                thinking = types.ThinkingConfig(thinking_level=getattr(types.ThinkingLevel, config.VERTEX_CHECK_THINKING.upper()))
            return types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0, media_resolution=resolution, thinking_config=thinking,
            )

        response = self._call(self.check_model, [*self._parts(images, labels=True), prompt], cfg, "check", config.VERTEX_CHECK_BACKUPS)
        self.spend.add(config.VERTEX_PRICE_PER_CHECK, "checks")
        self.calls["check"] += 1
        text = response.text or ""
        try:
            answer = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            try:
                answer = json.loads(text[start : end + 1]) if start >= 0 else {}
            except json.JSONDecodeError:
                answer = {}
        if isinstance(answer, list):  # sometimes the answer comes wrapped in a list
            answer = next((a for a in answer if isinstance(a, dict)), {})
        return answer if isinstance(answer, dict) else {}
