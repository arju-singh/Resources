# Deploy & Go-Live Runbook — Everything You Need

The site is static HTML + a Python backend (`server.py`) that does **auth, per-PDF
payments (Razorpay), and gated downloads**. Locally it all runs from one process.
In production we split it:

```
 Browser ──► Firebase Hosting (CDN)  ── static HTML/CSS/JS, library-data.js
        └──► /api/**, /libdl ─(rewrite)─► Cloud Run (server.py)  ── auth, payments,
                                                                     file downloads
                                          └─ GCS bucket ── users.db (persistent)
                                          └─ Secret Manager ── Razorpay secret
```

Same domain (`resource-arjusingh.web.app`), so cookies "just work" and there's no CORS.

---

## What you do once (the three gates I can't cross for you)

### 1. Rotate the Razorpay keys  🔴 do this first
A live secret was shared in chat, so treat it as **compromised**.
Razorpay Dashboard → **Settings → API Keys → Regenerate**. Keep the new:
- **Key Id** — `rzp_live_…` (public, safe to expose)
- **Key Secret** — secret (goes into Secret Manager below; never into code/git)

### 2. Enable billing (Blaze)
Cloud Run needs the pay-as-you-go plan:
👉 https://console.firebase.google.com/project/resource-arjusingh/usage/details
Free tier covers a personal site (2M requests/mo); you pay only for download
egress (~₹10/GB) and a few pennies of storage.

### 3. Put the Razorpay secret in Secret Manager
So the secret never sits in a file, a flag, or shell history:
```bash
gcloud config set project resource-arjusingh
printf %s 'PASTE_YOUR_ROTATED_SECRET' | \
  gcloud secrets create razorpay-key-secret --data-file=-
# (updating later: ... | gcloud secrets versions add razorpay-key-secret --data-file=-)
```

---

## Deploy

```bash
export RAZORPAY_KEY_ID=rzp_live_xxxxxxxxxxxx   # your PUBLIC key id
./scripts/deploy-backend.sh
```

The script: checks billing → enables APIs → creates the DB bucket → grants the
runtime access to the secret → bakes the library files into the image → deploys
Cloud Run → points the Firebase domain at it. ~5–8 min (first run uploads ~450 MB).

> No `RAZORPAY_KEY_ID` / secret yet? The script still deploys — **auth and free
> downloads work, payments stay off** until you add them and re-run.

---

## Verify (after it finishes)

```bash
curl -s https://resource-arjusingh.web.app/api/pay-config      # {"configured":true,...}
# In the browser: sign up → download a FREE resource → buy a paid one (use a
# Razorpay TEST key first to click through without real money).
```
Checklist: free PDF downloads after login · paid PDF shows **Buy · ₹X** · after
paying it flips to **✓ Purchased** and downloads · a signed-out user is asked to
sign in.

---

## Local development

```bash
# free/auth only:
PORT=3011 python3 server.py

# with payments (use Razorpay TEST keys — never live keys locally):
RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=xxx PORT=3011 python3 server.py
open http://localhost:3011/
```
Prices live in **`pricing.json`** (filename → rupees, `0` = free) — the server's
source of truth. Edit there; `static/library-data.js` carries a `price` field for
display only.

---

## Rough cost (personal traffic)
| Item | Cost |
|------|------|
| Cloud Run requests | Free tier (2M/mo) → ~₹0 |
| Download egress | ~₹10 / GB served |
| GCS DB storage | < ₹5 / mo |
| Secret Manager | ~₹0 |

Scale-to-zero (`--min-instances 0`) means you pay nothing while idle; first hit
after idle has a ~2–4 s cold start.

---

## Troubleshooting
- **`billing is NOT enabled`** → do gate #2, re-run.
- **Payments show "coming soon"** → secret/`RAZORPAY_KEY_ID` missing; do gate #1/#3, re-run.
- **402 on a paid file after paying** → check the Cloud Run logs; verify the GCS
  volume mounted (`/data/users.db`) so the purchase persisted.
- **Rewrites 404** → the Cloud Run service name/region in `firebase.backend.json`
  must match `deploy-backend.sh` (`eyn-backend` / `us-central1`).

## Rollback
Static only (drops the backend rewrites; login/downloads go offline but the site
loads): `firebase deploy --only hosting` (uses the plain `firebase.json`).
Roll back the service: `gcloud run services update-traffic eyn-backend --to-revisions=PREV=100 --region us-central1`.

## Notes
- SQLite on a mounted bucket is fine for one low-traffic instance
  (`--max-instances 3`, low concurrency). If this grows, move accounts/purchases
  to Firestore or Cloud SQL.
- Payment confirmation uses Razorpay's signed handler callback (verified
  server-side). For extra robustness against dropped callbacks, add a Razorpay
  **webhook** to `/api/verify-payment`-style endpoint later.
- If any library PDF is third-party/copyrighted, confirm you have the right to
  sell it before going live.
