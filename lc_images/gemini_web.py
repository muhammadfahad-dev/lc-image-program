"""Image generation through the Gemini website (gemini.google.com), driven by Playwright on your own PC.

You sign in yourself, in a normal Chrome window the program opens with its own profile folder.
The program then works like a person would: one image at a time, a new chat for each image,
a fixed pause between images. It does not hide that it is automated, does not handle CAPTCHAs,
and stops (QuotaExhausted) as soon as Gemini says a limit is reached; progress is kept and the
next run continues where this one stopped.

Photo selection and image checks still go to the Gemini API check model (free), through `api`.
"""
import base64
import io
import json
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from PIL import Image

from . import config
from .gemini import GenerationFailed, QuotaExhausted

SELECTORS_FILE = Path(__file__).with_name("selectors.json")


class NotSignedIn(Exception):
    """Gemini is not signed in in the program's Chrome profile. Run: python web.py login"""


class ControlMissing(Exception):
    """A control the program needs was not found on the page. A debug bundle was saved."""


def load_selectors(path=None):
    return json.loads(Path(path or config.WEB_SELECTORS or SELECTORS_FILE).read_text(encoding="utf-8"))


def find_chrome():
    if config.CHROME_PATH:
        return config.CHROME_PATH
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("chromium")


def ratio_of(text):
    w, h = (float(x) for x in text.split(":"))
    return w / h


class Usage:
    """Images made through the website per day, kept in output/web_usage.json."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def today(self):
        return self.data.get(date.today().isoformat(), 0)

    def add(self):
        key = date.today().isoformat()
        self.data[key] = self.data.get(key, 0) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


class GeminiWeb:
    def __init__(self, api=None, profile_dir=None, selectors=None, out_dir="output", log=print):
        self.api = api
        self.calls = api.calls if api else {"image": 0, "check": 0}
        self.profile = Path(profile_dir or config.WEB_PROFILE_DIR).resolve()
        self.sel = selectors or load_selectors()
        self.out = Path(out_dir)
        self.log = log
        self.usage = Usage(self.out / "web_usage.json")
        self.timings = []
        self._pw = self._browser = self._context = self.page = None
        self._chrome = None
        self._last = 0.0

    # ---------- browser ----------
    def start(self):
        """Open Chrome with the program's own profile and connect to it."""
        from playwright.sync_api import sync_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        chrome = find_chrome()
        if chrome and not config.WEB_USE_BUNDLED_BROWSER:
            # Normal Chrome started as a separate program; Playwright connects to it through the local debugging port.
            port = config.WEB_DEBUG_PORT
            if not self._port_open(port):
                self._chrome = subprocess.Popen(
                    [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check", *config.WEB_CHROME_ARGS, self.sel["app_url"]],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                for _ in range(60):
                    if self._port_open(port):
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"Chrome did not start ({chrome}). Close other Chrome windows that use the folder {self.profile}, or set CHROME_PATH in .env.")
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        else:
            self._context = self._pw.chromium.launch_persistent_context(str(self.profile), headless=False, accept_downloads=True, viewport=None)
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.page.set_default_timeout(15000)
        return self

    @staticmethod
    def _port_open(port):
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def close(self, keep_chrome_open=False):
        try:
            if self._chrome and not keep_chrome_open and self._browser:
                # Ask Chrome to shut down normally, so it saves the profile (your sign-in) first
                try:
                    self._browser.new_browser_cdp_session().send("Browser.close")
                    self._chrome.wait(20)
                except Exception:
                    pass
            if self._browser and self._browser.is_connected():
                self._browser.close()  # disconnects; Chrome itself stays open
            elif self._context:
                self._context.close()
        finally:
            if self._pw:
                self._pw.stop()
            if self._chrome and not keep_chrome_open and self._chrome.poll() is None:
                self._chrome.terminate()
                try:
                    self._chrome.wait(15)
                except subprocess.TimeoutExpired:
                    self._chrome.kill()

    # ---------- finding things ----------
    def _candidates(self, key, scope=None):
        root = scope or self.page
        for spec in self.sel.get(key) or []:
            if spec.startswith("role:"):
                role, _, name = spec[5:].partition("|")
                yield spec, root.get_by_role(role, name=name) if name else root.get_by_role(role)
            else:
                yield spec, root.locator(spec)

    def find(self, key, scope=None, visible=True):
        """First candidate for `key` that matches something on the page (or None)."""
        for _, loc in self._candidates(key, scope):
            try:
                if loc.count() == 0:
                    continue
                if visible:
                    for i in range(loc.count()):
                        if loc.nth(i).is_visible():
                            return loc.nth(i)
                    continue
                return loc.first
            except Exception:
                continue
        return None

    def count(self, key):
        for _, loc in self._candidates(key):
            try:
                n = loc.count()
                if n:
                    return n
            except Exception:
                pass
        return 0

    def need(self, key, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.find(key)
            if found:
                return found
            time.sleep(0.5)
        folder = self.snapshot(f"missing-{key}")
        raise ControlMissing(f"Could not find '{key}' on the Gemini page. Saved {folder}. Run 'python web.py inspect' and adjust lc_images/selectors.json.")

    def snapshot(self, reason):
        """Screenshot, page HTML and accessibility tree, for fixing selectors. Kept on your PC only."""
        folder = self.out / "debug" / f"{datetime.now():%Y%m%d-%H%M%S}-{reason}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
            (folder / "page.html").write_text(self.page.content(), encoding="utf-8")
            (folder / "aria.txt").write_text(self.page.locator("body").aria_snapshot(), encoding="utf-8")
        except Exception as error:
            (folder / "error.txt").write_text(str(error), encoding="utf-8")
        return folder

    # ---------- sign-in ----------
    def signed_in(self):
        """Signed in = no "Sign in" link, and the chat box (or your Google Account button) is there."""
        if self.find("signed_out") is not None:
            return False
        return self.find("signed_in") is not None or self.find("prompt_box") is not None

    def open_app(self):
        self.page.goto(self.sel["app_url"], wait_until="domcontentloaded")
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if self.find("prompt_box"):
                break
            time.sleep(0.5)
        time.sleep(3)  # the "Sign in" link and account button appear a moment after the chat box

    def login(self, wait_minutes=10):
        """Open Gemini and wait for you to sign in yourself in the Chrome window."""
        self.open_app()
        end = time.monotonic() + wait_minutes * 60
        told, steady = False, 0
        while time.monotonic() < end:
            # Must look signed in on three checks in a row, so a half-loaded page does not count
            steady = steady + 1 if self.signed_in() else 0
            if steady >= 3:
                time.sleep(5)  # give Chrome a moment to store the sign-in
                return True
            if not told and steady == 0:
                self.log("Sign in to Gemini in the Chrome window that opened. Waiting up to %d minutes..." % wait_minutes)
                told = True
            time.sleep(2)
        return False

    # ---------- one image ----------
    def generate(self, images, prompt, ratio):
        """Same contract as Gemini.generate: reference JPEG bytes in, generated image bytes out."""
        if config.WEB_DAILY_CAP and self.usage.today() >= config.WEB_DAILY_CAP:
            raise QuotaExhausted(f"Your own daily cap of {config.WEB_DAILY_CAP} website images is reached (WEB_DAILY_CAP).")
        gap = time.monotonic() - self._last
        if self._last and gap < config.WEB_SECONDS_BETWEEN_IMAGES:
            time.sleep(config.WEB_SECONDS_BETWEEN_IMAGES - gap)
        started = time.monotonic()
        try:
            data = self._generate(images, prompt, ratio)
        finally:
            self._last = time.monotonic()
        seconds = round(time.monotonic() - started, 1)
        self.timings.append(seconds)
        self.calls["image"] += 1
        self.usage.add()
        try:
            self._validate(data, ratio)
        except GenerationFailed:
            self._keep_rejected(data)
            raise
        self.log(f"    website image made in {seconds}s ({self.usage.today()} today)")
        return data

    def _generate(self, images, prompt, ratio):
        self.open_app()
        if not self.signed_in():
            raise NotSignedIn("Gemini is not signed in. Run: python web.py login")
        if self.sel.get("image_tool"):
            tool = self.find("image_tool")
            if tool:
                tool.click()

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, jpeg in enumerate(images):
                path = Path(tmp) / f"reference-{i + 1}.jpg"
                path.write_bytes(jpeg)
                paths.append(str(path))
            if paths:
                self._attach(paths)

            text = prompt + (config.WEB_RATIO_TEXT.format(ratio=ratio) if config.WEB_RATIO_TEXT else "")
            box = self.need("prompt_box")
            box.click()
            try:
                box.fill(text)
            except Exception:
                self.page.keyboard.insert_text(text)

            before = self.count("result_image")
            responses_before = self.count("response")
            self._send()
            return self._wait_for_image(before, responses_before)

    def _attach(self, paths):
        """Attach the photo: through Gemini's upload menu, else its file field, else by dropping it on the chat box."""
        for method in (self._attach_with_menu, self._attach_with_field, self._attach_by_drop, self._attach_by_paste):
            try:
                if not method(paths):
                    continue
            except Exception as error:
                self.log(f"    {method.__name__[1:]} did not work ({str(error)[:100]})")
                self.page.keyboard.press("Escape")
                continue
            if self._wait_for_preview(15):
                return
        folder = self.snapshot("missing-upload")
        raise ControlMissing(f"Could not attach the photo. Saved {folder}.")

    def _wait_for_preview(self, seconds):
        if not self.sel.get("attachment_preview"):
            time.sleep(3)
            return True
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.find("attachment_preview"):
                return True
            time.sleep(0.5)
        return False

    def _attach_with_menu(self, paths):
        menu = self.find("upload_menu")
        if not menu:
            return False
        with self.page.expect_file_chooser(timeout=10000) as chooser:
            menu.click()
            end = time.monotonic() + 5  # the menu takes a moment to open
            while time.monotonic() < end:
                item = self.find("upload_item")
                if item:
                    item.click()
                    break
                time.sleep(0.3)
        chooser.value.set_files(paths)
        return True

    def _attach_with_field(self, paths):
        field = self.find("file_input", visible=False)
        if not field:
            return False
        field.set_input_files(paths)
        return True

    def _drop_or_paste(self, paths, how):
        box = self.find("prompt_box")
        if not box:
            return False
        files = [{"name": Path(p).name, "data": base64.b64encode(Path(p).read_bytes()).decode()} for p in paths]
        box.evaluate(
            """(target, [files, how]) => {
                 const dt = new DataTransfer();
                 for (const f of files) {
                   const bytes = Uint8Array.from(atob(f.data), c => c.charCodeAt(0));
                   dt.items.add(new File([bytes], f.name, {type: 'image/jpeg'}));
                 }
                 if (how === 'paste') {
                   target.focus();
                   target.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: dt}));
                 } else {
                   for (const type of ['dragenter', 'dragover', 'drop'])
                     target.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: dt}));
                 }
               }""",
            [files, how],
        )
        return True

    def _attach_by_drop(self, paths):
        return self._drop_or_paste(paths, "drop")

    def _attach_by_paste(self, paths):
        return self._drop_or_paste(paths, "paste")

    def _send(self):
        end = time.monotonic() + 60
        while time.monotonic() < end:
            button = self.find("send")
            if button and button.is_enabled() and button.get_attribute("aria-disabled") != "true":
                button.click()
                return
            time.sleep(0.5)
        folder = self.snapshot("send-disabled")
        raise ControlMissing(f"The Send button did not become usable. Saved {folder}.")

    def _wait_for_image(self, images_before, responses_before):
        start = time.monotonic()
        end = start + config.WEB_TIMEOUT_SECONDS
        idle = 0
        while time.monotonic() < end:
            time.sleep(2)
            busy = self.find("stop") is not None
            if self.count("result_image") > images_before and not busy:
                time.sleep(2)  # let the full image finish loading
                return self._download()
            replied = self.count("response") > responses_before
            idle = idle + 1 if replied and not busy else 0
            # Finished without a new image (idle for three checks in a row, and not straight after sending): read why
            if idle >= 3 and time.monotonic() - start > 10:
                said = self._last_response_text()
                low = said.lower()
                if any(p in low for p in self.sel.get("limit_phrases", [])):
                    raise QuotaExhausted(f"Gemini website limit: {said[:500]}")
                raise GenerationFailed(f"Gemini website gave no image: {said[:300] or 'empty answer'}")
        self.snapshot("timeout")
        raise GenerationFailed(f"No image after {config.WEB_TIMEOUT_SECONDS}s")

    def _last_response_text(self):
        for _, loc in self._candidates("response_text"):
            try:
                if loc.count():
                    return loc.last.inner_text().strip()
            except Exception:
                pass
        for _, loc in self._candidates("response"):
            try:
                if loc.count():
                    return loc.last.inner_text().strip()
            except Exception:
                pass
        return ""

    def _download(self):
        """Full-size image through Gemini's own download button; falls back to the image shown on the page."""
        image = None
        for _, loc in self._candidates("result_image"):
            if loc.count():
                image = loc.last
                break
        if image is None:
            raise GenerationFailed("Image disappeared before download")
        try:
            image.hover()
            response = self.page.locator(self.sel["response"][0]).last if self.sel.get("response") else None
            button = self.find("download", scope=response) or self.find("download")
            if button:
                with self.page.expect_download(timeout=config.WEB_DOWNLOAD_TIMEOUT * 1000) as info:
                    button.click()
                path = info.value.path()
                if path:
                    return Path(path).read_bytes()
        except Exception as error:
            self.log(f"    download button did not work ({str(error)[:120]}); using the image on the page")
        src = image.get_attribute("src") or ""
        if src.startswith("http"):
            r = self._context.request.get(src, timeout=60000)
            if r.ok:
                return r.body()
        data = self.page.evaluate(
            """async (img) => { const r = await fetch(img.src); const b = new Uint8Array(await r.arrayBuffer());
                 let s = ''; for (let i = 0; i < b.length; i += 32768) s += String.fromCharCode(...b.subarray(i, i + 32768)); return btoa(s); }""",
            image.element_handle(),
        )
        return base64.b64decode(data)

    def _keep_rejected(self, data):
        """An image that failed the size/shape check is still saved, so a used generation is never lost."""
        folder = self.out / "web-rejected"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y%m%d-%H%M%S}.img"
        try:
            with Image.open(io.BytesIO(data)) as im:
                path = path.with_suffix("." + (im.format or "png").lower().replace("jpeg", "jpg"))
        except Exception:
            pass
        path.write_bytes(data)
        self.log(f"    kept the rejected image: {path}")

    @staticmethod
    def _validate(data, ratio):
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
        except Exception as error:
            raise GenerationFailed(f"Downloaded file is not a usable image ({error})")
        if min(width, height) < config.WEB_MIN_SIDE:
            raise GenerationFailed(f"Image too small ({width}x{height}, need at least {config.WEB_MIN_SIDE}px on the short side)")
        if ratio and abs(width / height - ratio_of(ratio)) / ratio_of(ratio) > config.WEB_RATIO_TOLERANCE:
            raise GenerationFailed(f"Wrong shape: {width}x{height}, asked for {ratio}")

    # ---------- checks go to the API ----------
    def ask_json(self, images, prompt):
        if not self.api:
            raise RuntimeError("Image checks need GEMINI_API_KEY (the free check model)")
        return self.api.ask_json(images, prompt)

    # ---------- calibration ----------
    def inspect(self):
        """Which selector candidates match on the current page. Prints names and counts only, no page text."""
        report = {}
        for key, value in self.sel.items():
            if key.startswith("_") or not isinstance(value, list) or key == "limit_phrases":
                continue
            rows = []
            for spec, loc in self._candidates(key):
                try:
                    n = loc.count()
                    shown = sum(1 for i in range(n) if loc.nth(i).is_visible())
                except Exception as error:
                    n, shown = -1, 0
                rows.append({"selector": spec, "found": n, "visible": shown})
            report[key] = rows
        return report
