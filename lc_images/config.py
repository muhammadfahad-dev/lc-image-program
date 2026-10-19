"""Settings for the LC image program. Secrets come from environment variables, never from this file."""
import os

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
