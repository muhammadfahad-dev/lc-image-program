"""Image generation through the Gemini website (gemini.google.com), driven by Playwright on your own PC.

You sign in yourself, in a normal Chrome window the program opens with its own profile folder.
Each product gets one chat for its images (in the first tab); all image checks go to one shared chat
(in a second tab), which is replaced by a fresh one every WEB_CHECK_CHAT_MAX checks. A passing Gemini
error is asked again in the same chat first. A photo is only ever sent once Gemini shows it attached.
The program does not hide that it is automated, does not handle CAPTCHAs, and stops (QuotaExhausted)
as soon as Gemini says a limit is reached; progress is kept and the next run continues.
"""
import base64
import io
import json
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from PIL import Image

from . import config
from .gemini import GenerationFailed, QuotaExhausted, TemporaryFailure

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


def parse_json(text):
    """The JSON object in a website answer (which may be wrapped in ``` fences or have words around it), or None."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
        self.gen_page = self.check_page = None
        self.gen_url = self.check_url = None  # the current image chat and check chat (None = start a new one)
        self.check_count = 0
        self._attach_order = None  # the way of attaching that worked last time is tried first

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
        self.gen_page = self.page
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

    # ---------- tabs and chats ----------
    @contextmanager
    def _on(self, page):
        saved, self.page = self.page, page
        try:
            yield
        finally:
            self.page = saved

    def _check_tab(self):
        if self.check_page is None or self.check_page.is_closed():
            self.check_page = self._context.new_page()
            self.check_page.set_default_timeout(15000)
            self.check_url, self.check_count = None, 0
        return self.check_page

    def start_product(self, key=None):
        """Each product gets its own image chat."""
        self.gen_url = None

    def _go_to_chat(self, url):
        """Open this chat, or a new chat when url is None. Returns True for a new chat."""
        if url is None:
            self.open_app()
            if not self.signed_in():
                raise NotSignedIn("Gemini is not signed in. Run: python web.py login")
            return True
        if self.page.url != url:
            self.page.goto(url, wait_until="domcontentloaded")
            self.need("prompt_box", 30)
            time.sleep(2)
        return False

    def _current_chat(self):
        """The address of the chat on screen, or None if Gemini gave it none (then the next message starts a new chat)."""
        url = self.page.url
        return None if url.rstrip("/") == self.sel["app_url"].rstrip("/") else url

    def _send_message(self, images, file_name, text):
        """Attach the images, type the message and send it. Returns the page counts from before sending."""
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, jpeg in enumerate(images):
                path = Path(tmp) / file_name.format(n=i + 1, i=i)
                path.write_bytes(jpeg)
                paths.append(str(path))
            if paths:
                self._attach(paths)
            box = self.need("prompt_box")
            box.click()
            try:
                box.fill(text)
            except Exception:
                self.page.keyboard.insert_text(text)
            before = (self.count("result_image"), self.count("response"))
            self._send()
        return before

    def _generate(self, images, prompt, ratio):
        text = prompt + (config.WEB_RATIO_TEXT.format(ratio=ratio) if config.WEB_RATIO_TEXT else "")
        if config.WEB_IMAGE_INTRO:
            text = config.WEB_IMAGE_INTRO + "\n\n" + text
        with self._on(self.gen_page):
            for attempt in range(1 + config.WEB_SAME_CHAT_RETRIES):
                if self._go_to_chat(self.gen_url) and self.sel.get("image_tool"):
                    tool = self.find("image_tool")
                    if tool:
                        tool.click()
                images_before, responses_before = self._send_message(images, "reference-{n}.jpg", text)
                try:
                    data = self._wait_for_image(images_before, responses_before)
                    self.gen_url = self._current_chat()
                    return data
                except TemporaryFailure as error:
                    self.gen_url = self._current_chat()
                    if attempt < config.WEB_SAME_CHAT_RETRIES:
                        self.log(f"    {str(error)[:120]}; asking again in the same chat")
                        time.sleep(10)
                        continue
                    self.gen_url = None  # it kept failing here: the next try starts a new chat
                    raise
                except GenerationFailed:
                    self.gen_url = None  # a refusal can colour the rest of the chat: start a new one
                    raise

    def _composer(self):
        """The chat box area (box, upload button, attached photos), so photos already sent in the chat are not counted."""
        box = self.find("prompt_box")
        if box is None:
            return None
        low = "translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
        area = box.locator(f"xpath=ancestor::*[.//button[contains({low},'upload') or contains({low},'add files')]][1]")
        try:
            return area if area.count() else None
        except Exception:
            return None

    def _attach(self, paths):
        """Attach the photos exactly once, and only go on once Gemini shows them attached.
        If a way of attaching shows nothing, the unsent message is discarded (page reload) before the next way is tried."""
        if self._preview_count():
            self.page.reload(wait_until="domcontentloaded")  # leftover photos from a stopped run
            self.need("prompt_box", 30)
            if self._preview_count():
                folder = self.snapshot("leftover-photos")
                raise ControlMissing(f"The chat box already has photos attached. Remove them in the Chrome window. Saved {folder}.")
        methods = [self._attach_with_menu, self._attach_with_field, self._attach_by_drop, self._attach_by_paste]
        if self._attach_order:
            methods.sort(key=lambda m: m.__name__ != self._attach_order)
        for method in methods:
            try:
                tried = method(paths)
            except Exception as error:
                self.log(f"    {method.__name__[1:]} did not work ({str(error).splitlines()[0][:100]})")
                self.page.keyboard.press("Escape")
                tried = True  # it may have attached anyway: the preview decides
            if not tried:
                continue
            shown = self._wait_for_preview(len(paths), 15)
            if shown == len(paths):
                self._attach_order = method.__name__
                return
            if shown > len(paths):
                self.snapshot("too-many-photos")
            # Nothing (or the wrong number) attached: discard the unsent message and try the next way
            self.page.reload(wait_until="domcontentloaded")
            self.need("prompt_box", 30)
            time.sleep(1)
        folder = self.snapshot("missing-upload")
        raise ControlMissing(
            f"Gemini did not show the photo as attached, so nothing was sent. Saved {folder}. "
            "Run 'python web.py upload-check' and send the result, so the program can be matched to Gemini's upload."
        )

    def _preview_count(self):
        """How many attached, unsent photos the chat box shows (0 if none, or if the preview cannot be recognised)."""
        scope = self._composer()
        for _, loc in self._candidates("attachment_preview", scope):
            try:
                n = sum(1 for i in range(loc.count()) if loc.nth(i).is_visible())
            except Exception:
                continue
            if n:
                return n
        return 0

    def _wait_for_preview(self, expected, seconds):
        """Wait until the attached photos show; returns how many are shown."""
        end = time.monotonic() + seconds
        shown = 0
        while time.monotonic() < end:
            shown = self._preview_count()
            if shown >= expected:
                time.sleep(1)
                return self._preview_count()
            time.sleep(0.5)
        return shown

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
                return self._download(images_before)
            replied = self.count("response") > responses_before
            idle = idle + 1 if replied and not busy else 0
            # Finished without a new image (idle for three checks in a row, and not straight after sending): read why
            if idle >= 3 and time.monotonic() - start > 10:
                said = self._last_response_text()
                low = said.lower()
                if any(p in low for p in self.sel.get("limit_phrases", [])):
                    raise QuotaExhausted(f"Gemini website limit: {said[:500]}")
                if not said or any(p in low for p in self.sel.get("retry_phrases", [])):
                    raise TemporaryFailure(f"Gemini website had a problem: {said[:300] or 'empty answer'}")
                raise GenerationFailed(f"Gemini website gave no image: {said[:300]}")
        self.snapshot("timeout")
        raise GenerationFailed(f"No image after {config.WEB_TIMEOUT_SECONDS}s")

    def _wait_for_text(self, responses_before):
        start = time.monotonic()
        end = start + config.WEB_TIMEOUT_SECONDS
        idle = 0
        while time.monotonic() < end:
            time.sleep(2)
            busy = self.find("stop") is not None
            replied = self.count("response") > responses_before
            idle = idle + 1 if replied and not busy else 0
            if idle >= 2 and time.monotonic() - start > 6:
                said = self._last_response_text()
                if said:
                    return said
        self.snapshot("timeout")
        raise RuntimeError(f"No answer from Gemini after {config.WEB_TIMEOUT_SECONDS}s")

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

    def _download(self, images_before):
        """The FIRST new image of the answer (Gemini sometimes makes several), full size through its own download
        button; falls back to the image shown on the page."""
        image, made = None, 0
        for _, loc in self._candidates("result_image"):
            n = loc.count()
            if n > images_before:
                image, made = loc.nth(images_before), n - images_before
                break
        if image is None:
            raise GenerationFailed("Image disappeared before download")
        if made > 1:
            self.log(f"    Gemini made {made} images in one answer; only the first is used")
        try:
            image.scroll_into_view_if_needed()
            image.hover()
            # This image's own download button: look in the image's frame first, then in the answer
            frame = image.locator("xpath=ancestor::*[self::generated-image or self::single-image][1]")
            button = self.find("download", scope=frame) if frame.count() else None
            if button is None:
                response = self.page.locator(self.sel["response"][0]).last if self.sel.get("response") else None
                button = self.find("download", scope=response)
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

    # ---------- photo choice and checks ----------
    def ask_json(self, images, prompt):
        """Same contract as Gemini.ask_json. In the shared check chat (CHECK_SOURCE=web) or through the API key (CHECK_SOURCE=api)."""
        if config.CHECK_SOURCE == "api":
            if not self.api:
                raise RuntimeError("CHECK_SOURCE=api needs GEMINI_API_KEY")
            return self.api.ask_json(images, prompt)
        gap = time.monotonic() - self._last
        if self._last and gap < config.WEB_SECONDS_BEFORE_CHECK:
            time.sleep(config.WEB_SECONDS_BEFORE_CHECK - gap)
        names = ", ".join(f"Image {i} (file Image-{i}.jpg)" for i in range(len(images)))
        text = (
            f"New question, separate from the earlier ones in this chat. Look only at the images attached to this message: {names}.\n\n"
            f"{prompt}\n\nReply with only the JSON object and nothing else. "
            'Include in it "images_seen": the number of images attached to this message.'
        )
        said = ""
        try:
            with self._on(self._check_tab()):
                for attempt in range(1 + config.WEB_SAME_CHAT_RETRIES):
                    if self.check_count >= config.WEB_CHECK_CHAT_MAX:
                        self.check_url = None
                    if self._go_to_chat(self.check_url):
                        self.check_count = 0
                    _, responses_before = self._send_message(images, "Image-{i}.jpg", text)
                    said = self._wait_for_text(responses_before)
                    self.check_url = self._current_chat()
                    self.check_count += 1
                    answer = parse_json(said)
                    if answer is None:
                        if any(p in said.lower() for p in self.sel.get("limit_phrases", [])):
                            raise QuotaExhausted(f"Gemini website limit: {said[:500]}")
                        self.log(f"    check answer was not JSON ({said[:80]}); asking again")
                        continue
                    seen = answer.get("images_seen")
                    if isinstance(seen, int) and seen < len(images):
                        self.log(f"    Gemini says it got {seen} of {len(images)} images; asking again in a new chat")
                        self.check_url = None
                        continue
                    self.calls["check"] += 1
                    return answer
            raise ControlMissing(f"Gemini did not give a usable check answer: {said[:200]}")
        finally:
            self._last = time.monotonic()

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
