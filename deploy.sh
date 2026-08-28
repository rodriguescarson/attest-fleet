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
# A DEDICATED Firestore database, so we never touch a (default) DB another app owns.
DB="${ATTEST_FIRESTORE_DATABASE:-attest-fleet}"
: "${GOOGLE_API_KEY:?set GOOGLE_API_KEY}"

gcloud config set project "$PROJECT" >/dev/null
gcloud services enable run.googleapis.com firestore.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com generativelanguage.googleapis.com

# Our own named Firestore database (Native mode) — idempotent, isolated from (default).
gcloud firestore databases describe --database="$DB" >/dev/null 2>&1 || \
  gcloud firestore databases create --database="$DB" --location="$REGION" --type=firestore-native

# Operator token for state-changing endpoints (kill switch, approvals, ticket trigger).
# This is a PUBLISHED demo credential, documented in the README: the contest rules require
# the project to be testable without restriction, and the point being demonstrated is that
# the boundary is enforced server-side, not that this string is secret.
OPERATOR_TOKEN="${ATTEST_OPERATOR_TOKEN:-attest-operator-cc3545eb9ca5}"
ADMIN_TOKEN="${ATTEST_ADMIN_TOKEN:-demo-reset-579a1daa}"

# Gemini key as a secret, never as a plain env var.
if ! gcloud secrets describe gemini-api-key >/dev/null 2>&1; then
  printf '%s' "$GOOGLE_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
else
  printf '%s' "$GOOGLE_API_KEY" | gcloud secrets versions add gemini-api-key --data-file=-
fi

# The Cloud Run runtime service account needs to read the secret and use Firestore.
SA="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding gemini-api-key --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor" >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="roles/datastore.user" --condition=None >/dev/null

gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --memory 1Gi --cpu 1 --timeout 900 --concurrency 4 --min-instances 0 --max-instances 3 \
  --set-env-vars "ATTEST_STORE=firestore,GOOGLE_CLOUD_PROJECT=$PROJECT,ATTEST_FIRESTORE_DATABASE=$DB,GOOGLE_GENAI_USE_VERTEXAI=FALSE,ATTEST_OPERATOR_TOKEN=$OPERATOR_TOKEN,ATTEST_ADMIN_TOKEN=$ADMIN_TOKEN" \
  --set-secrets "GOOGLE_API_KEY=gemini-api-key:latest"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "Deployed: $URL"
curl -fsS "$URL/health" && echo
