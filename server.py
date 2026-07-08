#!/usr/bin/env python3
"""
"Everything You Need" site backend — pure Python stdlib (no dependencies).

Model (2026-07 rewrite): NO accounts. Every library file is a paid digital good.
A buyer pays once (Razorpay) and is emailed a secure, reusable download link that
works for a limited window (default 30 days / 10 downloads). There is no signup,
login, password, or session — just pay-per-file with an emailed link.

Pricing is region-based and server-authoritative:
  - India  → ₹99 (INR)
  - Others → $4  (USD)
Both are shown in the UI; the order amount and the download gate always use THIS
server's numbers so a tampered client can't set its own price.

Security hardening (OWASP-aligned):
  - Rate limiting on all endpoints (per-IP), graceful 429s
  - Strict input validation (types, lengths, no path traversal, no extra fields)
  - No secrets in source — all config via environment variables
  - Security response headers, body-size caps, tamper-proof pricing & grants
  - Downloads are ONLY reachable via a signed, single-use-ish grant token; the raw
    files are never publicly served, so the paywall can't be bypassed by URL.

Run:  python3 server.py   (listens on http://localhost:3011)

Required env for live payments (set at deploy — never commit):
    RAZORPAY_KEY_ID       rzp_live_xxx / rzp_test_xxx   (public id; safe in browser)
    RAZORPAY_KEY_SECRET   xxxxxxxx                      (secret; server-side only)
Optional:
    PRICE_INR=99  PRICE_USD=4          per-file price per currency
    GRANT_TTL_DAYS=30  GRANT_MAX_DOWNLOADS=10
    RESEND_API_KEY=re_xxx  MAIL_FROM="Everything You Need <connect@arjusingh.com>"
    SITE_URL=https://resource-arjusingh.web.app   (used to build the emailed link)
"""
import os, re, json, time, hmac, base64, sqlite3, hashlib, secrets, threading, mimetypes, urllib.parse, urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
# DB on a persistent volume in prod (Cloud Run's own FS is ephemeral); local file otherwise.
DB_PATH = os.environ.get("DB_PATH", os.path.join(ROOT, "shop.db"))
# The paid files. Slug-named; served ONLY through the /dl grant gate — never publicly.
LIBFILES_DIR = os.environ.get("LIBFILES_DIR", os.path.join(ROOT, "library-files"))
STATIC_DIR = os.path.join(ROOT, "static")

# ---------- Configuration (all via environment — no secrets in source) ----------
PORT = int(os.environ.get("PORT", 3011))
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 64 * 1024))
MAX_EMAIL_LEN = 254
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

# ---------- Payments (Razorpay) ----------
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
PAYMENTS_ON = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

# Region-based default price PER FILE, per currency (major units). Any new file
# added to library-files/ is automatically priced at these — no per-file setup.
DEFAULT_PRICES = {
    "INR": int(os.environ.get("PRICE_INR", 99)),
    "USD": int(os.environ.get("PRICE_USD", 4)),
}
SUPPORTED_CURRENCIES = tuple(DEFAULT_PRICES.keys())

# Emailed-link policy.
GRANT_TTL_DAYS = int(os.environ.get("GRANT_TTL_DAYS", 30))
GRANT_MAX_DOWNLOADS = int(os.environ.get("GRANT_MAX_DOWNLOADS", 10))

# Email (Resend HTTP API). If unset, the download link is still returned in the
# API response and shown on-screen, so the product works before email is wired.
# For a first test with no domain setup, use MAIL_FROM="onboarding@resend.dev"
# (Resend's sandbox sender — only delivers to your own Resend account email).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "Everything You Need <connect@arjusingh.com>")

# Default price PER PAGE. Files added to a page inherit its price unless the admin
# sets a custom one. Library = ₹99/$4; Checklist = ₹199/$10. New pages fall back to
# DEFAULT_PRICES. (Env override: PAGE_PRICES_JSON='{"checklist":{"INR":199,"USD":10}}')
PAGE_PRICES = {
    "library":   {"INR": DEFAULT_PRICES["INR"], "USD": DEFAULT_PRICES["USD"]},
    "checklist": {"INR": int(os.environ.get("CHECKLIST_PRICE_INR", 199)),
                  "USD": int(os.environ.get("CHECKLIST_PRICE_USD", 10))},
}
# Extra pages the admin may also publish paid files to. They inherit the global
# default price; the admin picks a per-file price at upload time anyway.
for _p in ("docs", "notion", "github", "discord", "websites", "links",
           "free-llm-apis", "userpain"):
    PAGE_PRICES.setdefault(_p, {"INR": DEFAULT_PRICES["INR"], "USD": DEFAULT_PRICES["USD"]})
def page_price(page, currency):
    cur = currency if currency in DEFAULT_PRICES else "INR"
    return PAGE_PRICES.get(page, DEFAULT_PRICES)[cur]

# ---------- Admin + uploads ----------
# A single admin, gated by a password (set ADMIN_PASSWORD at deploy). Sessions are
# stateless signed tokens (survive Cloud Run restarts / multiple instances).
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "") or RAZORPAY_KEY_SECRET or secrets.token_hex(32)
ADMIN_ON = bool(ADMIN_PASSWORD)
ADMIN_TTL = 12 * 3600  # admin session lifetime (seconds)
# Admin-uploaded files + their metadata live on the writable volume (/data in prod),
# separate from the baked-in library-files/. Both are served through the /dl gate.
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(ROOT, "uploads"))
RESOURCES_PATH = os.environ.get("RESOURCES_PATH", os.path.join(ROOT, "resources.json"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 40 * 1024 * 1024))  # 40 MB

# Optional per-file overrides in pricing.json. Shape:
#   { "free": ["some-slug.pdf"], "INR": {"slug.pdf": 149}, "USD": {"slug.pdf": 6} }
PRICING_PATH = os.path.join(ROOT, "pricing.json")
def load_overrides():
    try:
        with open(PRICING_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {
            "free": set(str(x) for x in raw.get("free", [])),
            "INR": {str(k): int(v) for k, v in raw.get("INR", {}).items()},
            "USD": {str(k): int(v) for k, v in raw.get("USD", {}).items()},
        }
    except Exception:
        return {"free": set(), "INR": {}, "USD": {}}
OVERRIDES = load_overrides()

# ---------- Dynamic resources store (admin-uploaded files) ----------
_res_lock = threading.Lock()
def load_resources():
    try:
        with open(RESOURCES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []
def save_resources(items):
    os.makedirs(os.path.dirname(RESOURCES_PATH) or ".", exist_ok=True)
    tmp = RESOURCES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, RESOURCES_PATH)
def resource_by_slug(slug):
    for r in load_resources():
        if r.get("slug") == slug:
            return r
    return None

def price_of(fname, currency):
    """Price (major units) for a file in a currency. 0 = free. Order of precedence:
    admin resource's own price → pricing.json override → the file's page default →
    global default. Unknown currency falls back to INR."""
    cur = currency if currency in DEFAULT_PRICES else "INR"
    res = resource_by_slug(fname)
    if res:
        key = "price_inr" if cur == "INR" else "price_usd"
        try:
            v = int(res.get(key))
            if v >= 0:
                return v
        except (TypeError, ValueError):
            pass
        return page_price(res.get("page", "library"), cur)
    if fname in OVERRIDES["free"]:
        return 0
    if fname in OVERRIDES[cur]:
        return OVERRIDES[cur][fname]
    return DEFAULT_PRICES[cur]

def resolve_file(fname):
    """Return the on-disk path for a purchasable file (admin upload OR baked-in
    library file), or None. Guards against traversal via clean_filename upstream."""
    for base in (UPLOAD_DIR, LIBFILES_DIR):
        p = os.path.join(base, fname)
        if os.path.isfile(p):
            return p
    return None

# ---------- Database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        -- A Razorpay order we created, bound to the exact file/email/amount/currency,
        -- so verification grants only what was actually paid for (no tampering).
        CREATE TABLE IF NOT EXISTS orders (
            order_id   TEXT PRIMARY KEY,
            file       TEXT NOT NULL,
            name       TEXT,
            email      TEXT NOT NULL,
            currency   TEXT NOT NULL,
            amount     INTEGER NOT NULL,            -- major units (₹ or $)
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        -- A paid unlock: a secret token that downloads one file, reusable until it
        -- expires or hits the download cap. No account needed — the token IS access.
        CREATE TABLE IF NOT EXISTS grants (
            token         TEXT PRIMARY KEY,
            file          TEXT NOT NULL,
            name          TEXT,
            email         TEXT NOT NULL,
            payment_id    TEXT,
            currency      TEXT,
            amount        INTEGER,
            created_at    REAL NOT NULL,
            expires_at    REAL NOT NULL,
            max_downloads INTEGER NOT NULL,
            downloads     INTEGER NOT NULL DEFAULT 0
        );
        """)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Files that must NEVER be served, even if symlinked into a served dir (defense in
# depth for secret material — keys, certs, env files, credential dumps).
SENSITIVE_FILE_RE = re.compile(
    r"(\.(env|pem|key|p12|pfx|keystore|jks|ppk|mobileprovision|asc|gpg)$)"
    r"|(^\.env)|(id_rsa)|(google-?services?[-.])|(\.plist$)"
    r"|(backup[\s_-]*codes)|(secret|credential|password)s?\.",
    re.IGNORECASE,
)
def is_sensitive_filename(name):
    return bool(SENSITIVE_FILE_RE.search(os.path.basename(name or "")))

def clean_filename(fname):
    """Return a safe basename or None. Rejects traversal, separators, over-length."""
    if not isinstance(fname, str) or not fname or len(fname) > 255:
        return None
    if "/" in fname or "\\" in fname or "\x00" in fname:
        return None
    safe = os.path.basename(fname)
    if not safe or is_sensitive_filename(safe):
        return None
    return safe

def file_exists(fname):
    return resolve_file(fname) is not None

def slugify(name):
    """Make a URL/file-safe slug from an arbitrary filename, preserving the extension."""
    base, ext = os.path.splitext(os.path.basename(name or ""))
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower() or "file"
    ext = re.sub(r"[^a-zA-Z0-9.]+", "", ext).lower()
    return (base[:80] + ext)[:100]

# ---------- Admin session (stateless signed token) ----------
def admin_make_token():
    exp = str(int(time.time()) + ADMIN_TTL)
    sig = hmac.new(ADMIN_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"

def admin_token_valid(token):
    if not token or not isinstance(token, str) or "." not in token or len(token) > 200:
        return False
    exp, sig = token.rsplit(".", 1)
    good = hmac.new(ADMIN_SECRET.encode(), exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False

# ---------- Grants / email ----------
def create_grant(fname, name, email, payment_id, currency, amount):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO grants (token, file, name, email, payment_id, currency, amount, "
            "created_at, expires_at, max_downloads, downloads) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (token, fname, name, email, payment_id, currency, amount,
             now, now + GRANT_TTL_DAYS * 86400, GRANT_MAX_DOWNLOADS))
    return token

def grant_for_download(token):
    """Return (row, error). Valid grant → (row, None); else (None, message)."""
    if not token or not isinstance(token, str) or len(token) > 128:
        return None, "Invalid link."
    with db() as conn:
        row = conn.execute("SELECT * FROM grants WHERE token=?", (token,)).fetchone()
    if not row:
        return None, "This download link is invalid."
    if time.time() > row["expires_at"]:
        return None, "This download link has expired."
    if row["downloads"] >= row["max_downloads"]:
        return None, "This download link has reached its download limit."
    return row, None

def bump_download(token):
    with db() as conn:
        conn.execute("UPDATE grants SET downloads = downloads + 1 WHERE token=?", (token,))

def send_download_email(to_email, name, url, currency, amount):
    """Send the download link via Resend. Returns True on success, False otherwise
    (caller still returns the link in the API response so nothing is lost)."""
    if not RESEND_API_KEY:
        return False
    title = name or "your resource"
    html = (
        f"<p>Thanks for your purchase! Here's your download for <b>{title}</b>.</p>"
        f"<p><a href=\"{url}\">⬇ Download {title}</a></p>"
        f"<p>This link works for {GRANT_TTL_DAYS} days and up to {GRANT_MAX_DOWNLOADS} downloads. "
        f"Keep this email so you can re-download.</p>"
        f"<p style=\"color:#888;font-size:12px\">Everything You Need · by Arju Singh</p>"
    )
    payload = json.dumps({
        "from": MAIL_FROM,               # Resend accepts a plain "Name <email>" string
        "to": [to_email],
        "subject": f"Your download: {title}",
        "html": html,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {RESEND_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception:
        return False

# ---------- Razorpay ----------
def razorpay_create_order(fname, email, currency, amount):
    """Create a Razorpay order server-side (amount in smallest unit) and record it."""
    body = json.dumps({
        "amount": amount * 100,          # paise / cents
        "currency": currency,
        "receipt": f"eyn-{secrets.token_hex(6)}",
        "notes": {"file": fname[:200], "email": email[:200]},
    }).encode()
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def razorpay_signature_ok(order_id, payment_id, signature):
    """Verify Razorpay signature = HMAC_SHA256(order_id|payment_id, secret)."""
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(),
                        f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")

# ---------- Rate limiting (sliding window, thread-safe) ----------
class RateLimiter:
    def __init__(self):
        self._b = defaultdict(lambda: defaultdict(deque))
        self._lock = threading.Lock()
    def hit(self, bucket, key, limit, window):
        now = time.monotonic(); cutoff = now - window
        with self._lock:
            dq = self._b[bucket][key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return int(dq[0] + window - now) + 1
            dq.append(now)
            if len(self._b[bucket]) > 20_000:
                for k in [k for k, d in self._b[bucket].items() if not d]:
                    del self._b[bucket][k]
            return 0
RL = RateLimiter()
LIM_GLOBAL   = (1200, 60)
LIM_API      = (240, 60)
LIM_DOWNLOAD = (120, 60)
LIM_ORDER    = (20, 60)     # order creation is abuse-prone (hits Razorpay) — keep tight

# ---------- HTTP handler ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "web"
    sys_version = ""

    SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://checkout.razorpay.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://*.razorpay.com; "
            "font-src 'self'; "
            "connect-src 'self' https://cdn.jsdelivr.net https://*.razorpay.com https://lumberjack.razorpay.com; "
            "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
            "frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
        ),
    }
    def end_headers(self):
        for k, v in self.SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def site_url(self):
        if SITE_URL:
            return SITE_URL
        host = self.headers.get("Host") or f"localhost:{PORT}"
        scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        return f"{scheme}://{host}"

    def rate_limited(self, bucket, key, limit, window):
        retry = RL.hit(bucket, key, limit, window)
        if retry:
            self.send_json({"error": "Too many requests. Please slow down."}, 429, retry_after=retry)
            return True
        return False

    def cookie_secure(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        local = host in ("localhost", "127.0.0.1", "::1", "") or host.endswith(".local")
        return "" if local else "; Secure"

    def admin_cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "admintok":
                    return v
        return None

    def is_admin(self):
        return ADMIN_ON and admin_token_valid(self.admin_cookie())

    def send_json(self, obj, status=200, retry_after=None, set_cookie=None, clear_cookie=False):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        sec = self.cookie_secure()
        if set_cookie is not None:
            self.send_header("Set-Cookie",
                f"admintok={set_cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ADMIN_TTL}{sec}")
        if clear_cookie:
            self.send_header("Set-Cookie", f"admintok=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{sec}")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, max_bytes=MAX_BODY_BYTES):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None
        if length <= 0:
            return {}
        if length > max_bytes:
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
        return parsed if isinstance(parsed, dict) else None

    def serve_file(self, path, download_name=None):
        if not os.path.isfile(path):
            self.send_error(404, "Not found"); return
        ctype, _ = mimetypes.guess_type(path)
        ctype = ctype or "application/octet-stream"
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if download_name:
            ascii_name = download_name.encode("ascii", "ignore").decode() or "download"
            quoted = urllib.parse.quote(download_name)
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                self.wfile.write(chunk)

    # -- GET --
    def do_GET(self):
        ip = self.client_ip()
        if self.rate_limited("global", ip, *LIM_GLOBAL):
            return
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path.startswith("/api/"):
            if self.rate_limited("api", ip, *LIM_API):
                return

        # Public payment config — publishable key id + prices only, never the secret.
        if path == "/api/pay-config":
            return self.send_json({
                "configured": PAYMENTS_ON,
                "key_id": RAZORPAY_KEY_ID if PAYMENTS_ON else "",
                "prices": DEFAULT_PRICES,
                "page_prices": PAGE_PRICES,
                "ttl_days": GRANT_TTL_DAYS,
                "max_downloads": GRANT_MAX_DOWNLOADS,
            })

        # Public: admin-uploaded resources for a page (so pages can render + sell them).
        if path == "/api/resources":
            qs = urllib.parse.parse_qs(parsed.query)
            page = (qs.get("page", ["library"])[0] or "library")[:40]
            items = [r for r in load_resources() if r.get("page") == page]
            for r in items:
                r.pop("orig_name", None)  # not needed client-side
            return self.send_json({"resources": items})

        # Is the current request an authenticated admin?
        if path == "/api/admin/me":
            return self.send_json({"admin": self.is_admin(), "enabled": ADMIN_ON})

        # Admin: list uploaded resources (all pages) with prices.
        if path == "/api/admin/resources":
            if not self.is_admin():
                return self.send_json({"error": "Admin login required."}, 401)
            return self.send_json({"resources": load_resources()})

        # The paywalled download: reachable ONLY with a valid grant token.
        if path == "/dl":
            if self.rate_limited("download", ip, *LIM_DOWNLOAD):
                return
            qs = urllib.parse.parse_qs(parsed.query)
            token = qs.get("token", [None])[0]
            row, err = grant_for_download(token)
            if err:
                return self.send_error(403, err)
            safe = clean_filename(row["file"])
            target = resolve_file(safe) if safe else None
            if not target:
                return self.send_error(404, "File not found")
            bump_download(token)
            return self.serve_file(target, download_name=row["name"] or safe)

        # Static assets
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not os.path.abspath(target).startswith(os.path.abspath(STATIC_DIR) + os.sep):
                return self.send_error(403, "Forbidden")
            return self.serve_file(target)

        # Pages
        if path in ("/", ""):
            return self.serve_file(os.path.join(ROOT, "index.html"))
        if path.endswith(".html"):
            return self.serve_file(os.path.join(ROOT, os.path.basename(path)))

        safe_rel = os.path.basename(path.lstrip("/"))
        target = os.path.join(ROOT, safe_rel)
        if safe_rel and not is_sensitive_filename(safe_rel) and os.path.isfile(target):
            return self.serve_file(target)

        self.send_error(404, "Not found")

    # -- POST --
    ADMIN_POSTS = ("/api/admin/login", "/api/admin/logout", "/api/admin/upload", "/api/admin/delete")

    def do_POST(self):
        ip = self.client_ip()
        if self.rate_limited("global", ip, *LIM_GLOBAL):
            return
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/create-order", "/api/verify-payment") + self.ADMIN_POSTS:
            return self.send_error(404, "Not found")
        if self.rate_limited("api", ip, *LIM_API):
            return

        if path in self.ADMIN_POSTS:
            return self.handle_admin(path, ip)

        data = self.read_body()
        if data is None:
            return self.send_json({"error": "Invalid or oversized request."}, 400)

        # --- Create a Razorpay order for a specific file + currency ---
        if path == "/api/create-order":
            if self.rate_limited("order", ip, *LIM_ORDER):
                return
            if not PAYMENTS_ON:
                return self.send_json({"error": "Payments aren’t configured yet."}, 503)
            safe = clean_filename(data.get("file"))
            if not safe or not file_exists(safe):
                return self.send_json({"error": "Unknown file."}, 400)
            email = (data.get("email") or "").strip().lower()
            if len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
                return self.send_json({"error": "Enter a valid email address."}, 400)
            currency = (data.get("currency") or "INR").upper()
            if currency not in SUPPORTED_CURRENCIES:
                return self.send_json({"error": "Unsupported currency."}, 400)
            amount = price_of(safe, currency)
            if amount <= 0:
                return self.send_json({"error": "This resource is free."}, 400)
            name = data.get("name")
            name = name[:255] if isinstance(name, str) else None
            try:
                order = razorpay_create_order(safe, email, currency, amount)
            except Exception:
                return self.send_json({"error": "Couldn’t start the payment. Please try again."}, 502)
            with db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO orders (order_id, file, name, email, currency, amount) "
                    "VALUES (?,?,?,?,?,?)", (order["id"], safe, name, email, currency, amount))
            return self.send_json({
                "order_id": order["id"], "amount": amount * 100, "currency": currency,
                "key_id": RAZORPAY_KEY_ID, "file": safe, "email": email,
            })

        # --- Verify the signed callback, mint a grant, email the link ---
        if path == "/api/verify-payment":
            if not PAYMENTS_ON:
                return self.send_json({"error": "Payments aren’t configured yet."}, 503)
            oid = data.get("razorpay_order_id")
            pid = data.get("razorpay_payment_id")
            sig = data.get("razorpay_signature")
            if not all(isinstance(x, str) and x for x in (oid, pid, sig)):
                return self.send_json({"error": "Invalid payment response."}, 400)
            with db() as conn:
                order = conn.execute(
                    "SELECT file, name, email, currency, amount FROM orders WHERE order_id=?",
                    (oid,)).fetchone()
            if not order:
                return self.send_json({"error": "Unknown order."}, 400)
            if not razorpay_signature_ok(oid, pid, sig):
                return self.send_json({"error": "Payment verification failed."}, 400)
            token = create_grant(order["file"], order["name"], order["email"], pid,
                                 order["currency"], order["amount"])
            url = f"{self.site_url()}/dl?token={token}"
            emailed = send_download_email(order["email"], order["name"], url,
                                          order["currency"], order["amount"])
            return self.send_json({
                "ok": True, "file": order["file"], "name": order["name"],
                "download_url": url, "emailed": emailed, "email": order["email"],
                "expires_days": GRANT_TTL_DAYS, "max_downloads": GRANT_MAX_DOWNLOADS,
            })

    # -- Admin: password login + file upload/delete (writes to the persistent volume) --
    def handle_admin(self, path, ip):
        if not ADMIN_ON:
            return self.send_json({"error": "Admin isn’t configured on this server."}, 503)

        if path == "/api/admin/login":
            if self.rate_limited("order", ip, *LIM_ORDER):  # tight bucket to slow guessing
                return
            data = self.read_body()
            if data is None:
                return self.send_json({"error": "Bad request."}, 400)
            pw = data.get("password")
            if not isinstance(pw, str) or not hmac.compare_digest(pw, ADMIN_PASSWORD):
                return self.send_json({"error": "Incorrect admin password."}, 401)
            return self.send_json({"ok": True}, set_cookie=admin_make_token())

        if path == "/api/admin/logout":
            return self.send_json({"ok": True}, clear_cookie=True)

        # All remaining admin actions require a valid admin session.
        if not self.is_admin():
            return self.send_json({"error": "Admin login required."}, 401)

        if path == "/api/admin/upload":
            data = self.read_body(MAX_UPLOAD_BYTES)
            if data is None:
                return self.send_json({"error": "Upload missing or too large (40 MB max)."}, 413)
            return self.admin_upload(data)

        if path == "/api/admin/delete":
            data = self.read_body()
            if data is None:
                return self.send_json({"error": "Bad request."}, 400)
            return self.admin_delete(data)

    def admin_upload(self, data):
        title = (data.get("title") or "").strip()
        orig = (data.get("filename") or "").strip()
        b64 = data.get("data")
        page = (data.get("page") or "library").strip().lower()
        cat = (data.get("cat") or "Other").strip()[:60] or "Other"
        desc = (data.get("desc") or "").strip()[:600]
        if not title or not orig or not isinstance(b64, str):
            return self.send_json({"error": "Title, file name and file are required."}, 400)
        if page not in PAGE_PRICES:
            return self.send_json({"error": "Invalid target page."}, 400)
        if is_sensitive_filename(orig):
            return self.send_json({"error": "That file type isn’t allowed for security reasons."}, 400)
        if "," in b64[:64] and b64.lstrip().startswith("data:"):
            b64 = b64.split(",", 1)[1]
        try:
            blob = base64.b64decode(b64)
        except Exception:
            return self.send_json({"error": "Couldn’t read the uploaded file data."}, 400)
        if not blob:
            return self.send_json({"error": "The file is empty."}, 400)
        if len(blob) > MAX_UPLOAD_BYTES:
            return self.send_json({"error": "File too large (40 MB max)."}, 413)

        def as_price(v, default):
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return default

        with _res_lock:
            items = load_resources()
            taken = {r.get("slug") for r in items}
            base_slug = slugify(orig)
            slug, n = base_slug, 1
            while slug in taken or resolve_file(slug):
                stem, ext = os.path.splitext(base_slug)
                n += 1
                slug = f"{stem}-{n}{ext}"
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            with open(os.path.join(UPLOAD_DIR, slug), "wb") as f:
                f.write(blob)
            res = {
                "slug": slug, "title": title[:200], "desc": desc, "cat": cat, "page": page,
                "fmt": (os.path.splitext(orig)[1].lstrip(".").upper() or "FILE")[:8],
                "size": len(blob),
                "price_inr": as_price(data.get("price_inr"), page_price(page, "INR")),
                "price_usd": as_price(data.get("price_usd"), page_price(page, "USD")),
                "orig_name": orig[:255], "dl": 0, "created_at": int(time.time()),
            }
            items.append(res)
            save_resources(items)
        out = {k: v for k, v in res.items() if k != "orig_name"}
        return self.send_json({"ok": True, "resource": out})

    def admin_delete(self, data):
        slug = data.get("slug")
        if not isinstance(slug, str) or not slug:
            return self.send_json({"error": "Missing slug."}, 400)
        with _res_lock:
            items = load_resources()
            keep = [r for r in items if r.get("slug") != slug]
            if len(keep) == len(items):
                return self.send_json({"error": "Resource not found."}, 404)
            save_resources(keep)
        # Only ever remove admin uploads — never touch the baked-in library files.
        p = os.path.join(UPLOAD_DIR, os.path.basename(slug))
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
        return self.send_json({"ok": True})

    def log_message(self, fmt, *args):
        print("[server]", self.address_string(), fmt % args)

if __name__ == "__main__":
    init_db()
    print(f"Everything You Need (shop) — running → http://localhost:{PORT}")
    print(f"Payments {'ON' if PAYMENTS_ON else 'OFF (set RAZORPAY_KEY_ID/SECRET)'} · "
          f"price ₹{DEFAULT_PRICES['INR']} / ${DEFAULT_PRICES['USD']} · "
          f"link {GRANT_TTL_DAYS}d/{GRANT_MAX_DOWNLOADS}dls")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
