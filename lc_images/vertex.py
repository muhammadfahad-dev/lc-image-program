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
    """Estimated spend so far, kept in a file so the cap holds across runs."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"usd": 0.0, "images": 0, "checks": 0}

    def add(self, usd, kind):
        self.data["usd"] = round(self.data["usd"] + usd, 4)
        self.data[kind] = self.data.get(kind, 0) + 1
        self.data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


class GeminiVertex:
    def __init__(self, project=None, location=None, out_dir="output", client=None, log=print):
        self.project = project or config.VERTEX_PROJECT
        self.location = location or config.VERTEX_LOCATION
        self.log = log
        self.calls = {"image": 0, "check": 0}
        self.image_model = config.VERTEX_IMAGE_MODEL
        self.check_model = config.VERTEX_CHECK_MODEL
        self.spend = Spend(Path(out_dir) / "vertex_spend.json")
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

    def choose_models(self, require_image=True):
        return self.image_model, self.check_model

    # ---------- plumbing ----------
    def _budget_check(self, cost):
        if self.spend.data["usd"] + cost > config.VERTEX_BUDGET_USD:
            raise QuotaExhausted(
                f"Spending cap reached: about ${self.spend.data['usd']:.2f} used of the ${config.VERTEX_BUDGET_USD:.0f} cap "
                "(VERTEX_BUDGET_USD). Check the real figure in Google Cloud Billing before raising it."
            )

    def _call(self, model, contents, cfg, kind):
        from google.genai import errors

        waits = config.VERTEX_RATE_LIMIT_WAITS
        for attempt in range(len(waits) + 1):
            gap = time.monotonic() - self._last
            if gap < config.VERTEX_SECONDS_BETWEEN_CALLS:
                time.sleep(config.VERTEX_SECONDS_BETWEEN_CALLS - gap)
            self._last = started = time.monotonic()
            try:
                response = self.client.models.generate_content(model=model, contents=contents, config=cfg)
                self.log(f"    {kind} answered in {time.monotonic() - started:.0f}s")
                return response
            except errors.ClientError as error:
                if error.code in (401, 403):
                    raise RuntimeError(f"Vertex AI refused access ({error.code}): {str(error)[:300]}. Check the setup steps in lc_images/vertex.py.")
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
        cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0)
        response = self._call(self.check_model, [*self._parts(images, labels=True), prompt], cfg, "check")
        self.spend.add(config.VERTEX_PRICE_PER_CHECK, "checks")
        self.calls["check"] += 1
        text = response.text or ""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            return json.loads(text[start : end + 1]) if start >= 0 else {}
