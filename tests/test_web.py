"""Website generator against a local stand-in page (tests/mock_gemini). Not the real Gemini site.

Needs Playwright and a Chrome/Chromium; skipped otherwise.  Set CHROME_PATH if Chrome is not in the usual place.
"""
import functools
import http.server
import io
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from lc_images import config
from lc_images.gemini import GenerationFailed, QuotaExhausted
from lc_images.gemini_web import GeminiWeb, find_chrome, load_selectors

try:
    import playwright  # noqa: F401
except ImportError:
    print("SKIP test_web: playwright not installed")
    sys.exit(0)
if not find_chrome():
    print("SKIP test_web: no Chrome found (set CHROME_PATH)")
    sys.exit(0)

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


handler = functools.partial(Quiet, directory=str(Path(__file__).parent / "mock_gemini"))
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}/index.html"

config.WEB_SECONDS_BETWEEN_IMAGES = 0
config.WEB_TIMEOUT_SECONDS = 40
config.WEB_CHROME_ARGS = config.WEB_CHROME_ARGS or ["--headless=new"] + (["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else [])
config.WEB_DEBUG_PORT = 9444


LOG = []


def photo():
    buf = io.BytesIO()
    Image.new("RGB", (800, 1000), (198, 167, 123)).save(buf, "JPEG")
    return buf.getvalue()


def web_for(mode, tmp):
    sel = load_selectors()
    sel["app_url"] = f"{BASE}?mode={mode}"
    return GeminiWeb(profile_dir=Path(tmp) / "profile", selectors=sel, out_dir=Path(tmp) / "out", log=LOG.append)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        web = web_for("ok", tmp).start()
        try:
            for mode, expect in [("ok", None), ("slowmenu", None), ("nomenu", None), ("limit", QuotaExhausted), ("refuse", GenerationFailed), ("square", GenerationFailed)]:
                web.sel["app_url"] = f"{BASE}?mode={mode}"
                try:
                    data = web.generate([photo()], "Make the product photo.", "4:5")
                    outcome = None
                except (QuotaExhausted, GenerationFailed) as error:
                    outcome, data = type(error), str(error)
                assert outcome is expect, f"{mode}: expected {expect}, got {outcome} {str(data)[:200]}"
                if mode in ("slowmenu", "nomenu"):
                    assert web.page.evaluate("window.lastAttached") == 1, f"{mode}: photo was not attached"
                if mode == "ok":
                    with Image.open(io.BytesIO(data)) as im:
                        assert im.size == (1024, 1280), im.size
                    assert web.page.evaluate("window.lastAttached") == 1, "photo was not attached"
                    assert "Aspect ratio: 4:5." in web.page.evaluate("window.lastPrompt"), "ratio line missing"
                    assert web.timings and web.usage.today() == 1
                    assert not list((Path(tmp) / "out").glob("web-rejected/*"))
                    assert not any("download button did not work" in line for line in LOG), LOG
                if mode == "square":
                    assert len(list((Path(tmp) / "out").glob("web-rejected/*.png"))) == 1, "rejected image was not kept"
                if mode == "limit":
                    assert "reached your limit" in data
                print(f"PASS web {mode}")
            assert web.signed_in()
            assert any(r["found"] for r in web.inspect()["prompt_box"])
            print("PASS web inspect")
        finally:
            web.close()
    server.shutdown()


if __name__ == "__main__":
    main()
