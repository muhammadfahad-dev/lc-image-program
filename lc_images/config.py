"""Settings for the LC image program. Secrets come from environment variables, never from this file."""
import os

try:  # on your PC, settings and keys are read from the .env file next to run.py
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

SHOP_DOMAIN = os.environ.get("SHOPIFY_STORE", "0anyrv-ej.myshopify.com")
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
# Apps made in the Shopify Dev Dashboard give a client ID and a client secret (shpss_...) instead of a fixed token;
# the program then asks Shopify for a fresh access token at the start of every run.
SHOPIFY_CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# Models are tried in order; the first one the key can use is chosen at start-up.
IMAGE_MODELS = [m for m in os.environ.get("IMAGE_MODELS", "gemini-3.1-flash-image,gemini-3.1-flash-image-preview,gemini-2.5-flash-image").split(",") if m]
CHECK_MODELS = [m for m in os.environ.get("CHECK_MODELS", "gemini-3-flash,gemini-3-flash-preview,gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m]
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "2K")  # falls back to the model default if refused

# Which products
PRODUCT_TYPES = {
    "Abaya", "Plain Abaya", "Printed Abaya", "Embroidered Abaya", "Black Embroidered Abaya",
    "Handwork Cutdana Abaya", "Stonework Abaya", "Stonework Embroidery Abaya",
}
PRINTED_TYPES = {"Printed Abaya"}  # no colour images: recolouring would invent a new print
COLOUR_OPTION_NAMES = {"color", "colour"}

# Limits
MAX_PRODUCTS_PER_RUN = int(os.environ.get("MAX_PRODUCTS", "25"))
RETRIES = 2  # regenerations after the first attempt
SECONDS_BETWEEN_CALLS = float(os.environ.get("SECONDS_BETWEEN_CALLS", "7"))  # stays under free-tier per-minute limits
MAX_SELECTION_PHOTOS = 8

# Shopify fields (namespace custom)
AUDIT_KEY = "image_audit"
SUMMARY_KEY = "image_run_summary"
FIELD_KEYS = {"fitting": "fitting_image", "multi": "multi_display_images", "closer": "closer_images"}

# Gemini website (python run.py --web). Used on your own PC only.
WEB_PROFILE_DIR = os.environ.get("WEB_PROFILE_DIR", "chrome-profile")  # the program's own Chrome profile (your Gemini sign-in lives here)
CHROME_PATH = os.environ.get("CHROME_PATH", "")  # empty = find Chrome in the usual places
WEB_USE_BUNDLED_BROWSER = os.environ.get("WEB_USE_BUNDLED_BROWSER", "") == "1"  # 1 = Playwright's own Chromium instead of Chrome
WEB_CHROME_ARGS = os.environ.get("WEB_CHROME_ARGS", "").split()  # extra Chrome switches, normally none
WEB_DEBUG_PORT = int(os.environ.get("WEB_DEBUG_PORT", "9333"))  # local only, to talk to the Chrome window
WEB_SELECTORS = os.environ.get("WEB_SELECTORS", "")  # empty = lc_images/selectors.json
WEB_SECONDS_BETWEEN_IMAGES = float(os.environ.get("WEB_SECONDS_BETWEEN_IMAGES", "10"))
# Where photo choice and image checks run: "web" = the Gemini website (free, no key needed), "api" = GEMINI_API_KEY
CHECK_SOURCE = os.environ.get("CHECK_SOURCE", "web").strip().lower()
WEB_SECONDS_BEFORE_CHECK = float(os.environ.get("WEB_SECONDS_BEFORE_CHECK", "3"))
# One chat per product for its images, and one shared chat for all checks (a fresh one after this many checks,
# because a very long chat makes the page slow). A passing Gemini error is retried in the same chat first.
WEB_CHECK_CHAT_MAX = int(os.environ.get("WEB_CHECK_CHAT_MAX", "40"))
WEB_SAME_CHAT_RETRIES = int(os.environ.get("WEB_SAME_CHAT_RETRIES", "2"))
# Put before each image prompt, because several images share one chat
WEB_IMAGE_INTRO = os.environ.get("WEB_IMAGE_INTRO", "New image. Use only the photo attached to this message as the reference; ignore the earlier images in this chat.")
WEB_TIMEOUT_SECONDS = int(os.environ.get("WEB_TIMEOUT_SECONDS", "240"))
WEB_DOWNLOAD_TIMEOUT = int(os.environ.get("WEB_DOWNLOAD_TIMEOUT", "90"))
WEB_DAILY_CAP = int(os.environ.get("WEB_DAILY_CAP", "0"))  # your own limit per day; 0 = stop only when Gemini says so
WEB_MIN_SIDE = int(os.environ.get("WEB_MIN_SIDE", "768"))  # smallest acceptable short side; the website gives about 1 megapixel (928x1152 for 4:5)
WEB_RATIO_TOLERANCE = float(os.environ.get("WEB_RATIO_TOLERANCE", "0.05"))
# The website has no aspect-ratio setting and sometimes makes several images, so this line is added after each prompt.
# Set it empty to add nothing.
WEB_RATIO_TEXT = os.environ.get("WEB_RATIO_TEXT", "\n\nAspect ratio: {ratio}. Make only one image.").replace("\\n", "\n")
TEMPORARY_RETRIES = int(os.environ.get("TEMPORARY_RETRIES", "2"))  # extra tries after "Gemini had an error", not counted as failed attempts
TEMPORARY_WAIT_SECONDS = float(os.environ.get("TEMPORARY_WAIT_SECONDS", "60"))

# Vertex AI (python run.py --vertex): billed to your Google Cloud project, signed in with
# "gcloud auth application-default login" (no API key). See lc_images/vertex.py for the setup steps.
VERTEX_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "global")
VERTEX_IMAGE_MODEL = os.environ.get("VERTEX_IMAGE_MODEL", "gemini-2.5-flash-image")  # Google's own model; takes the product photo
VERTEX_CHECK_MODEL = os.environ.get("VERTEX_CHECK_MODEL", "gemini-2.5-flash")
VERTEX_BUDGET_USD = float(os.environ.get("VERTEX_BUDGET_USD", "280"))  # the program stops before its estimate passes this
# Estimates used for the cap; check them against Google's Vertex AI price list and Billing reports
VERTEX_PRICE_PER_IMAGE = float(os.environ.get("VERTEX_PRICE_PER_IMAGE", "0.04"))
VERTEX_PRICE_PER_CHECK = float(os.environ.get("VERTEX_PRICE_PER_CHECK", "0.003"))
VERTEX_SECONDS_BETWEEN_CALLS = float(os.environ.get("VERTEX_SECONDS_BETWEEN_CALLS", "4"))
VERTEX_TIMEOUT_SECONDS = int(os.environ.get("VERTEX_TIMEOUT_SECONDS", "150"))
# Waits after "too many requests" before trying again; after the last one the run stops (and continues next time)
VERTEX_RATE_LIMIT_WAITS = [int(x) for x in os.environ.get("VERTEX_RATE_LIMIT_WAITS", "15,30,60,60,90,120").split(",")]
