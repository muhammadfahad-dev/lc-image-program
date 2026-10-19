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

## Reports
- The **Image tracker** page shows every product's status, what still needs doing by hand, and the last run's summary.
- GitHub emails you if a daily run fails completely.

## Tests
`python tests/test_pipeline.py` and `python tests/test_gemini.py` run without any keys (fake Gemini and Shopify).
