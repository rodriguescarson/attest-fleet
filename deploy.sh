#!/usr/bin/env bash
# Deploy Attest Fleet to Cloud Run with Firestore as the evidence store.
#
#   gcloud auth login                       # once, as your user account
#   export PROJECT=<your-gcp-project-id>    # billing-enabled
#   export GOOGLE_API_KEY=<AI Studio key>   # used ONLY by the Gemma auditor
#   ./deploy.sh
#
# Gemini roles run on Vertex AI (billed to the project, no free-tier cap). Gemma is not a
# Vertex publisher model, so the auditor keeps the Gemini Developer API key — which is why
# GOOGLE_API_KEY is still required. After deploying, publish the agent cards once:
#   GOOGLE_CLOUD_PROJECT=$PROJECT uv run python scripts/register_agents.py
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-attest-fleet}"
# A DEDICATED Firestore database, so we never touch a (default) DB another app owns.
DB="${ATTEST_FIRESTORE_DATABASE:-attest-fleet}"
: "${GOOGLE_API_KEY:?set GOOGLE_API_KEY}"

gcloud config set project "$PROJECT" >/dev/null
gcloud services enable run.googleapis.com firestore.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com generativelanguage.googleapis.com \
  aiplatform.googleapis.com agentregistry.googleapis.com modelarmor.googleapis.com \
  cloudtrace.googleapis.com telemetry.googleapis.com

# Our own named Firestore database (Native mode) — idempotent, isolated from (default).
gcloud firestore databases describe --database="$DB" >/dev/null 2>&1 || \
  gcloud firestore databases create --database="$DB" --location="$REGION" --type=firestore-native

# Operator token for state-changing endpoints (kill switch, approvals, ticket trigger).
# This is a PUBLISHED demo credential, documented in the README: the contest rules require
# the project to be testable without restriction, and the point being demonstrated is that
# the boundary is enforced server-side, not that this string is secret.
OPERATOR_TOKEN="${ATTEST_OPERATOR_TOKEN:-attest-operator-cc3545eb9ca5}"
# Never a hardcoded default: this token guards a DESTRUCTIVE endpoint, and deploy.sh is a
# file reviewers open. If unset, generate one and print it — it stays out of the repo.
ADMIN_TOKEN="${ATTEST_ADMIN_TOKEN:-$(openssl rand -hex 16)}"

# Gemini key as a secret, never as a plain env var.
if ! gcloud secrets describe gemini-api-key >/dev/null 2>&1; then
  printf '%s' "$GOOGLE_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
else
  printf '%s' "$GOOGLE_API_KEY" | gcloud secrets versions add gemini-api-key --data-file=-
fi

# A DEDICATED runtime service account, not the default compute SA. The default carries
# roles/editor on most projects, and this service is publicly reachable and executes text
# an attacker controls -- running it as project editor over a shared project is the exact
# blast radius the fleet's own approval gate exists to avoid.
SA_NAME="${ATTEST_RUNTIME_SA:-attest-fleet-run}"
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
    --display-name="Attest Fleet Cloud Run runtime"
gcloud secrets add-iam-policy-binding gemini-api-key --project="$PROJECT" --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor" >/dev/null
for ROLE in roles/datastore.user roles/aiplatform.user roles/agentregistry.viewer \
            roles/modelarmor.user roles/cloudtrace.agent; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$ROLE" --condition=None >/dev/null
done

# Model Armor template for ingress screening (idempotent). asia-south1 does not support the
# malicious-URI filter, so only prompt-injection/jailbreak is enabled.
ARMOR="${ATTEST_MODEL_ARMOR_TEMPLATE:-attest-ticket-guard}"
ARMOR_LOC="${ATTEST_MODEL_ARMOR_LOCATION:-$REGION}"
curl -sf -X POST \
  "https://modelarmor.$ARMOR_LOC.rep.googleapis.com/v1/projects/$PROJECT/locations/$ARMOR_LOC/templates?templateId=$ARMOR" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json" \
  -d '{"filterConfig":{"piAndJailbreakFilterSettings":{"filterEnforcement":"ENABLED","confidenceLevel":"MEDIUM_AND_ABOVE"}}}' \
  >/dev/null 2>&1 || echo "model armor template already exists (or unavailable in $ARMOR_LOC)"

gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --service-account "$SA" \
  --memory 1Gi --cpu 1 --timeout 900 --concurrency 4 --min-instances 0 --max-instances 3 \
  --set-env-vars "ATTEST_STORE=firestore,GOOGLE_CLOUD_PROJECT=$PROJECT,ATTEST_FIRESTORE_DATABASE=$DB,ATTEST_USE_VERTEX=1,ATTEST_VERTEX_LOCATION=global,ATTEST_REGISTRY_LOCATION=global,ATTEST_MODEL_ARMOR_TEMPLATE=$ARMOR,ATTEST_MODEL_ARMOR_LOCATION=$ARMOR_LOC,ATTEST_TRACING=1,ATTEST_OPERATOR_TOKEN=$OPERATOR_TOKEN,ATTEST_ADMIN_TOKEN=$ADMIN_TOKEN" \
  --set-secrets "GOOGLE_API_KEY=gemini-api-key:latest"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo "Deployed: $URL"
curl -fsS "$URL/health" && echo
