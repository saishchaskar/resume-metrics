# ResumeMetrics

Resume screening workspace built with React and a Flask API.

## Repository layout

- `src/` — React client, screens, shared API client, and authentication context.
- `public/` — Firebase Hosting shell, brand assets, and web-app metadata.
- `resume-screening-backend/app.py` — Flask API entry point and HTTP routes. The production batch pipeline is split into `batch_preprocessor.py` (ZIP validation/fan-out) and `pipeline_store.py` (Firestore records and GCS references).
- `resume-screening-backend/requirements.txt` and `Dockerfile` — Python runtime dependencies and the Cloud Run image.
- `.github/workflows/deploy.yml` — deploys the Firebase frontend and the existing Cloud Run API/Job.

There is one supported backend: Flask. The old, unused Express backend and its checked-in dependencies have been removed. SQLite is retained only for local development; production state that must survive restarts or be shared across instances belongs in Firestore, while uploaded resumes remain in private Cloud Storage.

## Screenshots

Desktop sign-in:

![ResumeMetrics sign-in on desktop](docs/screenshots/login-desktop.png)

Mobile sign-in:

![ResumeMetrics sign-in on mobile](docs/screenshots/login-mobile.png)

## Authentication and Deployment

- Passwords are sent only to the API over HTTPS in production and are hashed by Werkzeug before storage. Client-side password hashing is not a substitute for HTTPS.
- Set `APP_ENV=production` and provide a stable, private `JWT_SECRET_KEY` for the backend. The backend refuses to start in production without it. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- Set `CORS_ALLOWED_ORIGINS` to the exact HTTPS frontend origin when the API is hosted separately. The default allows only the local development origin.
- Set `REACT_APP_API_URL` to the HTTPS API origin when building the frontend for a separate API host. Same-origin development uses the React proxy.
- The Integrations page shows the backend intake URL for the current build: local development uses `http://localhost:5000/api/v1`, while production uses the configured Cloud Run API origin (default `https://flask-backend-app-879571205118.asia-south1.run.app`).
- Production login and signup requests require HTTPS. When TLS terminates at a reverse proxy, ensure it sets `X-Forwarded-Proto` and that the Flask app is reachable only through that trusted proxy.
- Baseline throttles limit login traffic, signup creation, screening starts, and general API requests. The in-process counters are per Cloud Run instance and reset when it restarts; they are abuse controls, not a distributed denial-of-service defense.
- The legacy local ZIP endpoint accepts at most 29 MiB and expands at most 100 MiB. The production path uploads directly to Cloud Storage and is configured for a 1 GiB ZIP, 10 GiB expanded content, and 10 MiB per resume. There is no fixed applicant-count cap; byte, PDF-page, job-description, queue-rate, and Cloud Run execution limits still apply. Those are safety/initial capacity settings, not evidence of a measured 2,000-resume service level.
- Cloud Run is capped at three instances by the deployment workflow as a cost and scaling guardrail. This can return capacity errors during legitimate spikes; tune the cap after observing traffic.
- Access tokens expire after 30 minutes and are stored in browser session storage. This limits persistence across browser sessions but does not protect against cross-site scripting; continue to prevent XSS and keep dependencies current.
- Ranking uses a bounded 0–1 score: 75% semantic similarity across chunks from the full resume and role brief, plus 25% exact job-term coverage. This is a ranking aid, not a calibrated probability or prediction of job performance. Achievement, community, people-skill, and date phrases are shown as evidence excerpts; employment gaps are not inferred or scored.
- Processing progress is polled by the browser every 1.5 seconds. Completed screening history is paginated 50 records at a time, and results can be exported as CSV or Excel.
- Local development still uses SQLite and an in-process worker for the small ZIP endpoint. The production durable paths use Firestore for accounts, integration records, and large-batch progress/results, Cloud Storage for private resume files, a Cloud Run Job for ZIP inspection/fan-out, and Cloud Tasks for per-resume scoring. Older SQLite-only screening history is not migrated automatically.

### Careers portal integration API

Workspace offers two screening paths: manual ZIP batch upload and careers portal/ATS intake. Selecting portal intake opens role creation in Workspace; API key management and reusable connection instructions live on the separate **Integrations** page. Manual batches and integration roles appear together in **Results**, where recruiters can browse score-ranked candidates, review evidence, preview resumes, and export results. Workspace role setup can open or close application intake. The company's careers portal or ATS backend submits one PDF/DOCX resume per application. Each accepted application is queued for scoring; the Results page refreshes the selected role every 1.5 seconds while it is open or work is pending. Closing a role rejects new submissions while already accepted applications continue processing. API keys use the `Authorization: Bearer rm_live_...` header, are hashed in the database, and are returned in plaintext only once. Keep keys in the ATS server's secret store; do not put them in browser JavaScript.

The generic API contract is:

- `GET /api/v1/jobs` lists roles and their `open` or `closed` status for the API key's owner.
- `POST /api/v1/jobs/{job_id}/applications` accepts multipart fields `candidate_name`, optional `candidate_email`, and `resume`.
- `GET /api/v1/jobs/{job_id}/applications?limit=50&offset=0` returns processing counts and candidates sorted by score.
- Workspace users create roles at `POST /api/integrations/jobs`, change intake with `PATCH /api/integrations/jobs/{job_id}` and `{ "status": "open" | "closed" }`, and inspect recruiter results at `GET /api/integrations/jobs/{job_id}/applications?limit=50&offset=0`. API keys are managed at `/api/integrations/keys`.
- Closed roles return HTTP `409` for new candidate submissions. The API accepts individual resumes up to 10 MiB; the ZIP upload limits do not apply to these single-file requests.

Cloud Tasks is required for production scoring. Configure `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_SERVICE_ACCOUNT`, and `INTEGRATION_WORKER_URL`; grant the API runtime identity `roles/cloudtasks.enqueuer`, allow the Cloud Tasks service agent to mint OIDC tokens for the invoker account, and grant that account permission to invoke the API. The endpoint verifies the task's OIDC identity. The generic API supports ATS products that can send outbound HTTP requests; vendor-specific OAuth connectors need separate credentials and adapters.

### Large ZIP pipeline deployment

The production UI uses this sequence: create an owner-scoped Firestore batch, upload the ZIP directly to the private GCS bucket using a resumable session, start a Cloud Run Job to validate and fan out files, and enqueue one Cloud Task per supported PDF/DOCX resume. Each task scores one file and writes its state to Firestore. Results page progress polls every 1.5 seconds; candidates are fetched 50 at a time and CSV export retrieves all pages. Cloud Run's HTTP request size no longer limits ZIP uploads.

The code is ready for this pipeline, but the Google Cloud resources below must exist and the updated workflow must deploy before Firebase production uses it. The initial guardrails are 1 GiB compressed, 10 GiB expanded, 10 MiB per resume, Cloud Tasks queue limited to 2 dispatches/second and 6 concurrent deliveries, and Cloud Run API service limited to 3 instances with concurrency 4. No applicant-count limit is set; the expanded-byte cap and processing/cost guardrails bound practical batches. **2,000-resume capacity remains unverified until the production benchmark has run with representative resumes.**

#### One-time Cloud Shell setup

Run from the repository root in Cloud Shell with the `resume-screening-tool` project selected (clone/pull the repository there first if it is not already present). Firestore does not require App Engine; the earlier `gcloud app describe` error is unrelated.

```sh
gcloud config set project resume-screening-tool
gcloud services enable firestore.googleapis.com cloudtasks.googleapis.com run.googleapis.com storage.googleapis.com --project=resume-screening-tool
gcloud firestore databases create --database='(default)' --location=asia-south1 --type=firestore-native --project=resume-screening-tool
gcloud tasks queues create resumemetrics-batch-scoring --location=asia-south1 --max-dispatches-per-second=2 --max-concurrent-dispatches=6 --max-attempts=5 --min-backoff=10s --max-backoff=300s --project=resume-screening-tool

gcloud firestore indexes composite create --collection-group=screening_batches --field-config=field-path=owner_id,order=ascending --field-config=field-path=created_at,order=descending --project=resume-screening-tool
gcloud firestore indexes composite create --collection-group=integration_jobs --field-config=field-path=user_id,order=ascending --field-config=field-path=created_at,order=descending --project=resume-screening-tool
gcloud firestore indexes composite create --collection-group=integration_api_keys --field-config=field-path=user_id,order=ascending --field-config=field-path=created_at,order=descending --project=resume-screening-tool
gcloud firestore indexes composite create --collection-group=integration_api_keys --field-config=field-path=user_id,order=ascending --field-config=field-path=id,order=ascending --project=resume-screening-tool
gcloud firestore indexes composite create --collection-group=integration_applications --field-config=field-path=job_id,order=ascending --field-config=field-path=user_id,order=ascending --field-config=field-path=score,order=descending --project=resume-screening-tool
gcloud firestore indexes composite create --collection-group=integration_applications --field-config=field-path=job_id,order=ascending --field-config=field-path=user_id,order=ascending --field-config=field-path=status,order=ascending --project=resume-screening-tool

gcloud iam service-accounts create resumemetrics-task-invoker --display-name='ResumeMetrics Cloud Tasks invoker' --project=resume-screening-tool
gcloud run services add-iam-policy-binding flask-backend-app --region=asia-south1 --member='serviceAccount:resumemetrics-task-invoker@resume-screening-tool.iam.gserviceaccount.com' --role=roles/run.invoker --project=resume-screening-tool
gcloud iam service-accounts add-iam-policy-binding resumemetrics-task-invoker@resume-screening-tool.iam.gserviceaccount.com --member='serviceAccount:user-227@resume-screening-tool.iam.gserviceaccount.com' --role=roles/iam.serviceAccountUser --project=resume-screening-tool

gcloud projects add-iam-policy-binding resume-screening-tool --member='serviceAccount:user-227@resume-screening-tool.iam.gserviceaccount.com' --role=roles/datastore.user
gcloud projects add-iam-policy-binding resume-screening-tool --member='serviceAccount:user-227@resume-screening-tool.iam.gserviceaccount.com' --role=roles/cloudtasks.enqueuer
gcloud projects add-iam-policy-binding resume-screening-tool --member='serviceAccount:user-227@resume-screening-tool.iam.gserviceaccount.com' --role=roles/run.developer
gcloud storage buckets add-iam-policy-binding gs://resume-screening-resumes --member='serviceAccount:user-227@resume-screening-tool.iam.gserviceaccount.com' --role=roles/storage.objectAdmin
gcloud iam service-accounts add-iam-policy-binding resumemetrics-task-invoker@resume-screening-tool.iam.gserviceaccount.com --member='serviceAccount:service-879571205118@gcp-sa-cloudtasks.iam.gserviceaccount.com' --role=roles/iam.serviceAccountUser --project=resume-screening-tool

gcloud storage buckets update gs://resume-screening-resumes --cors-file=docs/gcs-cors.json
```

The `user-227@...` runtime account also needs `roles/storage.objectAdmin` on `resume-screening-resumes` (already required by the existing API). If a permission is already present, the add-binding command is harmless. For least privilege, replace project-wide Firestore/Tasks/Run grants with resource-level IAM after initial setup. The Cloud Tasks service agent is created when the API is enabled; if the account is not present yet, rerun its IAM binding after a short delay.

Firestore composite-index creation is asynchronous. Wait until the six indexes above show `READY` with `gcloud firestore indexes composite list --database='(default)' --project=resume-screening-tool` before deploying.

In GitHub repository **Settings → Secrets and variables → Actions → Variables**, set `CLOUD_TASKS_SERVICE_ACCOUNT` to `resumemetrics-task-invoker@resume-screening-tool.iam.gserviceaccount.com`. The existing deployment service account in `GCP_SA_KEY` must be allowed to update the existing Cloud Run service and deploy the Cloud Run Job. Push/dispatch the workflow only after the setup completes. It deploys the existing `flask-backend-app` service and the `resumemetrics-batch-preprocess` Job from the same image. The job uses the existing Cloud Run runtime service account.

After deployment, sign up once again if the old account existed only in Cloud Run's SQLite file; production accounts now live in Firestore and there is no automatic import from the ephemeral SQLite database. Old SQLite screening history, integration roles, and API keys are also not automatically imported. Create replacement API keys in Integrations and update the ATS secret store. Test with a small consented batch first, verify it appears under Results and that browser preview works, then use the 2,000-resume test batch. The code does not claim 2,000-resume latency or cost until that load test completes. Check `gcloud run jobs executions list --job=resumemetrics-batch-preprocess --region=asia-south1` and Cloud Tasks queue metrics/logs while processing.

### Local batch benchmark

Use a representative ZIP containing PDF/DOCX resumes and a job description to measure local sequential extraction and production-model inference. The benchmark applies the same ZIP-size, expanded-size, per-file-size, and archive-entry limits as the app. Install the production backend requirements first; the first run also downloads the Sentence Transformers model if it is not cached.

```sh
.venv/bin/python resume-screening-backend/benchmark_batch.py \
  /path/to/representative-resumes.zip \
  --job-description-file /path/to/job-description.txt
```

The JSON output reports model startup time, per-resume median and p95, local resumes/minute, ZIP/expanded sizes, and peak resident memory. It excludes Cloud Run startup, GCS transfer, and concurrent users; use it for local comparisons only. For a production benchmark, send the same representative batch through the deployed service, record upload duration, job preprocessing duration, queue drain time, Cloud Run CPU/memory/latency/errors, and total Google Cloud cost. Keep personal resume data private and use consented or synthetic examples.
- PDF resumes open in an in-app browser preview; DOCX resumes show an in-app text preview. Download is a separate action. The authenticated API checks that a resume belongs to one of the signed-in user's saved screening batches before serving it.
- Do not commit service-account credentials. The local service-account JSON is excluded from Git and Docker build context. Because it was previously tracked, revoke and rotate that key in Google Cloud; ignoring the file does not remove copies from Git history.

Build the frontend with `npm run build`. Firebase Hosting serves the generated `build` directory. Run the Flask API behind an HTTPS-enabled production server and configure the environment values above before deployment.

## Run locally

You can run a fast, credential-free local mode for UI and API development, or run against real Google Cloud Storage. The credential-free mode uses local files and a lightweight word-overlap scorer; it does not validate Google Cloud access or use the production semantic model. Never enable `RESUMESIGNAL_LOCAL_TEST` outside local development.

### One-time setup

Use Node.js 20 or 22 and Python 3.10 for the backend's pinned ML packages. The lightweight local test dependencies also work with Python 3.12. From the repository root:

```sh
npm ci
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r resume-screening-backend/requirements-test.txt
```

Run the end-to-end API smoke test (signup, login, upload, ranking, results, deletion):

```sh
.venv/bin/python -m pytest -q resume-screening-backend/test_local_e2e.py
```

### Start the app without Google Cloud

Terminal 1, from the repository root:

```sh
RESUMESIGNAL_LOCAL_TEST=1 \
DATABASE_PATH="$PWD/resume-screening-backend/users.local.db" \
RESUMESIGNAL_TEST_STORAGE="$PWD/resume-screening-backend/uploads.local" \
.venv/bin/python resume-screening-backend/app.py
```

Terminal 2, from the repository root (the `start` script enables polling to avoid the Linux watcher limit):

```sh
npm start
```

Open `http://localhost:3000`, create an account with a password of at least 12 characters, then upload a ZIP file containing PDF or DOCX resumes. The frontend's development proxy forwards `/api` requests to `http://localhost:5000`. Polling works around Linux's inotify watcher limit and can use more CPU while the dev server is running. If your system watcher limit has been raised, remove the polling environment settings from the `start` script in `package.json` for lower CPU usage.

If you prefer to fix the Linux watcher limit system-wide instead, inspect the current limit and raise it:

```sh
cat /proc/sys/fs/inotify/max_user_watches
sudo sysctl fs.inotify.max_user_watches=524288
```

To make that setting persist after reboot:

```sh
echo 'fs.inotify.max_user_watches=524288' | sudo tee /etc/sysctl.d/99-resumesignal-watches.conf
sudo sysctl --system
```

Then `npm start` can run without `CHOKIDAR_USEPOLLING=true`.

### Run against your Google Cloud Storage bucket

This mode uses the real Sentence Transformers model and Google Cloud Storage. It needs network access the first time it downloads the model, plus ADC credentials able to upload and download objects. Keep credential files outside the repository. The app's bucket name is configurable with `GCS_BUCKET_NAME` (default: `resume-screening-resumes`). Resume previews are streamed through the authenticated API, so signed URL signing permissions are not needed.

1. Install the Google Cloud CLI, then set your project and select a globally unique bucket name:

```sh
gcloud auth login
export GCP_PROJECT_ID="your-gcp-project-id"
export GCS_BUCKET_NAME="a-globally-unique-bucket-name"
export LOCAL_SA_NAME="resumesignal-local"
export LOCAL_SA="${LOCAL_SA_NAME}@${GCP_PROJECT_ID}.iam.gserviceaccount.com"
gcloud config set project "$GCP_PROJECT_ID"
gcloud services enable storage.googleapis.com iamcredentials.googleapis.com --project "$GCP_PROJECT_ID"
gcloud iam service-accounts create "$LOCAL_SA_NAME" --project "$GCP_PROJECT_ID" --display-name "ResumeMetrics local development"
gcloud storage buckets create "gs://$GCS_BUCKET_NAME" --project "$GCP_PROJECT_ID" --location us-central1 --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "gs://$GCS_BUCKET_NAME" \
  --member="serviceAccount:$LOCAL_SA" --role=roles/storage.objectAdmin
```

2. Allow your Google user to impersonate that service account, then create ADC credentials for local application use:

```sh
export GOOGLE_USER="$(gcloud config get-value account)"
gcloud iam service-accounts add-iam-policy-binding "$LOCAL_SA" \
  --project "$GCP_PROJECT_ID" \
  --member="user:$GOOGLE_USER" --role=roles/iam.serviceAccountTokenCreator
gcloud auth application-default login --impersonate-service-account="$LOCAL_SA"
```

If you cannot use impersonation, you can instead set `GOOGLE_APPLICATION_CREDENTIALS` to the absolute path of a service-account JSON key. Do not put that key in this repository or send it in chat. Keyless impersonation is preferred.

3. Install the backend's production dependencies. This includes the Sentence Transformers model stack and CPU-only PyTorch; it is larger than the lightweight test dependencies:

```sh
.venv/bin/python -m pip install -r resume-screening-backend/requirements.txt
```

4. Copy the backend environment template and set its project, bucket, and local JWT secret:

```sh
cp resume-screening-backend/.env.example resume-screening-backend/.env
```

Set `JWT_SECRET_KEY` to a private random value (generate one with `../.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))'`). Set `GOOGLE_CLOUD_PROJECT` to `$GCP_PROJECT_ID` and `GCS_BUCKET_NAME` to `$GCS_BUCKET_NAME`. With ADC impersonation, leave `GOOGLE_APPLICATION_CREDENTIALS` commented out. If using a JSON key instead, set that variable to its absolute path in `resume-screening-backend/.env`.

5. Start the API from its directory so Flask loads `resume-screening-backend/.env`:

```sh
cd resume-screening-backend
../.venv/bin/python app.py
```

In another terminal, from the repository root, start the frontend:

```sh
npm start
```

This real-cloud mode uploads resumes into your bucket. It does not use `RESUMESIGNAL_LOCAL_TEST`. Avoid uploading real candidate data until you have configured your bucket access and data retention appropriately.

#### Exercise the production large-ZIP pipeline from localhost

Complete the Cloud Shell setup under **Large ZIP pipeline deployment** first. Make sure your local ADC identity can access Firestore, upload to the resume bucket, enqueue Cloud Tasks, and start the existing Cloud Run Job. Add `http://localhost:3000` to the bucket CORS policy. In `resume-screening-backend/.env`, set the project, bucket, task queue, task invoker service account, worker URL, and Job name from that setup; keep `APP_ENV=development` so local sign-in works over HTTP. Start the backend as above. In a second terminal, opt the React dev server into the direct upload path:

```sh
REACT_APP_BATCH_PIPELINE=1 npm start
```

The browser uses the local Flask API for sign-in and API requests. ZIP bytes go from the browser to Cloud Storage, then the deployed Cloud Run Job and Cloud Tasks workers process them. The batch appears in Firestore-backed Results, while local development accounts remain in local SQLite. This exercises the real Google Cloud pipeline without deploying a new service revision.

## Production deployment settings

The backend production requirements are intentionally limited to packages the API uses. They use CPU-only PyTorch for Sentence Transformers; the previous environment freeze included `tensorflow-intel`, which has no compatible Linux distribution and caused Cloud Run image builds to fail. Build the same backend image locally from the repository root with:

```sh
docker build -f resume-screening-backend/Dockerfile \
  -t resumemetrics-api:local \
  resume-screening-backend
```

This builds the image but does not start it or require Google Cloud credentials. The deployed container still needs the production environment variables and Google Cloud permissions described below.

The Cloud Run workflow updates the existing `flask-backend-app` service in `asia-south1`; it checks that service exists before deploying so a name or region mismatch fails instead of creating another service. It caps the service at three instances as a cost guardrail. It uses GitHub Actions secrets `GCP_PROJECT_ID` and `GCP_SA_KEY` (JSON for a deployment service account). It expects a Secret Manager secret named `JWT_SECRET_KEY`, granted to the Cloud Run runtime service account. That runtime account needs `Storage Object Admin` on the configured resume bucket. No user-managed service-account key or `signBlob` permission is needed by the application.

For distributed denial-of-service protection, put the API behind an external Application Load Balancer with Cloud Armor rate-based rules and restrict Cloud Run ingress to `internal-and-cloud-load-balancing`; the current Firebase frontend calls the public Cloud Run URL directly, so Cloud Armor is not currently in the request path. See Google's [Cloud Armor rate-limiting guide](https://docs.cloud.google.com/armor/docs/rate-limiting-overview) and [Cloud Run ingress guide](https://docs.cloud.google.com/run/docs/securing/ingress).

Set the GitHub Actions variable `GCS_BUCKET_NAME` if the bucket is not named `resume-screening-resumes`; the workflow defaults to that bucket name. Never commit credentials, and revoke any service-account key that was previously committed. If you previously granted the runtime service account `roles/iam.serviceAccountTokenCreator` on the whole project solely for signed resume URLs, remove that grant after the updated app is deployed; it is no longer required.

After the new backend is deployed, remove the now-unneeded project-level signing grant if you added it for the earlier signed-URL flow:

```sh
gcloud projects remove-iam-policy-binding resume-screening-tool \
  --member="serviceAccount:user-227@resume-screening-tool.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

Firebase Hosting deploys the frontend from `master` in project `ocr-text-50a5b`. Its build uses the Cloud Run URL shown above; override it with the GitHub Actions variable `REACT_APP_API_URL` if the service URL changes. The Cloud Run deploy sets `CORS_ALLOWED_ORIGINS` for the Firebase `.web.app` and `.firebaseapp.com` domains. Firebase Hosting preview channels build the frontend too, but their temporary domains are not included in the production CORS allowlist.

The Firebase Hosting workflows require the repository Actions secret `FIREBASE_SERVICE_ACCOUNT_OCR_TEXT_50A5B`, containing raw JSON for a service account in project `ocr-text-50a5b` with the Firebase Hosting Admin role (`roles/firebasehosting.admin`). Never paste the key into chat or commit it. The workflow checks this secret before building and reports a clear error if it is missing.

If the deploy job reports that `GCP_SA_KEY` is not valid JSON, download a fresh JSON key for the deployment service account and replace the repository secret with the raw file contents. On Linux, you can validate and set it with GitHub CLI:

```sh
python3 -m json.tool /secure/path/gcp-deploy-key.json >/dev/null
gh secret set GCP_SA_KEY < /secure/path/gcp-deploy-key.json
```

The secret must be the plain UTF-8 JSON file, not Base64, not wrapped in quotes, and not UTF-16 encoded. The workflow checks the format without printing the credential. Do not paste the key into chat or commit it.
