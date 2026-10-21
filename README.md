# LC image program

Makes the product page images for LC Modest Wear abayas with Gemini, checks them, uploads them to Shopify,
and makes single-colour variant images linked to their colour.

## What it does, per product
1. Picks the clearest full-length front photo from the product's own photos (Gemini looks at them).
2. Makes the listing images from the prompt file, word for word:
   - Media 1.1, 1.2, 1.3 and 1.4 for front-open abayas; 1.1, 1.2 and 1.4 for closed ones.
   - Fitting image 2.1.
   - Multi display 3.1, 3.2, and 3.3 for plain abayas or 3.4 for decorated ones.
   - A little closer 4.1 to 4.4.
3. Gemini checks every image against the real photo. A failed image is made again, up to 2 more times.
   If it still fails, it is marked **Do manually** and nothing is uploaded for it.
4. Uploads the passed images. New Media images go first; your own photos stay, after them.
   An image field is filled only when all its images passed.
5. Colour images, single colours only (mixed, ombré, patterns, embroidery/stonework options and printed abayas are skipped):
   uses an existing photo of that colour if there is one, otherwise recolours the new front image to the colour name,
   checks it, and links it to every size of that colour.
6. Saves its progress on the product (field "Image automation record"), so nothing is done twice and a stopped run continues.

Products whose Fitting, Multi display and A little closer fields are already filled are treated as done by hand:
only the colour step runs for them.

## One-time setup (about 15 minutes)
1. **Create a GitHub repository** and upload this folder to it.
   A *public* repository gets unlimited free run time. A *private* one gets 2,000 free minutes a month,
   enough for about 1 hour a day (set MAX_PRODUCTS to 10).
   The keys are never visible in either: they are stored as secrets.
2. **Shopify access.** In Shopify admin go to Settings → Apps → Develop apps, create an app, and give it these
   Admin API permissions: `read_products`, `write_products`, `read_files`, `write_files`. Install it and copy the
   Admin API access token (it starts with `shpat_`).
   If your admin no longer offers "Develop apps", create the app in the Shopify Dev Dashboard and install it on the store.
3. **Add the secrets** in the repository: Settings → Secrets and variables → Actions → New repository secret:
   - `GEMINI_API_KEY`: your Gemini key
   - `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` (starts with `shpss_`) from the app's settings, **or**
     `SHOPIFY_ADMIN_TOKEN` (starts with `shpat_`) if your app shows an Admin API access token instead.
4. Optional repository *variables* (same page, Variables tab): `MAX_PRODUCTS` (default 25).

## Running it
Open the **Actions** tab → **LC images** → **Run workflow**, and pick a mode:
- **test-key**: makes one test image to prove the Gemini key can generate images. Do this first.
- **pilot**: makes all images for up to 5 products (or the handles you type) and **changes nothing in Shopify**.
  When it finishes, download the "lc-images-…" file from the run page and open `index.html` to see every image.
- **live**: does the real work on the store.

When the pilot looks right, add the repository variable `LIVE` = `true`. From then on the program runs every day
by itself at 08:20 UTC. It stops cleanly when Gemini's daily free limit is used up and continues the next day.

## Running on your Windows PC with the Gemini website
The Gemini API's free tier gives no images, so the images can be made on gemini.google.com instead, on your own PC,
signed in with your own Google account. The program opens a separate Chrome window with its own profile folder
(`chrome-profile`), so your normal Chrome is not touched. Each product gets **one chat** for all its images (first tab),
and all checks go to **one shared check chat** (second tab; a fresh one every 40 checks so the page stays fast).
A photo is only sent once Gemini shows it attached; if it cannot be attached, the run stops instead of sending a prompt
without the photo. A passing Gemini error ("I encountered an error...") is asked again in the same chat, then in a new one.
It waits 10 seconds between images and stops as soon as Gemini says a limit is reached; progress is saved on every product,
so the next run continues where it stopped. It does not deal with CAPTCHAs or sign-in checks.

Photo choice and the image checks also run on the website, each in its own new chat with the photos attached,
because the free API key allows only about 20 checks a day. (Set `CHECK_SOURCE=api` in `.env` to use the key instead.)
A check fails an image only for a major problem: a different garment, length or cut, design work missing or added,
the wrong colour, text or a watermark, or a clearly deformed person. Small simplifications do not fail it.

### One-time setup
1. Install **Python 3.12** from python.org (tick "Add python.exe to PATH") and **Git** from git-scm.com. Google Chrome must be installed.
2. Open **Command Prompt** and run:
   ```
   cd %USERPROFILE%\Documents
   git clone https://github.com/muhammadfahad-dev/lc-image-program.git
   cd lc-image-program
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env
   notepad .env
   ```
   In Notepad, fill in `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` and `GEMINI_API_KEY`, then save. These stay on your PC.
3. Sign in to Gemini (once): `python web.py login`. A Chrome window opens on Gemini: sign in yourself.
   Expected: `OK: signed in to Gemini.` The window closes; the sign-in is remembered in `chrome-profile`.

Every time you open a new Command Prompt, first run `cd %USERPROFILE%\Documents\lc-image-program` and `.venv\Scripts\activate`.
To get program updates: `git pull`, then `pip install -r requirements.txt`.

### Step by step
1. `python web.py inspect`: shows `OK` or `--` for every control the program looks for. Expected `OK` for at least
   `prompt_box`, `upload_menu` (or `file_input`) and `send`. Add `--save` to keep a screenshot in `output\debug`.
2. `python web.py test --handle <product-handle>`: makes **one** image (Media 1.1) from the product's first photo and
   **changes nothing in Shopify**. Expected, for example: `OK: image made in 48.2s, 896x1152 PNG ...` and a file in `output\web-test\<handle>`.
   Without Shopify: `python web.py test --image C:\path\photo.jpg`.
3. Pilot of one product, all images, nothing changed in Shopify: `python run.py --web --dry-run --handles <handle>`.
   Open `output\index.html` to see every image.
4. Live: `python run.py --web --limit 5`, and more once the results look right.

**Making images again.** `python run.py --web --redo closer --handles <handle>` makes that product's
"A little closer" images again (also: `media`, `fitting`, `multi`, or single images like `closer-1,closer-3`).
The old ones leave the product page but stay in Shopify Files. `--retry-manual` retries the "do manually" images.

**Stopping and continuing.** You can stop at any time: press Ctrl+C, or close Chrome, or let the PC sleep.
Everything made so far is kept (live runs on the product in Shopify, pilots in `output\<handle>\report.json`).
Run the same command again and it continues from the next image. To redo a pilot from scratch, rename its folder in `output`.

If something fails, the message says what. When a control could not be found, a folder in `output\debug` holds a screenshot;
`lc_images\selectors.json` says where each control is and can be adjusted without changing the program.
`output\debug` can show your account name: do not share it publicly.

## Vertex AI (Google Cloud free-trial credit)
Images and checks through Vertex AI in your own Google Cloud project, signed in with your Google account
(no API key). It uses Google's own Gemini image model, which takes the product photo as its reference.
Which money pays (free credit, prepaid balance, card) is decided by Google Billing, not by the program;
Google uses the credit first for the services it covers. The program stops by itself before its own
estimate passes `VERTEX_BUDGET_USD` ($280 by default) and keeps the running total in `output\vertex_spend.json`.

One-time setup:
1. Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install (Windows installer), then open a new Command Prompt.
2. Run, one at a time (use your project ID):
   ```
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   gcloud auth application-default login
   gcloud auth application-default set-quota-project YOUR_PROJECT_ID
   gcloud services enable aiplatform.googleapis.com
   ```
3. In `.env` add `GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID`, then `pip install -r requirements.txt`.
4. `python run.py --vertex --test-key` makes ONE test image (about $0.04). The next day, check in Google Cloud
   (Billing → Reports, "Credits" column) that it was paid by the free-trial credit before running more.
5. Then `python run.py --vertex --dry-run --handles <handle>` (nothing changes in Shopify), and live runs with `--vertex`.

## Reports
- The **Image tracker** page shows every product's status, what still needs doing by hand, and the last run's summary.
- GitHub emails you if a daily run fails completely.

## Tests
`python tests/test_pipeline.py` and `python tests/test_gemini.py` run without any keys (fake Gemini and Shopify).
`python tests/test_web.py` runs the website steps against a stand-in page in `tests/mock_gemini` (needs Chrome; not the real Gemini site).
