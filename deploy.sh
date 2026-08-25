#!/usr/bin/env bash
# Deploy Attest Fleet to Cloud Run with Firestore as the evidence store.
#
#   gcloud auth login                       # once, as your user account
#   export PROJECT=<your-gcp-project-id>    # billing-enabled
#   export GOOGLE_API_KEY=<AI Studio key>   # or switch to Vertex below
#   ./deploy.sh
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-attest-fleet}"
: "${GOOGLE_API_KEY:?set GOOGLE_API_KEY}"

gcloud config set project "$PROJECT" >/dev/null
gcloud services enable run.googleapis.com firestore.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com generativelanguage.googleapis.com

# Firestore (Native mode) — idempotent.
gcloud firestore databases describe --database="(default)" >/dev/null 2>&1 || \
  gcloud firestore databases create --database="(default)" --location="$REGION" --type=firestore-native

# Gemini key as a secret, never as a plain env var.
if ! gcloud secrets describe gemini-api-key >/dev/null 2>&1; then
  printf '%s' "$GOOGLE_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
else
  printf '%s' "$GOOGLE_API_KEY" | gcloud secrets versions add gemini-api-key --data-file=-
fi

gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --memory 1Gi --cpu 1 --timeout 900 --concurrency 4 --min-instances 0 --max-instances 3 \
  --set-env-vars "ATTEST_STORE=firestore,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_GENAI_USE_VERTEXAI=FALSE" \
  --set-secrets "GOOGLE_API_KEY=gemini-api-key:latest"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "Deployed: $URL"
curl -fsS "$URL/healthz" && echo
