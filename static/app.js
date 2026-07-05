// Shared auth + nav helper for all pages
const AUTH = {
  user: null,
  backendUp: false,
  async fetchMe() {
    this.backendUp = false;
    try {
      const r = await fetch('/api/me');
      // A JSON reply (even a 401) means the backend is present. On the static
      // host /api/me is a 404 HTML page, so we know there are no accounts.
      const ct = r.headers.get('content-type') || '';
      if (ct.indexOf('application/json') !== -1) {
        this.backendUp = true;
        this.user = r.ok ? await r.json() : null;
      } else { this.user = null; }
    } catch { this.user = null; }
    return this.user;
  },
  async logout() {
    await fetch('/api/logout', { method: 'POST' });
    this.user = null;
    location.href = '/';
  }
};

// Renders the right side of the nav based on auth state
async function renderNavAuth() {
  const slot = document.getElementById('nav-auth');
  if (!slot) return;
  await AUTH.fetchMe();
  if (AUTH.user) {
    slot.innerHTML = `
      <span class="email">${AUTH.user.email}</span>
      <button class="btn-ghost" id="logout-btn">Log Out</button>`;
    document.getElementById('logout-btn').addEventListener('click', () => AUTH.logout());
  } else if (AUTH.backendUp) {
    slot.innerHTML = `
      <a class="btn-ghost" href="/login.html">Log In</a>
      <a class="btn-grad" href="/login.html?mode=signup">Sign Up</a>`;
  } else {
    // Static host with no backend — everything is free, no accounts needed.
    slot.innerHTML = '';
  }
}

document.addEventListener('DOMContentLoaded', renderNavAuth);

// --- Cookie consent banner (GDPR) --------------------------------------------
// Strictly-necessary session cookies always apply; this records the user's
// choice on non-essential cookies (e.g. analytics) in localStorage.
function initCookieConsent() {
  var KEY = 'cookie-consent-v1';
  var choice;
  try { choice = localStorage.getItem(KEY); } catch (e) { choice = 'dismissed'; }
  if (choice === 'accepted' || choice === 'rejected') {
    window.cookieConsent = choice;
    return;
  }

  var bar = document.createElement('div');
  bar.id = 'cookie-consent';
  bar.setAttribute('role', 'dialog');
  bar.setAttribute('aria-label', 'Cookie consent');
  bar.innerHTML =
    '<div class="cc-inner">' +
      '<p class="cc-text">We use a strictly necessary cookie to keep you signed in. ' +
        'With your consent we may also use non-essential cookies (e.g. analytics) to improve the site. ' +
        'See our <a href="/privacy.html">Privacy Policy</a>.</p>' +
      '<div class="cc-actions">' +
        '<button type="button" class="cc-btn cc-reject" id="cc-reject">Decline</button>' +
        '<button type="button" class="cc-btn cc-accept" id="cc-accept">Accept</button>' +
      '</div>' +
    '</div>';

  var css = document.createElement('style');
  css.textContent =
    '#cookie-consent{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:999;' +
      'width:min(680px,calc(100% - 32px));background:#141417;border:1px solid #2a2a30;border-radius:16px;' +
      'box-shadow:0 18px 50px rgba(0,0,0,.55);padding:16px 18px;animation:ccUp .35s ease both;}' +
    '@keyframes ccUp{from{opacity:0;transform:translate(-50%,12px)}to{opacity:1;transform:translate(-50%,0)}}' +
    '#cookie-consent .cc-inner{display:flex;align-items:center;gap:16px;flex-wrap:wrap;}' +
    '#cookie-consent .cc-text{margin:0;flex:1;min-width:240px;color:#a1a1aa;font-size:13px;line-height:1.6;}' +
    '#cookie-consent .cc-text a{color:#c084fc;text-decoration:none;}' +
    '#cookie-consent .cc-actions{display:flex;gap:10px;flex:0 0 auto;}' +
    '#cookie-consent .cc-btn{cursor:pointer;border-radius:100px;padding:9px 18px;font-size:13px;font-weight:700;border:1px solid #2a2a30;}' +
    '#cookie-consent .cc-reject{background:transparent;color:#f4f4f5;}' +
    '#cookie-consent .cc-reject:hover{border-color:#c084fc;}' +
    '#cookie-consent .cc-accept{background:linear-gradient(90deg,#c084fc,#f0abfc);color:#0a0a0b;border-color:transparent;}';
  document.head.appendChild(css);
  document.body.appendChild(bar);

  function decide(value) {
    try { localStorage.setItem(KEY, value); } catch (e) {}
    window.cookieConsent = value;
    bar.remove();
    document.dispatchEvent(new CustomEvent('cookie-consent', { detail: value }));
  }
  document.getElementById('cc-accept').addEventListener('click', function () { decide('accepted'); });
  document.getElementById('cc-reject').addEventListener('click', function () { decide('rejected'); });
}

document.addEventListener('DOMContentLoaded', initCookieConsent);
