# Docker & Cloud Run Deployment Guide

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| Docker Desktop | Build & run images locally | https://docs.docker.com/get-docker |
| Google Cloud SDK (`gcloud`) | Push images, deploy to Cloud Run | https://cloud.google.com/sdk/docs/install |
| Docker credential helper | Authenticate Docker → Artifact Registry | `gcloud auth configure-docker` |

Verify everything is in place:

```bash
docker --version
gcloud --version
gcloud auth list          # ensure the correct account is active
gcloud config get-value project
```

---

## 1. Authenticate with Google Cloud

```bash
# Log in with your user account (one-time)
gcloud auth login

# Set the default project
gcloud config set project <YOUR_GCP_PROJECT_ID>

# Authorize Docker to push to Artifact Registry / GCR
gcloud auth configure-docker <YOUR_REGION>-docker.pkg.dev  # e.g. us-central1, europe-west1, asia-east1
```

---

## 2. Build the Image

From the project root (where `Dockerfile` lives):

```bash
docker build -t translation-service:latest .
```

To tag with a specific version (recommended for production):

```bash
docker build -t translation-service:1.0.0 .
```

### Verify the build locally

```bash
docker run --rm \
  -p 8080:8080 \
  -e GOOGLE_CLOUD_PROJECT=<YOUR_GCP_PROJECT_ID> \
  -e GOOGLE_CLOUD_LOCATION=<YOUR_REGION> \
  -e VERTEX_AI_ENDPOINTS="<YOUR_GCP_PROJECT_ID>:<YOUR_REGION>,<YOUR_VERTEX_PROJECT_1>:<YOUR_REGION>,<YOUR_VERTEX_PROJECT_2>:<YOUR_REGION>" \
  -e GEMINI_MODEL=gemini-2.5-flash \
  -e LOG_LEVEL=INFO \
  -e EVAL=false \
  -e FIRESTORE_DATABASE=glossarydb \
  -e FIRESTORE_COLLECTION=glossary_entries \
  translation-service:latest
```

> **Note:** Running locally requires a service account key or `gcloud auth application-default login`.
> On Cloud Run, authentication is handled automatically via ADC — no credentials file is needed.

Health check:
```bash
curl http://localhost:8080/health
```

---

## 3. Push to Artifact Registry

### 3a. Create the repository (one-time setup)

```bash
# Replace <YOUR_REGION> with your region, e.g. us-central1
gcloud artifacts repositories create translation-service \
  --repository-format=docker \
  --location=<YOUR_REGION> \
  --description="Translation Service container images"
```

### 3b. Tag and push

```bash
IMAGE=<YOUR_REGION>-docker.pkg.dev/<YOUR_GCP_PROJECT_ID>/translation-service/translation-service

# Tag
docker tag translation-service:latest ${IMAGE}:latest
docker tag translation-service:latest ${IMAGE}:1.0.0

# Push
docker push ${IMAGE}:latest
docker push ${IMAGE}:1.0.0
```

---

## 4. Deploy to Cloud Run

```bash
# Replace <YOUR_REGION> (e.g. us-central1) and <YOUR_GCP_PROJECT_ID> throughout
IMAGE=<YOUR_REGION>-docker.pkg.dev/<YOUR_GCP_PROJECT_ID>/translation-service/translation-service:latest

gcloud run deploy translation-service \
  --image "${IMAGE}" \
  --region <YOUR_REGION> \
  --platform managed \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 10 \
  --concurrency 80 \
  --cpu 1 \
  --memory 512Mi \
  --timeout 60 \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=<YOUR_GCP_PROJECT_ID>" \
  --set-env-vars "GOOGLE_CLOUD_LOCATION=<YOUR_REGION>" \
  --set-env-vars "VERTEX_AI_ENDPOINTS=<YOUR_GCP_PROJECT_ID>:<YOUR_REGION>,<YOUR_VERTEX_PROJECT_1>:<YOUR_REGION>,<YOUR_VERTEX_PROJECT_2>:<YOUR_REGION>,<YOUR_VERTEX_PROJECT_3>:<YOUR_REGION>" \
  --set-env-vars "GEMINI_MODEL=gemini-2.5-flash" \
  --set-env-vars "LOG_LEVEL=INFO" \
  --set-env-vars "LOG_TO_FILE=false" \
  --set-env-vars "LLM_MAX_RETRIES=3" \
  --set-env-vars "LLM_RETRY_DELAY_SECONDS=2.0" \
  --set-env-vars "EVAL=false" \
  --set-env-vars "FIRESTORE_DATABASE=glossarydb" \
  --set-env-vars "FIRESTORE_COLLECTION=glossary_entries"
```

After deployment, Cloud Run prints the service URL. Test it:

```bash
curl https://<SERVICE_URL>/health
curl https://<SERVICE_URL>/health/deep
```

---

## 5. IAM & Permissions

The Cloud Run service account needs the following roles to function:

| Role | Purpose |
|------|---------|
| `roles/aiplatform.user` | Call Vertex AI / Gemini endpoints |
| `roles/datastore.user` | Read/write Firestore (glossary) |

Grant them for each Vertex AI project:

```bash
SA=<PROJECT_NUMBER>-compute@developer.gserviceaccount.com

# Vertex AI — repeat for each project in VERTEX_AI_ENDPOINTS
for PROJECT in <YOUR_GCP_PROJECT_ID> <YOUR_VERTEX_PROJECT_1> <YOUR_VERTEX_PROJECT_2> <YOUR_VERTEX_PROJECT_3>; do
  gcloud projects add-iam-policy-binding ${PROJECT} \
    --member="serviceAccount:${SA}" \
    --role="roles/aiplatform.user"
done

# Firestore (glossary project)
gcloud projects add-iam-policy-binding <YOUR_GCP_PROJECT_ID> \
  --member="serviceAccount:${SA}" \
  --role="roles/datastore.user"
```

---

## 6. Updating the Service

After code changes, rebuild, re-tag, push, and redeploy:

```bash
IMAGE=<YOUR_REGION>-docker.pkg.dev/<YOUR_GCP_PROJECT_ID>/translation-service/translation-service

docker build -t ${IMAGE}:latest .
docker push ${IMAGE}:latest

gcloud run deploy translation-service \
  --image "${IMAGE}:latest" \
  --region <YOUR_REGION>
```

Cloud Run performs a zero-downtime rolling update automatically.

---

## 7. Useful Commands

```bash
# View running revisions
gcloud run revisions list --service translation-service --region <YOUR_REGION>

# Tail live logs
gcloud run services logs read translation-service --region <YOUR_REGION> --tail 50 --follow

# Roll back to a previous revision
gcloud run services update-traffic translation-service \
  --region <YOUR_REGION> \
  --to-revisions <REVISION_NAME>=100

# Inspect current environment variables
gcloud run services describe translation-service \
  --region <YOUR_REGION> \
  --format "yaml(spec.template.spec.containers[0].env)"

# Delete the service
gcloud run services delete translation-service --region <YOUR_REGION>
```

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CLOUD_PROJECT` | `<YOUR_GCP_PROJECT_ID>` | GCP project for Firestore and fallback Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | `<YOUR_REGION>` | Fallback Vertex AI region (e.g. `us-central1`, `europe-west1`) |
| `VERTEX_AI_ENDPOINTS` | _(empty)_ | Comma-separated `project:location` pairs for quota routing |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model ID |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_TO_FILE` | `false` | Write cost log to `costs_log.md` |
| `LLM_MAX_RETRIES` | `3` | Total LLM call attempts |
| `LLM_RETRY_DELAY_SECONDS` | `2.0` | Fixed delay between retries |
| `EVAL` | `false` | Enable LLM-as-Judge quality scoring |
| `GLOSSARY_ADMIN_PASSWORD` | `admin` | Password for the glossary management UI |
| `FIRESTORE_DATABASE` | `glossarydb` | Firestore database name |
| `FIRESTORE_COLLECTION` | `glossary_entries` | Firestore collection name |

> **Security:** Never pass credentials via environment variables or bake them into the image.
> Cloud Run resolves authentication automatically through Workload Identity / ADC.
> For secrets that must be stored (e.g. API keys), use [Google Secret Manager](https://cloud.google.com/secret-manager) and mount them as environment variables in the Cloud Run service configuration.
