@echo off
rem One-time Google Cloud setup so GitHub can run the image program with your PC off.
rem Run it on the PC where gcloud is signed in with the account that has the credit:
rem     cd C:\lc-image-program
rem     git pull
rem     setup\google_cloud_setup.bat
rem It creates: a storage bucket for Batch jobs, permissions for the lc-images service account (Vertex AI and the
rem bucket only), and a keyless sign-in that lets ONLY the GitHub repository below act as that service account.
rem It creates no key or password. Running it twice is harmless ("already exists" messages are fine).

set PROJECT=project-c065e1d2-16ae-4565-bb0
set REPO=muhammadfahad-dev/lc-image-program
set SA=lc-images@%PROJECT%.iam.gserviceaccount.com
set BUCKET=lc-images-%PROJECT%

echo.
echo === 1/7 Project number
for /f "delims=" %%i in ('gcloud projects describe %PROJECT% --format^="value(projectNumber)"') do set NUMBER=%%i
if "%NUMBER%"=="" (
  echo Could not read the project number. Is gcloud signed in? Run: gcloud auth login
  exit /b 1
)
echo Project number: %NUMBER%
call gcloud config set project %PROJECT%

echo.
echo === 2/7 Switching on the Google services the program uses
call gcloud services enable aiplatform.googleapis.com storage.googleapis.com iamcredentials.googleapis.com sts.googleapis.com sheets.googleapis.com iam.googleapis.com

echo.
echo === 3/7 Storage bucket for Batch jobs
call gcloud storage buckets create gs://%BUCKET% --location=us-central1 --uniform-bucket-level-access
call gcloud storage buckets add-iam-policy-binding gs://%BUCKET% --member=serviceAccount:service-%NUMBER%@gcp-sa-aiplatform.iam.gserviceaccount.com --role=roles/storage.objectAdmin

echo.
echo === 4/7 Service account lc-images
call gcloud iam service-accounts describe %SA% >nul 2>&1
if errorlevel 1 call gcloud iam service-accounts create lc-images --display-name="LC images"

echo.
echo === 5/7 What the service account may do (Vertex AI, and the bucket)
call gcloud projects add-iam-policy-binding %PROJECT% --member=serviceAccount:%SA% --role=roles/aiplatform.user --condition=None
call gcloud storage buckets add-iam-policy-binding gs://%BUCKET% --member=serviceAccount:%SA% --role=roles/storage.objectAdmin

echo.
echo === 6/7 Keyless sign-in for GitHub (only this repository)
call gcloud iam workload-identity-pools create github --location=global --display-name="GitHub"
call gcloud iam workload-identity-pools providers create-oidc lc-repo --location=global --workload-identity-pool=github --display-name="lc-image-program" --issuer-uri="https://token.actions.githubusercontent.com" --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" --attribute-condition="assertion.repository=='%REPO%'"
call gcloud iam service-accounts add-iam-policy-binding %SA% --role=roles/iam.workloadIdentityUser --member="principalSet://iam.googleapis.com/projects/%NUMBER%/locations/global/workloadIdentityPools/github/attribute.repository/%REPO%"

echo.
echo === 7/7 Done
echo Send this line to Claude (it is not secret):
echo PROJECT_NUMBER=%NUMBER%
