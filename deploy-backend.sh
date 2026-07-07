#!/usr/bin/env bash
# ===========================================================================
# Go-live deploy: Cloud Run backend + Firebase Hosting rewrites.
#
#   • Deploys server.py (auth, per-PDF payments, gated downloads) to Cloud Run.
#   • Mounts a GCS bucket for the SQLite DB so accounts/purchases persist.
#   • Reads the Razorpay SECRET from Secret Manager (never from source/args).
#   • Rewrites /api/**, /libdl, /download on the Firebase domain → Cloud Run,
#     so real login + paid downloads work at https://resource-arjusingh.web.app.
#
# See DEPLOY.md for the full runbook. In short, once (after enabling billing):
#
#   export RAZORPAY_KEY_ID=rzp_live_xxxxxxxx          # the PUBLIC key id
#   printf %s "$RAZORPAY_KEY_SECRET_VALUE" | \        # the rotated SECRET
#     gcloud secrets create razorpay-key-secret --data-file=- --project resource-arjusingh
#   ./deploy-backend.sh
# ===========================================================================
set -euo pipefail

PROJECT="resource-arjusingh"
REGION="us-central1"
SERVICE="eyn-backend"
BUCKET="${PROJECT}-eyn-data"          # persistent SQLite DB
SECRET_NAME="razorpay-key-secret"     # Secret Manager entry for the Razorpay secret
ROOT="$(cd "$(dirname "$0")" && pwd)"
STAGE="$(mktemp -d)/eyn-build"

echo "▶ Project $PROJECT · region $REGION · service $SERVICE"
gcloud config set project "$PROJECT" >/dev/null

# --- 0. Billing must be on (Cloud Run/Build need it) --------------------------
if [[ "$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null)" != "True" ]]; then
  echo "✋ Billing is NOT enabled on $PROJECT — Cloud Run can't run without it."
  echo "   Enable the Blaze plan, then re-run:"
  echo "   https://console.firebase.google.com/project/$PROJECT/usage/details"
  exit 1
fi

# --- 1. APIs ------------------------------------------------------------------
echo "▶ Enabling APIs…"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com secretmanager.googleapis.com

# --- 2. Persistent DB bucket --------------------------------------------------
echo "▶ Ensuring DB bucket gs://$BUCKET…"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access

# --- 3. Razorpay wiring (optional — deploy works without it; payments off) ----
# All env vars go in ONE --update-env-vars string (repeating the flag makes gcloud
# keep only the last one). The secret is injected separately via --set-secrets.
# No accounts/cookies anymore; DB (orders+grants) + admin uploads live on the mounted
# GCS volume so download links and uploaded files survive restarts. SITE_URL builds
# the links in emails.
ENV_VARS="DB_PATH=/data/shop.db,LIBFILES_DIR=/app/library-files,UPLOAD_DIR=/data/uploads,RESOURCES_PATH=/data/resources.json,TRUST_PROXY=1,SITE_URL=https://resource-arjusingh.web.app"
PAY_FLAGS=()

# Admin panel (optional). Export ADMIN_PASSWORD before running to enable /admin.html.
# ADMIN_SECRET signs admin sessions; a stable value keeps you logged in across restarts.
if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
  ADMIN_SECRET_VAL="${ADMIN_SECRET:-$(openssl rand -hex 32 2>/dev/null || echo "eyn-admin-secret")}"
  ENV_VARS="${ENV_VARS},ADMIN_PASSWORD=${ADMIN_PASSWORD},ADMIN_SECRET=${ADMIN_SECRET_VAL}"
  echo "▶ Admin panel: ON (/admin.html)"
else
  echo "▶ Admin panel: OFF (export ADMIN_PASSWORD before running to enable /admin.html)"
fi
if gcloud secrets describe "$SECRET_NAME" >/dev/null 2>&1; then
  if [[ -z "${RAZORPAY_KEY_ID:-}" ]]; then
    echo "✋ Secret '$SECRET_NAME' exists but RAZORPAY_KEY_ID (public key id) isn't set."
    echo "   export RAZORPAY_KEY_ID=rzp_live_xxxx   then re-run."
    exit 1
  fi
  # Let the Cloud Run runtime service account read the secret.
  PNUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  RUNTIME_SA="${PNUM}-compute@developer.gserviceaccount.com"
  gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
  ENV_VARS="${ENV_VARS},RAZORPAY_KEY_ID=${RAZORPAY_KEY_ID}"
  PAY_FLAGS+=(--set-secrets "RAZORPAY_KEY_SECRET=${SECRET_NAME}:latest")
  echo "▶ Payments: ON (key ${RAZORPAY_KEY_ID}, secret from Secret Manager)"
else
  echo "▶ Payments: OFF (no '$SECRET_NAME' secret yet — pages still serve; buy button says 'not live')."
  echo "  Add it later per DEPLOY.md and re-run to enable paid downloads."
fi

# --- 3b. Email delivery (optional — Resend, for the download-link emails) ------
# Create the secret once:  printf %s "re_xxx" | gcloud secrets create resend-api-key --data-file=-
if gcloud secrets describe "resend-api-key" >/dev/null 2>&1; then
  PNUM="${PNUM:-$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')}"
  RUNTIME_SA="${PNUM}-compute@developer.gserviceaccount.com"
  gcloud secrets add-iam-policy-binding "resend-api-key" \
    --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
  ENV_VARS="${ENV_VARS},MAIL_FROM=Everything You Need <connect@arjusingh.com>"
  PAY_FLAGS+=(--set-secrets "RESEND_API_KEY=resend-api-key:latest")
  echo "▶ Email: ON (download links sent via Resend)"
else
  echo "▶ Email: OFF (no 'resend-api-key' secret — link is shown on-screen after payment instead)."
fi

# --- 4. Build context: materialise the library symlinks into real files -------
echo "▶ Staging build context…"
mkdir -p "$STAGE/library-files"
cp "$ROOT"/server.py "$ROOT"/Dockerfile "$ROOT"/.gcloudignore "$ROOT"/pricing.json "$STAGE"/
cp "$ROOT"/*.html "$ROOT"/robots.txt "$ROOT"/sitemap.xml "$STAGE"/ 2>/dev/null || true
cp -R "$ROOT"/static "$STAGE"/static
# The paid, slug-named files the /dl gate serves. These are real files (no symlinks).
cp -RL "$ROOT"/library-files/. "$STAGE"/library-files/
echo "  staged $(find "$STAGE/library-files" -type f | wc -l | tr -d ' ') files ($(du -sh "$STAGE/library-files" | cut -f1))"

# --- 5. Deploy to Cloud Run (Cloud Build builds the image — no local Docker) --
echo "▶ Deploying to Cloud Run…"
gcloud run deploy "$SERVICE" \
  --source "$STAGE" \
  --region "$REGION" \
  --allow-unauthenticated \
  --memory 512Mi --cpu 1 \
  --min-instances 0 --max-instances 3 \
  --concurrency 40 --timeout 120 \
  --add-volume "name=dbvol,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=dbvol,mount-path=/data" \
  --update-env-vars "$ENV_VARS" \
  ${PAY_FLAGS[@]+"${PAY_FLAGS[@]}"}

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo "▶ Cloud Run live: $URL"

# --- 6. Point the Firebase domain at the backend (rewrites) -------------------
echo "▶ Deploying Firebase Hosting with rewrites…"
firebase deploy --only hosting --config firebase.backend.json --project "$PROJECT"

rm -rf "$(dirname "$STAGE")"
echo ""
echo "✅ Live at https://${PROJECT}.web.app  —  login + paid downloads working."
echo "   Backend: $URL"
