"""Settings for the LC image program. Secrets come from environment variables, never from this file."""
import os

try:  # on your PC, settings and keys are read from the .env file next to run.py
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def env(name, default=""):
    """A setting from the environment or .env; an empty value (as GitHub passes for unset variables) means the default."""
    value = os.environ.get(name, "").strip()
    return value if value else default

SHOP_DOMAIN = env("SHOPIFY_STORE", "0anyrv-ej.myshopify.com")
SHOPIFY_TOKEN = env("SHOPIFY_ADMIN_TOKEN", "")
# Apps made in the Shopify Dev Dashboard give a client ID and a client secret (shpss_...) instead of a fixed token;
# the program then asks Shopify for a fresh access token at the start of every run.
SHOPIFY_CLIENT_ID = env("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = env("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_API_VERSION = env("SHOPIFY_API_VERSION", "2026-07")
GEMINI_KEY = env("GEMINI_API_KEY", "")

# Models are tried in order; the first one the key can use is chosen at start-up.
IMAGE_MODELS = [m for m in env("IMAGE_MODELS", "gemini-3.1-flash-image,gemini-3.1-flash-image-preview,gemini-2.5-flash-image").split(",") if m]
CHECK_MODELS = [m for m in env("CHECK_MODELS", "gemini-3-flash,gemini-3-flash-preview,gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m]
IMAGE_SIZE = env("IMAGE_SIZE", "2K")  # falls back to the model default if refused

# Which products
PRODUCT_TYPES = {
    "Abaya", "Plain Abaya", "Printed Abaya", "Embroidered Abaya", "Black Embroidered Abaya",
    "Handwork Cutdana Abaya", "Stonework Abaya", "Stonework Embroidery Abaya",
}
PRINTED_TYPES = {"Printed Abaya"}  # no colour images: recolouring would invent a new print
COLOUR_OPTION_NAMES = {"color", "colour"}

# Limits
MAX_PRODUCTS_PER_RUN = int(env("MAX_PRODUCTS", "25"))
RETRIES = 2  # regenerations after the first attempt
SECONDS_BETWEEN_CALLS = float(env("SECONDS_BETWEEN_CALLS", "7"))  # stays under free-tier per-minute limits
MAX_SELECTION_PHOTOS = 8
UNCHECKED_DIR = env("UNCHECKED_DIR", "output/unchecked")  # images made but not yet checked (kept across a stop)

# Shopify fields (namespace custom)
AUDIT_KEY = "image_audit"
SUMMARY_KEY = "image_run_summary"
FIELD_KEYS = {"fitting": "fitting_image", "multi": "multi_display_images", "closer": "closer_images"}

# Gemini website (python run.py --web). Used on your own PC only.
WEB_PROFILE_DIR = env("WEB_PROFILE_DIR", "chrome-profile")  # the program's own Chrome profile (your Gemini sign-in lives here)
CHROME_PATH = env("CHROME_PATH", "")  # empty = find Chrome in the usual places
WEB_USE_BUNDLED_BROWSER = env("WEB_USE_BUNDLED_BROWSER", "") == "1"  # 1 = Playwright's own Chromium instead of Chrome
WEB_CHROME_ARGS = env("WEB_CHROME_ARGS", "").split()  # extra Chrome switches, normally none
WEB_DEBUG_PORT = int(env("WEB_DEBUG_PORT", "9333"))  # local only, to talk to the Chrome window
WEB_SELECTORS = env("WEB_SELECTORS", "")  # empty = lc_images/selectors.json
WEB_SECONDS_BETWEEN_IMAGES = float(env("WEB_SECONDS_BETWEEN_IMAGES", "10"))
# Where photo choice and image checks run: "web" = the Gemini website (free, no key needed), "api" = GEMINI_API_KEY
CHECK_SOURCE = env("CHECK_SOURCE", "web").strip().lower()
WEB_SECONDS_BEFORE_CHECK = float(env("WEB_SECONDS_BEFORE_CHECK", "3"))
# One chat per product for its images, and one shared chat for all checks (a fresh one after this many checks,
# because a very long chat makes the page slow). A passing Gemini error is retried in the same chat first.
WEB_CHECK_CHAT_MAX = int(env("WEB_CHECK_CHAT_MAX", "40"))
WEB_SAME_CHAT_RETRIES = int(env("WEB_SAME_CHAT_RETRIES", "2"))
# Put before each image prompt, because several images share one chat
WEB_IMAGE_INTRO = os.environ.get("WEB_IMAGE_INTRO", "New image. Use only the photo attached to this message as the reference; ignore the earlier images in this chat.")
WEB_TIMEOUT_SECONDS = int(env("WEB_TIMEOUT_SECONDS", "240"))
WEB_DOWNLOAD_TIMEOUT = int(env("WEB_DOWNLOAD_TIMEOUT", "90"))
WEB_DAILY_CAP = int(env("WEB_DAILY_CAP", "0"))  # your own limit per day; 0 = stop only when Gemini says so
WEB_MIN_SIDE = int(env("WEB_MIN_SIDE", "768"))  # smallest acceptable short side; the website gives about 1 megapixel (928x1152 for 4:5)
WEB_RATIO_TOLERANCE = float(env("WEB_RATIO_TOLERANCE", "0.05"))
# The website has no aspect-ratio setting and sometimes makes several images, so this line is added after each prompt.
# Set it empty to add nothing.
WEB_RATIO_TEXT = os.environ.get("WEB_RATIO_TEXT", "\n\nAspect ratio: {ratio}. Make only one image.").replace("\\n", "\n")
TEMPORARY_RETRIES = int(env("TEMPORARY_RETRIES", "2"))  # extra tries after "Gemini had an error", not counted as failed attempts
TEMPORARY_WAIT_SECONDS = float(env("TEMPORARY_WAIT_SECONDS", "60"))

# Vertex AI (python run.py --vertex): billed to your Google Cloud project, signed in with
# "gcloud auth application-default login" (no API key). See lc_images/vertex.py for the setup steps.
VERTEX_PROJECT = env("GOOGLE_CLOUD_PROJECT", "")
VERTEX_LOCATION = env("VERTEX_LOCATION", "global")
VERTEX_IMAGE_MODEL = env("VERTEX_IMAGE_MODEL", "gemini-nano-banana-2.1")  # Google's own model; takes the product photo
VERTEX_CHECK_MODEL = env("VERTEX_CHECK_MODEL", "gemini-3.5-flash")  # 3.8 Flash was often busy (429); 3.5 answered at once
# The program stops before its estimate passes this. Its estimate ran above Google's real charge (Billing,
# 23 Oct: about $36-43 real against $57 estimated), so 290 still stops before the $300 free credit is used up.
VERTEX_BUDGET_USD = float(env("VERTEX_BUDGET_USD", "290"))
# Estimates used for the cap; check them against Google's Vertex AI price list and Billing reports
VERTEX_PRICE_PER_IMAGE = float(env("VERTEX_PRICE_PER_IMAGE", "0.036"))  # Nano Banana 2.1, 1K image + photo
# About $0.009 a check in Billing with full picture detail and thinking; less with the settings below
VERTEX_PRICE_PER_CHECK = float(env("VERTEX_PRICE_PER_CHECK", "0.006"))
# Checks look at the pictures in medium detail (enough for colour, design work and the garment's shape) and think
# briefly: these two were most of a check's cost
VERTEX_CHECK_MEDIA_RESOLUTION = env("VERTEX_CHECK_MEDIA_RESOLUTION", "medium")  # low, medium or high
VERTEX_CHECK_THINKING = env("VERTEX_CHECK_THINKING", "low")  # minimal, low, medium or high
VERTEX_SECONDS_BETWEEN_CALLS = float(env("VERTEX_SECONDS_BETWEEN_CALLS", "4"))
# A parallel worker (--minutes) that Google keeps turning away waits this long, then goes on with the next product
WORKER_BUSY_WAIT_SECONDS = int(env("WORKER_BUSY_WAIT_SECONDS", "300"))
VERTEX_TIMEOUT_SECONDS = int(env("VERTEX_TIMEOUT_SECONDS", "120"))
# Waits after "too many requests" before trying again; after the last one the run stops (and continues next time)
VERTEX_RATE_LIMIT_WAITS = [int(x) for x in env("VERTEX_RATE_LIMIT_WAITS", "15,30,60,60,90,120").split(",")]
# When the check model stays busy, these are tried in turn (each with shorter waits) before the run stops
VERTEX_CHECK_BACKUPS = [m for m in env("VERTEX_CHECK_BACKUPS", "gemini-2.5-flash,gemini-3.8-flash").split(",") if m]
# A busy check model is only waited for briefly before a backup model takes over
VERTEX_CHECK_WAITS = [int(x) for x in env("VERTEX_CHECK_WAITS", "10,20").split(",")]
VERTEX_BACKUP_WAITS = [int(x) for x in env("VERTEX_BACKUP_WAITS", "15,30,60").split(",")]

# Report (python run.py --report, and after every live run when REPORT_SHEET_ID is set)
STORE_URL = env("STORE_URL", "wearlcm.com")
REPORT_SHEET_ID = env("REPORT_SHEET_ID", "")  # the long part of the Sheet's address, between /d/ and /edit
REPORT_SHEET_TAB = env("REPORT_SHEET_TAB", "Image report")
REPORT_ALL_PRODUCTS = env("REPORT_ALL_PRODUCTS", "") == "1"  # 1 = every active product, not only the ones worked on
# On your PC: the service account the report writes the Sheet as (gcloud cannot get Sheets permission itself)
REPORT_SERVICE_ACCOUNT = env("REPORT_SERVICE_ACCOUNT", "")

# Batch mode (python run.py --vertex --batch submit|collect|auto)
VERTEX_BUCKET = env("VERTEX_BUCKET", "")  # Cloud Storage bucket for the request lists and answers
VERTEX_BATCH_SIZE = int(env("VERTEX_BATCH_SIZE", "50"))  # most images in one Batch job; start small
VERTEX_BATCH_PRICE_PER_IMAGE = float(env("VERTEX_BATCH_PRICE_PER_IMAGE", str(round(VERTEX_PRICE_PER_IMAGE / 2, 4))))
VERTEX_BATCH_MAX_READS = int(env("VERTEX_BATCH_MAX_READS", "3"))  # a job that cannot be finished is dropped after this many reads
VERTEX_BATCH_MAX_JOBS = int(env("VERTEX_BATCH_MAX_JOBS", "2"))  # Batch jobs out at the same time
