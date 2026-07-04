#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-shot production deploy: Cloud Run backend + Firebase Hosting rewrites.
#
#   1. Deploys server.py (auth + gated downloads) to Cloud Run.
#   2. Mounts a GCS bucket for the SQLite DB so accounts survive restarts.
#   3. Rewrites /api/**, /libdl, /download on the Firebase domain to Cloud Run,
#      so real login + downloads work at https://resource-arjusingh.web.app.
#
# PREREQUISITE (one-time, yours to do — it needs a card):
#   Enable the Blaze (pay-as-you-go) plan on the project:
#     https://console.firebase.google.com/project/resource-arjusingh/usage/details
#   Cloud Run's free tier (2M requests/mo) covers a personal site; you pay only
#   for egress on downloads (~$0.12/GB) and DB storage (pennies).
#
# Then just run:  ./deploy-backend.sh
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT="resource-arjusingh"
REGION="us-central1"
SERVICE="eyn-backend"
BUCKET="${PROJECT}-eyn-data"     # holds the persistent users.db
ROOT="$(cd "$(dirname "$0")" && pwd)"
STAGE="$(mktemp -d)/eyn-build"

echo "▶ Project: $PROJECT   Region: $REGION   Service: $SERVICE"
gcloud config set project "$PROJECT" >/dev/null

echo "▶ Enabling required APIs (run, build, artifactregistry, storage)…"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com

echo "▶ Ensuring DB bucket gs://$BUCKET exists…"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access

echo "▶ Assembling build context (materialising library symlinks → real files)…"
mkdir -p "$STAGE/library"
# App code + assets
cp "$ROOT"/server.py "$ROOT"/Dockerfile "$ROOT"/.gcloudignore "$STAGE"/
cp "$ROOT"/*.html "$ROOT"/robots.txt "$ROOT"/sitemap.xml "$STAGE"/ 2>/dev/null || true
cp -R "$ROOT"/static "$STAGE"/static
# -L dereferences the symlinks so the container gets the real PDFs/files
cp -RL "$ROOT"/library/. "$STAGE"/library/
echo "  library files staged: $(find "$STAGE/library" -type f | wc -l | tr -d ' ')  ($(du -sh "$STAGE/library" | cut -f1))"

echo "▶ Deploying to Cloud Run (builds in the cloud — no local Docker needed)…"
gcloud run deploy "$SERVICE" \
  --source "$STAGE" \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 512Mi --cpu 1 \
  --min-instances 0 --max-instances 3 \
  --concurrency 40 --timeout 120 \
  --add-volume "name=dbvol,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=dbvol,mount-path=/data" \
  --set-env-vars "SECURE_COOKIES=1,DB_PATH=/data/users.db,LIBRARY_DIR=/app/library,TRUST_PROXY=1"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo "▶ Cloud Run live at: $URL"

echo "▶ Pointing the Firebase domain at the backend (rewrites)…"
firebase deploy --only hosting --config firebase.backend.json --project "$PROJECT"

echo ""
echo "✅ Done. Login + downloads now work at https://${PROJECT}.web.app"
echo "   Backend: $URL"
rm -rf "$(dirname "$STAGE")"
