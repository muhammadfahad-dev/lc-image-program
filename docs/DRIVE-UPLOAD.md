# Drive upload: abaya images from Shopify to Google Drive

`drive_upload.py` copies every image of every active abaya in Shopify into the client's Google Drive:

```
ABAYA PICTURE EDITED / <type folder> / <product name> / <product name> 1.jpg, 2.jpg, 3.jpg ...
```

| Shopify collection | Drive folder |
|---|---|
| Stonework Embroidery Abaya | Embroidery + StoneWork |
| Embroidery Abaya | Embroidery Abaya |
| Handcrafted Cutdana Abaya | Hand Work Abaya |
| Plain Abaya | Plain Abaya |
| Printed Abaya | Printed Abaya |
| Stonework Abaya | Stone Work Abaya |
| Kids | Kids Abaya |

- Images, in page order: the Media gallery, the colour (variant) images, then Fitting, Multi display and A little
  closer. Each image once (colour images are normally also in the gallery); videos are left out. Full original quality.
- **A product with any black image is skipped entirely**, in every folder: the image program's "do manually"
  placeholders, files named `lc-PLACEHOLDER`, and any image that is all black (each image is checked pixel by pixel;
  real photos, even close-ups of black fabric, are far brighter). Nothing of it is uploaded, and the report lists it
  as `Skipped - black image` with the section and image number. Replace the image in Shopify and run again.
- A product in two collections gets a folder in both type folders.
- In names, `|` becomes `-` and characters Windows refuses (`\ / : * ? " < >`) are dropped.
- The seven type folders must already exist with exactly these names; the program never makes or renames them.
- Safe to run again: every uploaded file remembers its Shopify image ID, so a second run only adds new images.
- Nothing is ever deleted or changed, in Drive or in Shopify (Shopify is only read). The Drive code has no delete or
  update call.
- Each run writes `output/drive_report.csv`: folder, product, Admin link, status (Uploaded, Already in Drive,
  Would upload, Skipped - black image, Skipped / No images), images uploaded, already in Drive, reason. Skipped
  products come first. The run's log also ends with the list of skipped products.

## Why it signs in with the client's Gmail

The folder is in the client's normal Gmail Drive. Google does not let program (service) accounts own files there, so
the program signs in once as the client and uploads as the client. The files are then owned by the client and use
the client's storage, exactly as if the client had uploaded them; nothing depends on anyone else's account.

The sign-in asks for full Drive access, because the program has to find the client's existing folders. The program
itself only lists folders and adds files.

## One-time setup (about 15 minutes)

### 1. Google Cloud: a sign-in client

In the Google Cloud console (the same project as the image program is fine):

1. **APIs & Services > Library**: enable **Google Drive API**.
2. **APIs & Services > OAuth consent screen** (or **Google Auth Platform**): choose **External**, app name
   `LC Drive upload`, your email as support and developer contact. Under **Audience / Test users**, add the
   **client's Gmail** (the owner of ABAYA PICTURE EDITED). Leave it in **Testing**.
3. **APIs & Services > Credentials > Create credentials > OAuth client ID**: type **Desktop app**, name
   `LC Drive upload`. Copy the **Client ID** and **Client secret**.

In Testing, Google ends the sign-in after 7 days. That is enough for this job; if it expires, run step 2 again.

### 2. Sign in once (on your PC)

1. Put the Client ID and secret in `.env` next to `run.py`:
   ```
   GOOGLE_OAUTH_CLIENT_ID=...
   GOOGLE_OAUTH_CLIENT_SECRET=...
   ```
2. Run `python drive_upload.py --authorize`. A browser opens on that PC: **the client signs in with their Gmail**
   there (in person, or with you sharing the screen). The sign-in has to finish in a browser on the PC that runs
   the command.
   Google warns that the app is not verified: click **Advanced > Go to LC Drive upload (unsafe)**; it is your own
   app. Allow access.
3. The program prints a refresh token. Keep it private. Put it in `.env` as `GOOGLE_OAUTH_REFRESH_TOKEN=...`.

### 3. GitHub secrets (to run it on GitHub)

Repository **Settings > Secrets and variables > Actions > New repository secret**, three secrets:
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`.
The Shopify secrets are already there from the image program.

## Running it

On GitHub: **Actions > Drive upload > Run workflow**. It runs only when started by hand.

1. **Test**: mode `test`. Lists every product and how many images it would upload; uploads nothing. Download the
   `drive-report` from the run's page and check it.
2. **Small upload**: mode `upload`, limit `3`. Check the three product folders in Drive.
3. **Everything**: mode `upload`, limit empty. About 300 products and several thousand images take a few hours. If
   it stops (time limit, network), run it again: it carries on where it stopped and never uploads an image twice.

On your PC instead: `pip install requests python-dotenv`, then `python drive_upload.py --dry-run`,
`python drive_upload.py --limit 3`, `python drive_upload.py`.

## When the job is finished

1. Delete the three Google secrets from GitHub.
2. Remove the program's access in the client's Google account: Security > Your connections to third-party apps >
   LC Drive upload > Delete all connections.
3. Set the ABAYA PICTURE EDITED folder's sharing back to **Restricted** and add only the people who need it.
