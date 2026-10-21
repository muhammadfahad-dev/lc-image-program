# How the LC image program works

This document describes every part of the program in plain words: what each file and function does,
what it changes in Shopify, and when it stops. Use it to decide what else should change.

---

## 1. The whole job, start to finish

For each abaya:

1. **Choose the reference photo.** The check model looks at up to 8 of the product's own photos.
   It picks the clearest full-length front photo. It also says:
   - front-open or closed;
   - plain or with design work;
   - which colour that photo shows;
   - which colours have a photo of their own (that abaya **alone**, never a group or rack photo).
2. **Make the listing images** with your prompts, word for word:

   | Section | Images | Shape |
   |---|---|---|
   | Media | 1.1, 1.2, 1.3, 1.4 (closed abayas: 1.1, 1.2, 1.4) | 4:5 |
   | Fitting | 2.1 | 4:5 |
   | Multi display | 3.1, 3.2, plus 3.3 (plain) or 3.4 (decorated) | 2:3 |
   | A little closer | 4.1 (4:5), 4.2 (1:1), 4.3 (2:3), 4.4 (16:9) | as listed |

   For closed abayas, 3.1 and 3.2 get a short note ("show the simple front view" / "a three-quarter view
   instead of worn open"), and the checker is told the same note.
3. **Check every image** against the reference photo. An image **fails only for a major problem**:
   - a different garment, length or cut;
   - a person or mannequin wearing some other dress;
   - design work missing, moved or added;
   - a fabric or detail the abaya does not have;
   - the wrong colour;
   - text, a logo or a watermark;
   - a clearly deformed person;
   - the brief clearly ignored;
   - **the same view as another image in the same section**.

   It does **not** fail for small things: slightly simplified beadwork, small drape or framing differences,
   imperfect hands, or accessories.
4. **Try again.** An image is tried up to **3 times**. A passing Google error ("too many requests",
   "server busy", no answer) does not use up a try.
5. **Place the images in Shopify:**

   | Section | What happens |
   |---|---|
   | Media | The passed images go **first**, in order. Your own photos stay, after them. Nothing is deleted. |
   | Fitting, Multi display, A little closer | Each position is filled. Where an image failed every try, a **black placeholder** keeps the order. |

   A section filled **by hand** is never touched.
6. **Colour images** (single colours only, once the Media section is finished):

   | Colour | What happens |
   |---|---|
   | Mixed colour (Black-Pink, "with", ombré, multi) | Skipped |
   | Pattern (floral, checkered, geometric) | Skipped |
   | Any colour of a printed abaya | Skipped |
   | Already has a variant image | Left alone |
   | Has its own product photo | That photo is linked to every size |
   | Same colour as the new front image | The new front image is linked |
   | Anything else | Image 1.1 is recoloured to the colour name, checked, added to Media, and linked to every size |

7. **Save progress after every image**, in the product's field "Image automation record" (`custom.image_audit`).
   A stopped run continues from the same place next time.

---

## 2. Ways to run it

| Command | What it does | Changes Shopify? | Cost |
|---|---|---|---|
| `python run.py --vertex --test-key` | One test image through Vertex AI | No | ~$0.04 |
| `python run.py --vertex --list-models` | Lists the model IDs your project can use | No | Free |
| `python run.py --vertex --dry-run --handles X` | Pilot: all images for product X, saved in `output\` | **No** | ~$0.70–1.00 per product |
| `python run.py --vertex --handles X` | One-by-one, live, for product X | Yes | ~$0.70–1.00 per product |
| `python run.py --vertex --limit 25` | One-by-one, live, the next 25 products that are not done | Yes | as above |
| `python run.py --vertex --batch submit --limit 5` | Sends the images still needed for up to 5 products as one Batch job | Marks them as sent | ~half price |
| `python run.py --vertex --batch status` | Shows the Batch jobs that are out | No | Free |
| `python run.py --vertex --batch collect` | Reads finished jobs, checks and places the images | Yes | checks only |
| `python run.py --vertex --batch auto` | collect, then submit the next round | Yes | as above |
| `--retry-manual` (add to any of the above) | Also tries again the images marked "do manually" | Yes | per image |
| `--redo closer --handles X` | Makes those images of product X again (`media`, `fitting`, `multi`, `closer`, or `closer-1,closer-3`) | Yes | per image |
| `python run.py --report` | Writes the progress report (CSV + Google Sheet) | No | Free |
| `python run.py --sheet` | Rebuilds the pilot page `output\index.html` | No | Free |
| `python run.py --web ...` | The same, through the Gemini website (free plan, 100 images a day) | as above | Free |

**GitHub** runs the same commands (Actions → LC images → Run workflow). With the repository variable
`AUTO` = `batch`, it runs a Batch round every 3 hours by itself.

---

## 3. Statuses you will see

**Per product** (Image status in the report):

| Status | Meaning |
|---|---|
| `new` / not started | Nothing done yet |
| `in_progress` | Work started, or images are out in a Batch job |
| `done` | Everything finished |
| `manual` | Finished, but some images failed every try: see "To do" |
| `needs_photo` | No usable full-length front photo |
| `skipped` | Not an abaya type |
| `error` | Something unexpected; tried again on the next run |

**Per image** (inside the record):

| Status | Meaning |
|---|---|
| `done` | Passed and uploaded |
| `manual` | Failed 3 tries; a placeholder holds its place (not in Media) |
| `existing` | The section was filled by hand, so it is left alone |
| `queued` | Out in a Batch job |
| `pending` | Waiting for the next round (tries so far kept) |

**Report columns:**
- **Done:** finished.
- **Retry (…):** what still has to be made (by retry or by hand).
- **In progress:** Batch work is out.
- **Not started:** nothing yet.
- **Needs photo:** no usable photo.
- **Not completed (n of m):** variant images; lists the colours without an image.

---

## 4. Files and functions

### `run.py` (the commands)
| Function | What it does |
|---|---|
| `main()` | Reads the command line and runs the right part. Refuses unsafe combinations: Batch with `--dry-run` (Batch jobs are real and paid), and `--redo` without `--handles`. Writes `output\summary.json`, the shop summary and the report. Exit code 1 when a product had an error or the website stopped the run (GitHub then shows it as failed). Reaching a limit or the spending cap is expected and is not a failure. |
| `make_generator()` | Sets up the image maker: Vertex AI, the Gemini website, or the Gemini API key. |
| `run_products()` | One-by-one. Goes through products (oldest first, active, abaya types). Skips finished ones (unless you named them or used `--retry-manual`). Applies `--redo` / `--retry-manual` and runs each product. Stops cleanly on a limit, Ctrl+C or a website problem. One product's error does not stop the others. |
| `run_batch()` | Batch mode: submit, collect, auto or status, and prints what happened. |
| `test_vertex()` / `test_key()` | Make one test image (Vertex AI / Gemini API key). |
| `contact_sheet()` | Builds `output\index.html` for pilots. Every image shows **USED** (green), **REJECTED** (red) or **PRODUCT PHOTO** (gold). |

### `lc_images/pipeline.py` (the rules for one product)
| Function | What it does |
|---|---|
| `listing_plan()` | Which images a product gets (front-open or closed, plain or decorated). |
| `classify_colour()` | Single, mixed, pattern or printed. |
| `fetch()` / `to_jpeg()` | Download a photo (retries on dropped connections) / convert to JPEG. |
| `ProductRun.prepare()` | Skips finished and non-abaya products. Chooses the reference photo (once) and downloads it. Marks sections filled by hand as `existing`. |
| `ProductRun.pending()` | The images still to make. Colour images only once Media is finished. |
| `ProductRun.make()` | One-by-one: generate and check, up to 3 tries in a row. Passing errors wait and try again. |
| `ProductRun.judge()` | Checks one image: against the photo, and against the section's other images (repeat check). |
| `ProductRun.check()` | Asks the check model, using the rules in `CHECK_PROMPT`. Only a clear "pass" passes. |
| `ProductRun.record()` | Saves one image's outcome. Passed: uploaded (colours also added to Media and linked to their sizes). Failed every try: "do manually", plus a placeholder outside Media. |
| `ProductRun.place_fields()` | Puts finished sections in place: Media in order, the other sections with placeholders where needed. Writes only what changed; never writes a section filled by hand. |
| `ProductRun.colour_jobs()` | Settles every colour (skipped, has image, own photo, same as front image) and lists the ones that need a recoloured image. |
| `ProductRun.colour_base()` | The image colours are made from: the new 1.1, else the reference photo. |
| `ProductRun.finish()` | Final status. A product is only `done` when nothing is left, colour images included. |
| `ProductRun.run()` | One-by-one: prepare, then make and record everything pending, then finish. |
| `ProductRun.reopen_manual()` | `--retry-manual`: the "do manually" images become to-do again. Placeholders stay until the new image passes. |
| `ProductRun.reopen()` | `--redo`: those images become to-do again. Replaced Media images leave the product page (they stay in Files). Redoing 1.1 also relinks the colour that used it. Redoing part of a section that was filled by hand remakes that whole section (it can only be replaced as a whole). Colour images made earlier from the old 1.1 are kept. |
| `ProductRun.images()` | The product's own photos, never the program's images or placeholders. |
| `ProductRun.mark_queued()` / `batch_result()` / `job_for()` / `release_orphans()` | Batch support: mark images as sent; act on one answer (pass, try again, or "do manually" after the 3rd failed try); rebuild an image's job only for the Batch job it is really in; put images of a lost job back in the queue. |

### `lc_images/batch.py` (Batch rounds)
| Function | What it does |
|---|---|
| `BatchRunner.submit()` | Collects the images still needed (up to `VERTEX_BATCH_SIZE`, default 50, and never past the spending cap, checks included). Uploads each product's photo and the request list to your bucket. Starts one Vertex AI Batch job and records it straight away. |
| `BatchRunner.collect()` | For each finished job: reads the answers, then checks, uploads and places each image. A job is cleared only when every product in it was handled; otherwise it is read again next time (up to 3 times; then it is dropped and its unfinished images go back into the queue, so one bad product can never block Batch). A failed job puts its images back (no try counted). |
| `BatchRunner.auto()` | collect, then submit the next round when no job is still out. |
| `_save_state()` | The list of jobs out, kept in Shopify (`image_batches`) and `output\batches.json`. Merged before every write, so a PC and GitHub do not wipe each other's jobs (and a job one machine cleared is never written back by the other). Images are only treated as lost after the list is read again, so a job another machine has just started is never sent twice. |
| `Storage` | Upload, list, download and delete files in your bucket (only the program's own `lc-batches/` folder). |

### `lc_images/vertex.py` (Vertex AI)
| Function | What it does |
|---|---|
| `GeminiVertex.generate()` | One image through Vertex AI in your project (signed in with your Google account; no API key). |
| `GeminiVertex.ask_json()` | Photo choice and checks (`gemini-3.8-flash`). |
| `_call()` | Every request has a 2.5-minute limit. "Too many requests", server errors and no-answer wait 15s, 30s, 60s… and try again (6 times), then the run stops and continues next time. |
| `_budget_check()` / `Spend` | The spending cap: stops before the estimate passes $280. The figure is kept in `output\vertex_spend.json` and in Shopify (`vertex_spend`); each machine adds its own spending to the shared figure. |
| `create_batch()` / `batch_state()` / `list_models()` | Start and follow Batch jobs; list available models. |

### `lc_images/shopify.py` (Shopify)
| Function | What it does |
|---|---|
| `Shopify()` | Signs in with your app's Client ID and Secret (a fresh token every run). Retries on throttling and dropped connections. |
| `iter_products()` / `product_by_id()` | Read products (oldest first, active), or one product. |
| `upload_image()` | Upload to Shopify Files and wait until it is ready. |
| `attach_to_product()` / `detach_from_product()` / `move_media_to_front()` | Add to, take off (the file stays in Files) and reorder the product's Media. |
| `set_metafields()` / `save_audit()` | Write the image fields / the progress record. |
| `link_variants()` | Give each size of a colour its image. |
| `placeholder()` | One black image per image position, uploaded once and reused. Named PLACEHOLDER in Files and in its alt text. Made again if it was deleted. |
| `get_shop_json()` / `set_shop_json()` | Shop-wide records: placeholders, Batch jobs, spend, run summary. |

### `lc_images/report.py` (the report)
| Function | What it does |
|---|---|
| `make_report()` | Builds one row per active product **from what is really in Shopify**. Saves `output\report.csv` and updates the Google Sheet (tab "Image report", colours: green Done, red Retry / Not completed, amber In progress). |
| `row_for()` | Product, Admin link, Store link, Template, Media, Fitting, Multi display, A little closer, Variant images, the six text fields, Text content, Image status, To do, Checked. |
| `image_section_status()` / `media_status()` / `variant_status()` / `_text_status()` | Each column's Done / Retry / In progress / Not started. Text lists must have 5 items and the closer description 2 lines. |

### Website mode (`web.py`, `lc_images/gemini_web.py`)
The same rules, driven in a Chrome window you sign in to yourself:
- one chat per product, and one shared chat for checks;
- a photo is only sent once Gemini shows it attached;
- errors are retried in the same chat first;
- it stops at Gemini's limit.

`web.py login | inspect | test | upload-check` are the setup and test commands.

---

## 5. Safety rules built in

- **Nothing is deleted:** no product photo, no file. Replaced Media images only leave the product page.
- **Hand work is respected:** a section filled by hand is never written; colours with a variant image are left alone.
- **No wrong images by design:**
  - a failed image is never uploaded;
  - no prompt is sent without the product photo;
  - answers are matched to the exact product, image and Batch job.
- **Money:**
  - the $280 cap, checks included, holds across runs and across PC and GitHub;
  - pilots never change Shopify;
  - Batch can't run as a "pilot".
- **Stopping is always safe:** Ctrl+C, a closed window, sleep, a lost connection, a limit or the cap. Progress is kept and the next run continues.
- **Keys:**
  - only in `.env` on your PC or as GitHub secrets;
  - GitHub signs in to Google Cloud without any stored key (Workload Identity).

## 6. Known limits

- **Image size:** about 1 megapixel (Nano Banana 2.1 at 1K).
- **Model mistakes:** the check model can still miss a mistake, or reject a good image. Look at pilot pages and the report.
- **Front-open vs closed:** decided by the photo choice. One product (Maxi Abaya with Pulse Work Shrug) was recorded as front-open, but the listing notes say it looks closed; check such cases.
- **The first Batch round on real Google Cloud is untested.** All Batch steps are tested against stand-ins only, so start with `--batch submit --limit 1` and check it.
