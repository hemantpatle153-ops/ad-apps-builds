#!/usr/bin/env bash
# Puts the India relay on Cloud Run in Mumbai. Run it in Google Cloud Shell:
#   bash feeds/relay/deploy.sh [project-id]
# It prints the two values to add as GitHub secrets.
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${1:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "$PROJECT" ]; then
  echo "Your projects:"; gcloud projects list --format="value(projectId)"
  read -rp "Project ID to use: " PROJECT
fi
gcloud config set project "$PROJECT" >/dev/null
REGION=asia-south1   # Mumbai
SERVICE=feeds-relay

echo "Turning on Cloud Run and Cloud Build in $PROJECT (needs billing on; this stays in the free tier)..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# keep the same token on re-deploys so the GitHub secret stays valid
TOKEN="$(gcloud run services describe "$SERVICE" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].env[?name=="RELAY_TOKEN"].value)' 2>/dev/null || true)"
TOKEN="${TOKEN:-$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40)}"

gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --set-env-vars "RELAY_TOKEN=$TOKEN" --max-instances 2 --memory 512Mi --timeout 120 --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "Checking it can reach UPSC from Mumbai..."
curl -sS -m 60 -o /dev/null -D - -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"url":"https://upsc.gov.in/whats-new"}' | grep -i -e "^HTTP" -e "x-relay-status" || true
echo
echo "Done. Add these two secrets at"
echo "https://github.com/hemantpatle153-ops/ad-apps-builds/settings/secrets/actions/new"
echo "  FETCH_RELAY_URL   = $URL"
echo "  FETCH_RELAY_TOKEN = $TOKEN"
