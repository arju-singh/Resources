# Backend container for Cloud Run — runs the stdlib server.py (no dependencies).
# The build context is assembled by deploy-backend.sh, which materialises the
# library/ symlinks into real files so the container can serve downloads.
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# Cloud Run injects PORT (usually 8080); server.py reads it. DB_PATH, SITE_URL,
# RAZORPAY_* and SENDGRID_* are set at deploy time (deploy-backend.sh) — DB_PATH
# points at the mounted GCS volume so orders + emailed download links persist.
ENV PORT=8080

EXPOSE 8080
CMD ["python3", "server.py"]
