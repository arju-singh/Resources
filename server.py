#!/usr/bin/env python3
"""
"Everything You Need" site backend — pure Python stdlib (no dependencies).

Features:
  - Serves static HTML/CSS/JS pages
  - Real signup/login with PBKDF2-hashed passwords stored in SQLite
  - Session cookies (HttpOnly, SameSite=Lax, optional Secure)
  - File downloads in /library are GATED behind authentication

Security hardening (OWASP-aligned):
  - Rate limiting on all public endpoints (per-IP + per-account), graceful 429s
  - Strict, schema-based input validation & sanitization (types, lengths, no extra fields)
  - No secrets in source: all config comes from environment variables
  - Security response headers, constant-time auth, session expiry, body-size caps

Run:  python3 server.py   (listens on http://localhost:3011)

---------------------------------------------------------------------------
SECURE API-KEY / SECRET HANDLING
---------------------------------------------------------------------------
This service holds NO third-party API keys and ships none to the browser.
Should you ever need one (e.g. an email provider), read it from the
environment — never hard-code it and never inline it into HTML/JS:

    SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]   # server-side only

Operational guidance:
  - Keep real secrets in a local `.env` / your process manager, NOT in git.
  - Rotate any key that has ever been committed or shared.
  - Client-side code must only ever see PUBLIC, rate-limited values.
"""
import os, re, json, time, hmac, sqlite3, hashlib, secrets, threading, mimetypes, urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie

ROOT = os.path.dirname(os.path.abspath(__file__))
# DB_PATH is overridable so production can point it at a persistent volume
# (Cloud Run's own filesystem is ephemeral — a mounted bucket keeps accounts).
DB_PATH = os.environ.get("DB_PATH", os.path.join(ROOT, "users.db"))
FILES_DIR = os.environ.get("FILES_DIR", os.path.join(ROOT, "files"))
LIBRARY_DIR = os.environ.get("LIBRARY_DIR", os.path.join(ROOT, "library"))
STATIC_DIR = os.path.join(ROOT, "static")

# ---------- Configuration (all via environment — no secrets in source) ----------
PORT = int(os.environ.get("PORT", 3011))
# Set SECURE_COOKIES=1 in production (HTTPS) so the session cookie carries `Secure`.
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "").lower() in ("1", "true", "yes")
# Trust X-Forwarded-For only when explicitly behind a known proxy.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", 30))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 64 * 1024))  # cap request bodies (DoS guard)

# Input-validation bounds
MAX_EMAIL_LEN = 254          # RFC 5321 practical maximum
MIN_PW_LEN, MAX_PW_LEN = 6, 128
PBKDF2_ITERS = 120_000

# ---------- Payments (Razorpay) ----------
# Keys come ONLY from the environment — never hard-code a secret. The publishable
# key id is safe to expose to the browser; the secret must stay server-side.
#   export RAZORPAY_KEY_ID=rzp_test_xxx      (use TEST keys for local dev)
#   export RAZORPAY_KEY_SECRET=xxxxxxxx
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
CURRENCY = os.environ.get("CURRENCY", "INR")
PAYMENTS_ON = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

# Server-authoritative price map (filename -> rupees; 0 = free). The client copy
# in library-data.js is for display only — order amounts and the download gate
# always use THIS map so a tampered client can't set its own price.
PRICING_PATH = os.path.join(ROOT, "pricing.json")
def load_prices():
    try:
        with open(PRICING_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {str(k): int(v) for k, v in raw.items() if isinstance(v, (int, float))}
    except Exception:
        return {}
PRICES = load_prices()

def price_of(fname):
    """Rupees for a library file. Unknown files default to free (nothing else is
    served through /libdl, so this can't accidentally expose a paid asset)."""
    return PRICES.get(fname, 0)

# ---------- Database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        -- Persistent brute-force state so account/IP lockouts survive restarts
        -- and are shared across worker processes. key = "acct:<email>" | "ip:<addr>".
        CREATE TABLE IF NOT EXISTS auth_throttle (
            key TEXT PRIMARY KEY,
            fails TEXT NOT NULL DEFAULT '[]',   -- JSON array of recent failure epochs
            locked_until REAL NOT NULL DEFAULT 0,
            strikes INTEGER NOT NULL DEFAULT 0
        );
        -- A Razorpay order we created, bound to the buyer + the exact file/amount,
        -- so verification grants only what was actually paid for (no price tampering).
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            file TEXT NOT NULL,
            amount INTEGER NOT NULL,             -- rupees
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        -- A completed purchase unlocks a file for a user, forever.
        CREATE TABLE IF NOT EXISTS purchases (
            user_id INTEGER NOT NULL,
            file TEXT NOT NULL,
            payment_id TEXT,
            amount INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, file)
        );
        """)
        # Drop clearly-stale rows (expired lock and no recent failures) on startup.
        conn.execute("DELETE FROM auth_throttle WHERE locked_until < ? AND fails = '[]'",
                     (time.time() - 86400,))

def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERS).hex()

# A fixed dummy hash lets login do equal work whether or not the email exists,
# mitigating user-enumeration via timing (OWASP: identical responses & timing).
_DUMMY_SALT = secrets.token_hex(16)
_DUMMY_HASH = hash_pw("invalid-placeholder", _DUMMY_SALT)

# ---------- Auth helpers ----------
def create_user(email, password):
    salt = secrets.token_hex(16)
    pw = hash_pw(password, salt)
    with db() as conn:
        conn.execute("INSERT INTO users (email, pw_hash, salt) VALUES (?,?,?)",
                     (email, pw, salt))
        return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]

def verify_user(email, password):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        # Constant-time dummy compare so missing accounts take ~the same time.
        hmac.compare_digest(hash_pw(password, _DUMMY_SALT), _DUMMY_HASH)
        return None
    # Constant-time comparison defeats hash-timing side channels.
    if hmac.compare_digest(hash_pw(password, row["salt"]), row["pw_hash"]):
        return row["id"]
    return None

def new_session(user_id):
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?,?)", (token, user_id))
    return token

def user_from_token(token):
    if not token or not isinstance(token, str) or len(token) > 128:
        return None
    with db() as conn:
        # Server-side session expiry — a stolen/old cookie stops working after the TTL.
        row = conn.execute(
            "SELECT u.id, u.email FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token=? AND s.created_at > datetime('now', ?)",
            (token, f"-{SESSION_TTL_DAYS} days")).fetchone()
    return dict(row) if row else None

def drop_session(token):
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))

# ---------- Purchases / payments ----------
def has_purchased(user_id, fname):
    with db() as conn:
        row = conn.execute("SELECT 1 FROM purchases WHERE user_id=? AND file=?",
                           (user_id, fname)).fetchone()
    return row is not None

def user_purchases(user_id):
    with db() as conn:
        rows = conn.execute("SELECT file FROM purchases WHERE user_id=?", (user_id,)).fetchall()
    return [r["file"] for r in rows]

def razorpay_create_order(user_id, fname, rupees):
    """Create a Razorpay order server-side and remember what it's for. Returns the
    order dict or raises. Amount is sent in the smallest unit (paise)."""
    import base64, urllib.request
    body = json.dumps({
        "amount": rupees * 100,
        "currency": CURRENCY,
        "receipt": f"eyn-{user_id}-{secrets.token_hex(6)}",
        "notes": {"user_id": str(user_id), "file": fname[:200]},
    }).encode()
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        order = json.loads(r.read())
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO orders (order_id, user_id, file, amount) VALUES (?,?,?,?)",
                     (order["id"], user_id, fname, rupees))
    return order

def razorpay_signature_ok(order_id, payment_id, signature):
    """Verify the Razorpay callback signature = HMAC_SHA256(order_id|payment_id, secret)."""
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(),
                        f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")

def record_purchase(user_id, fname, payment_id, rupees):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO purchases (user_id, file, payment_id, amount) VALUES (?,?,?,?)",
            (user_id, fname, payment_id, rupees))

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Files that must NEVER be served, even if accidentally placed/symlinked into a
# served directory. Matched case-insensitively against the basename. Defense in
# depth for secret material (keys, certs, env files, credential dumps).
SENSITIVE_FILE_RE = re.compile(
    r"(\.(env|pem|key|p12|pfx|keystore|jks|ppk|mobileprovision|asc|gpg)$)"
    r"|(^\.env)"
    r"|(id_rsa)"
    r"|(google-?services?[-.])"           # google-services.json / GoogleService-Info.plist
    r"|(\.plist$)"
    r"|(backup[\s_-]*codes)"
    r"|(secret|credential|password)s?\.",  # secrets.json, credentials.txt, ...
    re.IGNORECASE,
)

def is_sensitive_filename(name):
    return bool(SENSITIVE_FILE_RE.search(os.path.basename(name or "")))

# ---------- Input validation / sanitization ----------
def clean_credentials(data, require_strength):
    """Schema-validate an auth payload. Returns ((email, password), None) or (None, error).

    Enforces: object body, only {email,password} keys, string types, length caps,
    email format, and (on signup) password strength.
    """
    if not isinstance(data, dict):
        return None, "Invalid request body."
    extra = set(data.keys()) - {"email", "password"}
    if extra:
        return None, "Unexpected fields in request."
    email, pw = data.get("email"), data.get("password")
    if not isinstance(email, str) or not isinstance(pw, str):
        return None, "Email and password must be text."
    email = email.strip().lower()
    if len(email) > MAX_EMAIL_LEN or not EMAIL_RE.match(email):
        return None, "Enter a valid email address."
    if require_strength:
        if not (MIN_PW_LEN <= len(pw) <= MAX_PW_LEN):
            return None, f"Password must be {MIN_PW_LEN}–{MAX_PW_LEN} characters."
    else:
        # On login, don't leak the policy — just bound length to keep PBKDF2 cheap.
        if not (1 <= len(pw) <= MAX_PW_LEN):
            return None, "Invalid email or password."
    return (email, pw), None

# ---------- Rate limiting (sliding window, thread-safe) ----------
class RateLimiter:
    """In-memory sliding-window limiter keyed by (bucket, key). Thread-safe.

    hit() returns 0 if allowed, or seconds-to-retry (>0) if the limit is hit.
    """
    def __init__(self):
        self._b = defaultdict(lambda: defaultdict(deque))
        self._lock = threading.Lock()

    def hit(self, bucket, key, limit, window):
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            dq = self._b[bucket][key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return int(dq[0] + window - now) + 1
            dq.append(now)
            # Opportunistic cleanup so idle keys don't accumulate forever.
            if len(self._b[bucket]) > 20_000:
                for k in [k for k, d in self._b[bucket].items() if not d]:
                    del self._b[bucket][k]
            return 0

RL = RateLimiter()
# (limit, window_seconds) per bucket — generous for normal use, strict for abuse-prone routes.
LIM_GLOBAL   = (1200, 60)   # per-IP backstop across ALL requests (incl. static assets)
LIM_API      = (240, 60)    # /api/* (the library page polls these)
LIM_DOWNLOAD = (120, 60)    # gated file downloads per IP
LIM_AUTH_IP  = (30, 900)    # login+signup attempts per IP / 15 min — flood backstop only;
                            # precise brute-force defense is the failure-based guards below.


class AuthThrottle:
    """Failure-based lockout with exponential backoff (OWASP-recommended), persisted
    in SQLite so lockouts survive restarts and are shared across worker processes.

    Counts FAILED logins per key inside a rolling window; once a threshold is hit the
    key is locked, and each repeat lockout doubles the lock duration (capped). A
    successful login clears the key. Uses wall-clock time (epoch) so it persists.
    Complements the volume rate limiter: the limiter caps request rate; this
    specifically punishes credential guessing.
    """
    def __init__(self):
        # Serialize read-modify-write across threads (SQLite also locks, but this
        # keeps the failure-count update atomic without relying on a single txn).
        self._lock = threading.Lock()

    def locked_for(self, key):
        """Seconds remaining on an active lock, or 0 if not locked."""
        now = time.time()
        with db() as conn:
            row = conn.execute("SELECT locked_until FROM auth_throttle WHERE key=?", (key,)).fetchone()
        if row and row["locked_until"] and row["locked_until"] > now:
            return int(row["locked_until"] - now) + 1
        return 0

    def record_failure(self, key, threshold, base_lock, max_lock, window):
        """Register a failed attempt; returns the lock duration (s) if it just locked, else 0."""
        now = time.time()
        with self._lock, db() as conn:
            row = conn.execute(
                "SELECT fails, locked_until, strikes FROM auth_throttle WHERE key=?", (key,)).fetchone()
            try:
                fails = json.loads(row["fails"]) if row else []
            except Exception:
                fails = []
            strikes = row["strikes"] if row else 0
            locked_until = row["locked_until"] if row else 0.0
            fails = [t for t in fails if t > now - window]
            fails.append(now)
            lock = 0
            if len(fails) >= threshold:
                strikes += 1
                lock = min(base_lock * (2 ** (strikes - 1)), max_lock)
                locked_until = now + lock
                fails = []
            conn.execute(
                "INSERT INTO auth_throttle (key, fails, locked_until, strikes) VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, "
                "locked_until=excluded.locked_until, strikes=excluded.strikes",
                (key, json.dumps(fails), locked_until, strikes))
            return int(lock)

    def reset(self, key):
        with self._lock, db() as conn:
            conn.execute("DELETE FROM auth_throttle WHERE key=?", (key,))

THROTTLE = AuthThrottle()
# (threshold, base_lock, max_lock, window) — applied per key prefix.
# Lock an ACCOUNT after 5 bad logins / 15 min: 1m → 2m → 4m … capped at 1h.
ACCT_CFG = (5, 60, 3600, 900)
# Lock a source IP after 20 bad logins / 15 min (higher, since IPs can be shared): up to 30m.
IP_CFG   = (20, 60, 1800, 900)

# ---------- HTTP handler ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "web"     # reduce fingerprinting
    sys_version = ""

    # Security headers applied to EVERY response (incl. errors) via end_headers().
    SECURITY_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        # Allows the app's inline scripts/styles + the jsDelivr CDN (three.js/motion),
        # blocks everything else. 'unsafe-inline' is required by the existing inline
        # modules/handlers; tighten to nonces/hashes if those are refactored.
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

    # -- utilities --
    def client_ip(self):
        # Only trust the proxy header when explicitly configured (avoids IP spoofing
        # of the rate limiter when not behind a trusted proxy).
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def cookie_secure(self):
        """Return '; Secure' when the session cookie should be HTTPS-only.

        Forced on by SECURE_COOKIES; otherwise auto-enabled for any non-localhost
        host so real deployments are secure by default, while local http://localhost
        development keeps working (a Secure cookie isn't sent over plain HTTP)."""
        if SECURE_COOKIES:
            return "; Secure"
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        local = host in ("localhost", "127.0.0.1", "::1", "[::1]", "") or host.endswith(".local")
        return "" if local else "; Secure"

    def rate_limited(self, bucket, key, limit, window):
        """Returns True (and emits a 429) if the caller is over the limit."""
        retry = RL.hit(bucket, key, limit, window)
        if retry:
            self.send_json({"error": "Too many requests. Please slow down."},
                           429, retry_after=retry)
            return True
        return False

    def cookies(self):
        c = SimpleCookie()
        if "Cookie" in self.headers:
            try:
                c.load(self.headers["Cookie"])
            except Exception:
                return SimpleCookie()
        return c

    def session_token(self):
        c = self.cookies()
        return c["session"].value if "session" in c else None

    def current_user(self):
        return user_from_token(self.session_token())

    def send_json(self, obj, status=200, set_cookie=None, clear_cookie=False, retry_after=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        secure = self.cookie_secure()
        if set_cookie:
            self.send_header("Set-Cookie",
                f"session={set_cookie}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={SESSION_TTL_DAYS * 86400}{secure}")
        if clear_cookie:
            self.send_header("Set-Cookie", f"session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        """Read & parse a request body, enforcing the size cap. Returns a dict, or
        None if oversized/malformed (caller decides the response)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
        return parsed if isinstance(parsed, dict) else None

    def serve_file(self, path, download_name=None):
        if not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        ctype, _ = mimetypes.guess_type(path)
        ctype = ctype or "application/octet-stream"
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if download_name:
            # ASCII-safe fallback + RFC 5987 UTF-8 name so unicode/emoji filenames don't crash the header
            ascii_name = download_name.encode("ascii", "ignore").decode() or "download"
            quoted = urllib.parse.quote(download_name)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}")
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                self.wfile.write(chunk)

    # -- routing --
    def do_GET(self):
        ip = self.client_ip()
        # Per-IP DoS backstop on every request, including static assets.
        if self.rate_limited("global", ip, *LIM_GLOBAL):
            return

        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        # JSON APIs get a tighter per-IP budget.
        if path.startswith("/api/"):
            if self.rate_limited("api", ip, *LIM_API):
                return

        if path == "/api/me":
            user = self.current_user()
            if user:
                return self.send_json(user)
            return self.send_json({"error": "not authenticated"}, 401)

        # Public payment config (publishable key id only — never the secret).
        if path == "/api/pay-config":
            return self.send_json({"configured": PAYMENTS_ON,
                                   "key_id": RAZORPAY_KEY_ID if PAYMENTS_ON else "",
                                   "currency": CURRENCY})

        # Which files the signed-in user has unlocked (bought or free), so the
        # library can show the right button. Prices come from the server, not the client.
        if path == "/api/my-purchases":
            user = self.current_user()
            if not user:
                return self.send_json({"error": "not authenticated"}, 401)
            return self.send_json({"purchased": user_purchases(user["id"])})

        # Which library files currently exist (skips deleted/dangling) — lets the
        # client hide resources whose underlying file was physically removed.
        if path == "/api/library-files":
            present = []
            if os.path.isdir(LIBRARY_DIR):
                for name in os.listdir(LIBRARY_DIR):
                    p = os.path.join(LIBRARY_DIR, name)
                    if os.path.exists(p):  # follows symlinks; False if target gone
                        present.append(name)
            return self.send_json({"files": present})

        # Gated downloads (resources in /files and curated library in /library)
        if path.startswith("/download") or path.startswith("/libdl"):
            if self.rate_limited("download", ip, *LIM_DOWNLOAD):
                return
            user = self.current_user()
            if not user:
                return self.send_json({"error": "Sign in required to download."}, 401)
            base_dir = LIBRARY_DIR if path.startswith("/libdl") else FILES_DIR
            qs = urllib.parse.parse_qs(parsed.query)
            fname = qs.get("file", [None])[0]
            # Validate the filename: present, sane length, no path separators.
            if not fname or len(fname) > 255 or "/" in fname or "\\" in fname or "\x00" in fname:
                return self.send_error(400, "Bad request")
            # Defense in depth against path traversal: basename + prefix check.
            safe = os.path.basename(fname)
            # Never serve secret material even if it was symlinked into the library.
            if is_sensitive_filename(safe):
                return self.send_error(403, "Forbidden")
            target = os.path.join(base_dir, safe)
            if os.path.abspath(target) != os.path.abspath(os.path.join(base_dir, safe)) \
               or not os.path.abspath(target).startswith(os.path.abspath(base_dir) + os.sep):
                return self.send_error(403, "Forbidden")
            # Paywall: library files with a price require a completed purchase.
            # Free files (price 0) download for any signed-in user. The gate is
            # here on the server, so it can't be bypassed from the client.
            if path.startswith("/libdl"):
                price = price_of(safe)
                if price > 0 and not has_purchased(user["id"], safe):
                    return self.send_json(
                        {"error": "Purchase required to download this resource.",
                         "price": price, "file": safe}, 402)
            return self.serve_file(target, download_name=safe)

        # Static: /static/* (supports nested dirs, e.g. /static/covers/x.png)
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not os.path.abspath(target).startswith(os.path.abspath(STATIC_DIR) + os.sep):
                return self.send_error(403, "Forbidden")
            return self.serve_file(target)

        # Pages
        if path == "/" or path == "":
            return self.serve_file(os.path.join(ROOT, "index.html"))
        # Any .html in root (basename only — no traversal)
        if path.endswith(".html"):
            target = os.path.join(ROOT, os.path.basename(path))
            return self.serve_file(target)

        # Allow direct asset access for favicon etc. (root-level files only)
        safe_rel = os.path.basename(path.lstrip("/"))
        target = os.path.join(ROOT, safe_rel)
        if safe_rel and not is_sensitive_filename(safe_rel) and os.path.isfile(target):
            return self.serve_file(target)

        self.send_error(404, "Not found")

    def do_POST(self):
        ip = self.client_ip()
        if self.rate_limited("global", ip, *LIM_GLOBAL):
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path not in ("/api/signup", "/api/login", "/api/logout",
                        "/api/create-order", "/api/verify-payment"):
            return self.send_error(404, "Not found")

        # All POST APIs share the tighter API budget; auth routes add brute-force limits.
        if self.rate_limited("api", ip, *LIM_API):
            return

        data = self.read_body()
        if data is None:
            return self.send_json({"error": "Invalid or oversized request."}, 400)

        if path == "/api/signup":
            if self.rate_limited("auth", ip, *LIM_AUTH_IP):
                return
            creds, err = clean_credentials(data, require_strength=True)
            if err:
                return self.send_json({"error": err}, 400)
            email, pw = creds
            try:
                uid = create_user(email, pw)
            except sqlite3.IntegrityError:
                return self.send_json({"error": "An account with that email already exists."}, 409)
            token = new_session(uid)
            return self.send_json({"email": email}, 201, set_cookie=token)

        if path == "/api/login":
            if self.rate_limited("auth", ip, *LIM_AUTH_IP):
                return
            creds, err = clean_credentials(data, require_strength=False)
            if err:
                return self.send_json({"error": "Invalid email or password."}, 401)
            email, pw = creds
            acct_key, ip_key = "acct:" + email, "ip:" + ip
            # Brute-force lockout: refuse (generically) while the account or IP is locked.
            locked = max(THROTTLE.locked_for(acct_key), THROTTLE.locked_for(ip_key))
            if locked:
                return self.send_json(
                    {"error": "Too many failed attempts. Please try again later."},
                    429, retry_after=locked)
            uid = verify_user(email, pw)
            if not uid:
                # Count the failure against both the account and the IP; if this trips
                # the threshold, respond with the lockout (still generic to avoid enumeration).
                just_locked = max(THROTTLE.record_failure(acct_key, *ACCT_CFG),
                                  THROTTLE.record_failure(ip_key, *IP_CFG))
                if just_locked:
                    return self.send_json(
                        {"error": "Too many failed attempts. Please try again later."},
                        429, retry_after=just_locked)
                return self.send_json({"error": "Invalid email or password."}, 401)
            # Success clears the counters so legitimate users aren't punished later.
            THROTTLE.reset(acct_key)
            THROTTLE.reset(ip_key)
            token = new_session(uid)
            return self.send_json({"email": email}, 200, set_cookie=token)

        if path == "/api/logout":
            drop_session(self.session_token())
            return self.send_json({"ok": True}, 200, clear_cookie=True)

        # --- Payments: create a Razorpay order for a specific file ---
        if path == "/api/create-order":
            user = self.current_user()
            if not user:
                return self.send_json({"error": "Sign in to purchase."}, 401)
            if not PAYMENTS_ON:
                return self.send_json({"error": "Payments aren’t configured yet."}, 503)
            fname = data.get("file")
            if not isinstance(fname, str) or not fname or len(fname) > 255 \
               or "/" in fname or "\\" in fname or "\x00" in fname:
                return self.send_json({"error": "Invalid file."}, 400)
            fname = os.path.basename(fname)
            price = price_of(fname)
            if price <= 0:
                return self.send_json({"error": "This resource is free — no payment needed."}, 400)
            if has_purchased(user["id"], fname):
                return self.send_json({"already_purchased": True})
            try:
                order = razorpay_create_order(user["id"], fname, price)
            except Exception:
                return self.send_json({"error": "Couldn’t start the payment. Please try again."}, 502)
            return self.send_json({"order_id": order["id"], "amount": price * 100,
                                   "currency": CURRENCY, "key_id": RAZORPAY_KEY_ID,
                                   "file": fname})

        # --- Payments: verify the signed callback, then unlock the file ---
        if path == "/api/verify-payment":
            user = self.current_user()
            if not user:
                return self.send_json({"error": "Sign in required."}, 401)
            if not PAYMENTS_ON:
                return self.send_json({"error": "Payments aren’t configured yet."}, 503)
            oid = data.get("razorpay_order_id")
            pid = data.get("razorpay_payment_id")
            sig = data.get("razorpay_signature")
            if not all(isinstance(x, str) and x for x in (oid, pid, sig)):
                return self.send_json({"error": "Invalid payment response."}, 400)
            # The order must be one WE created for THIS user — grant only that file.
            with db() as conn:
                row = conn.execute(
                    "SELECT file, amount FROM orders WHERE order_id=? AND user_id=?",
                    (oid, user["id"])).fetchone()
            if not row:
                return self.send_json({"error": "Unknown order."}, 400)
            if not razorpay_signature_ok(oid, pid, sig):
                return self.send_json({"error": "Payment verification failed."}, 400)
            record_purchase(user["id"], row["file"], pid, row["amount"])
            return self.send_json({"ok": True, "file": row["file"]})

    def log_message(self, fmt, *args):
        print("[server]", self.address_string(), fmt % args)

if __name__ == "__main__":
    init_db()
    print(f"Everything You Need — running → http://localhost:{PORT}")
    print("Hardened: rate limiting, input validation, security headers. Ctrl+C to stop.")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
