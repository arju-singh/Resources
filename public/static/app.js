// Shared nav + lightweight helpers for every page.
//
// The site is now fully static — no accounts, no backend, no cookies. Everything
// in the Library is free to download directly, so there is nothing to log in to
// and no `/api/*` endpoint to call. This helper keeps the shape other pages
// expect (some still reference AUTH.*) while making zero network requests.

const AUTH = {
  user: null,
  backendUp: false,
  // Kept so callers that `await AUTH.fetchMe()` don't break — always resolves null.
  async fetchMe() { return null; },
  async logout() { location.href = '/'; }
};

// The nav reserves a `#nav-auth` slot on every page. With no accounts there is
// nothing to show there, so we simply ensure it's empty (no fetch, no flicker).
function renderNavAuth() {
  const slot = document.getElementById('nav-auth');
  if (slot) slot.innerHTML = '';
}

document.addEventListener('DOMContentLoaded', renderNavAuth);
