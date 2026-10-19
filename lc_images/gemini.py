"""Gemini API calls: image generation, photo selection and image checks."""
import base64
import json
import time

import requests

from . import config

API = "https://generativelanguage.googleapis.com/v1beta"


class QuotaExhausted(Exception):
    """The daily free limit is used up. The run stops and continues tomorrow."""


class GenerationFailed(Exception):
    """Gemini answered but gave no usable image (safety block, text only, etc.)."""


class Gemini:
    def __init__(self, key=None, session=None):
        self.key = key or config.GEMINI_KEY
        self.http = session or requests.Session()
        self._last = 0.0
        self.calls = {"image": 0, "check": 0}
        self.image_model = None
        self.check_model = None
        self.image_size = config.IMAGE_SIZE

    # ---------- plumbing ----------
    def _wait(self):
        gap = time.monotonic() - self._last
        if gap < config.SECONDS_BETWEEN_CALLS:
            time.sleep(config.SECONDS_BETWEEN_CALLS - gap)
        self._last = time.monotonic()

    def _post(self, model, body):
        for attempt in range(4):
            self._wait()
            r = self.http.post(f"{API}/models/{model}:generateContent", params={"key": self.key}, json=body, timeout=240)
            if r.status_code == 429:
                text = r.text
                if "PerDay" in text or "per day" in text.lower() or "daily" in text.lower():
                    raise QuotaExhausted(text[:3000])
                time.sleep(65)  # per-minute limit: wait and try again
                continue
            if r.status_code >= 500:
                time.sleep(20 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Gemini {r.status_code}: {r.text[:400]}")
            return r.json()
        raise QuotaExhausted("Gemini kept refusing with rate limits")

    def choose_models(self):
        """Pick the first configured models this key can actually use."""
        r = self.http.get(f"{API}/models", params={"key": self.key, "pageSize": 1000}, timeout=60)
        r.raise_for_status()
        names = {m["name"].split("/", 1)[1] for m in r.json().get("models", [])}
        self.available = names
        self.image_model = next((m for m in config.IMAGE_MODELS if m in names), None)
        self.check_model = next((m for m in config.CHECK_MODELS if m in names), None)
        if not self.image_model or not self.check_model:
            raise RuntimeError(f"No usable Gemini model. Image: {self.image_model}, check: {self.check_model}. Available: {sorted(names)[:40]}")
        return self.image_model, self.check_model

    @staticmethod
    def _part(jpeg):
        return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}

    # ---------- image generation ----------
    def generate(self, images, prompt, ratio):
        parts = [self._part(i) for i in images] + [{"text": prompt}]
        image_config = {"aspectRatio": ratio}
        if self.image_size:
            image_config["imageSize"] = self.image_size
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": image_config}}
        try:
            data = self._post(self.image_model, body)
        except RuntimeError as error:
            if self.image_size and "imagesize" in str(error).lower().replace("_", ""):
                self.image_size = None  # this model does not take a size: use its default from now on
                return self.generate(images, prompt, ratio)
            raise
        self.calls["image"] += 1
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        reason = (data.get("candidates") or [{}])[0].get("finishReason") or data.get("promptFeedback", {}).get("blockReason") or "no image returned"
        raise GenerationFailed(str(reason))

    # ---------- text + vision (JSON answers) ----------
    def ask_json(self, images, prompt):
        parts = []
        for index, image in enumerate(images):
            parts.append({"text": f"Image {index}:"})
            parts.append(self._part(image))
        parts.append({"text": prompt})
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        data = self._post(self.check_model, body)
        self.calls["check"] += 1
        text = "".join(p.get("text", "") for c in data.get("candidates", []) for p in c.get("content", {}).get("parts", []))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            return json.loads(text[start : end + 1]) if start >= 0 else {}
