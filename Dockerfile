# Backend container for Cloud Run — runs the stdlib server.py (no dependencies).
# The build context is assembled by deploy-backend.sh, which materialises the
# library/ symlinks into real files so the container can serve downloads.
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# Cloud Run injects PORT (usually 8080); server.py reads it. SECURE_COOKIES=1
# makes the session cookie HTTPS-only. DB_PATH points at the mounted volume so
# accounts survive restarts (set at deploy time).
ENV PORT=8080 \
    SECURE_COOKIES=1 \
    SESSION_TTL_DAYS=30

EXPOSE 8080
CMD ["python3", "server.py"]
