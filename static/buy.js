// Shared purchase engine — used by any page that sells files (library, checklist…).
// Region-based currency, server-authoritative pricing, Razorpay checkout, and the
// emailed-link success state. Exposes a small global: window.EYN.
window.EYN = (function () {
  const PAY = { configured: false, key_id: '', prices: { INR: 99, USD: 4 },
                page_prices: {}, ttl_days: 30, max_downloads: 10 };

  // Region default: India → INR, everyone else → USD (server still sets the charge).
  const CUR = (function () {
    try {
      const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone || '');
      if (/Calcutta|Kolkata/i.test(tz)) return 'INR';
      if (/-IN$/i.test(navigator.language || '')) return 'INR';
    } catch (e) {}
    return 'USD';
  })();

  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  const money = (cur, amt) => cur === 'INR' ? ('₹' + amt) : ('$' + amt);

  async function loadConfig() {
    try {
      const r = await fetch('/api/pay-config');
      if (r.ok) Object.assign(PAY, await r.json());
    } catch (e) { /* backend unreachable — buy() explains on click */ }
    return PAY;
  }

  // Price for a resource: its own price if set, else the page default, else global.
  function priceFor(res) {
    const pp = PAY.page_prices[res.page] || PAY.prices;
    const inr = (res && res.price_inr != null) ? res.price_inr : pp.INR;
    const usd = (res && res.price_usd != null) ? res.price_usd : pp.USD;
    return {
      inr, usd, cur: CUR,
      label: money(CUR, CUR === 'INR' ? inr : usd),
      alt: CUR === 'INR' ? ('or ' + money('USD', usd) + ' international')
                         : ('or ' + money('INR', inr) + ' in India')
    };
  }

  function loadRazorpay() {
    return new Promise(function (resolve, reject) {
      if (window.Razorpay) return resolve();
      const s = document.createElement('script');
      s.src = 'https://checkout.razorpay.com/v1/checkout.js';
      s.onload = resolve; s.onerror = function () { reject(new Error('checkout')); };
      document.head.appendChild(s);
    });
  }

  // res: { slug, page, title, name? }.  hooks: { setMsg(text,kind), onSuccess(vd), done() }
  async function buy(res, email, hooks) {
    const setMsg = (hooks && hooks.setMsg) || function () {};
    const done = (hooks && hooks.done) || function () {};
    email = (email || '').trim().toLowerCase();
    if (!EMAIL_RE.test(email)) { setMsg('Enter a valid email for your download link.', 'err'); return; }
    if (!PAY.configured) { setMsg('Payments aren’t live yet — check back shortly.', 'err'); return; }
    setMsg('Starting secure checkout…');
    try {
      await loadRazorpay();
      const r = await fetch('/api/create-order', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: res.slug, name: res.name || res.title, email, currency: CUR })
      });
      const o = await r.json();
      if (!r.ok) { setMsg(o.error || 'Couldn’t start checkout.', 'err'); done(); return; }
      const rzp = new Razorpay({
        key: o.key_id, order_id: o.order_id, amount: o.amount, currency: o.currency,
        name: 'Everything You Need', description: res.title || res.slug, prefill: { email },
        theme: { color: '#a855f7' },
        handler: async function (resp) {
          setMsg('Verifying payment…');
          try {
            const v = await fetch('/api/verify-payment', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                razorpay_order_id: resp.razorpay_order_id,
                razorpay_payment_id: resp.razorpay_payment_id,
                razorpay_signature: resp.razorpay_signature
              })
            });
            const vd = await v.json();
            if (vd.ok && hooks && hooks.onSuccess) hooks.onSuccess(vd);
            else if (!vd.ok) { setMsg(vd.error || 'Verification failed. If charged, email connect@arjusingh.com.', 'err'); done(); }
          } catch (e) { setMsg('Verification error. If charged, email connect@arjusingh.com with your payment id.', 'err'); done(); }
        },
        modal: { ondismiss: function () { setMsg('Checkout closed — you were not charged.'); done(); } }
      });
      rzp.on('payment.failed', function (e) { setMsg((e.error && e.error.description) || 'Payment failed. Try again.', 'err'); done(); });
      rzp.open();
    } catch (e) {
      setMsg('Couldn’t load the payment window. Check your connection and retry.', 'err');
      done();
    }
  }

  // One-time style injection so any page can mount a buy box without editing CSS.
  function injectStyles() {
    if (document.getElementById('eyn-buy-css')) return;
    const s = document.createElement('style');
    s.id = 'eyn-buy-css';
    s.textContent = `
      .eyn-buy,.eyn-success{display:flex;flex-direction:column;gap:11px}
      .eyn-price{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
      .eyn-price b{font-size:24px;font-weight:900;letter-spacing:-.5px}
      .eyn-price span{color:#9a9aa5;font-size:13px}
      .eyn-email{background:#17171c;border:1px solid #2a2a30;border-radius:11px;padding:12px 14px;color:#f4f4f5;font-size:15px;font-family:inherit;width:100%}
      .eyn-email:focus{outline:none;border-color:#a855f7}
      .eyn-btn{cursor:pointer;border:none;font-family:inherit;font-weight:800;font-size:15px;color:#fff;text-decoration:none;text-align:center;
        background:linear-gradient(90deg,#a855f7,#ec4899);padding:14px 18px;border-radius:12px;display:block}
      .eyn-btn:disabled{opacity:.55;cursor:default}
      .eyn-note{color:#9a9aa5;font-size:12px;line-height:1.5}
      .eyn-msg{font-size:13px;font-weight:600;min-height:1em;color:#9a9aa5}
      .eyn-msg.err{color:#fca5a5}
      .eyn-check{font-size:18px;font-weight:900;color:#4ade80}
      .eyn-success p{color:#c8c8d0;font-size:14px;line-height:1.55}`;
    document.head.appendChild(s);
  }

  // Render a self-contained price + email + Buy widget into `container` for `res`.
  function mountBuyBox(container, res) {
    injectStyles();
    const p = priceFor(res);
    container.innerHTML =
      '<div class="eyn-buy">' +
        '<div class="eyn-price"><b>' + p.label + '</b> <span>' + p.alt + '</span></div>' +
        '<input class="eyn-email" type="email" inputmode="email" placeholder="Email — we’ll send your download link">' +
        '<button class="eyn-btn">🔒 Buy &amp; Download · ' + p.label + '</button>' +
        '<div class="eyn-note">Instant, secure link · re-download for ' + PAY.ttl_days + ' days.</div>' +
        '<div class="eyn-msg" role="status"></div>' +
      '</div>';
    const emailEl = container.querySelector('.eyn-email');
    const btn = container.querySelector('.eyn-btn');
    const msgEl = container.querySelector('.eyn-msg');
    const setMsg = (t, k) => { msgEl.textContent = t; msgEl.className = 'eyn-msg ' + (k || ''); };
    function go() {
      btn.disabled = true;
      buy(res, emailEl.value, {
        setMsg: setMsg, done: () => { btn.disabled = false; },
        onSuccess: (vd) => {
          container.innerHTML =
            '<div class="eyn-success"><div class="eyn-check">✅ Payment successful</div>' +
            '<p>Link sent to <b>' + (vd.email || '') + '</b>' + (vd.emailed ? '' : ' (use the button if it doesn’t arrive)') + '. Download now:</p>' +
            '<a class="eyn-btn" href="' + vd.download_url + '">⬇ Download</a>' +
            '<div class="eyn-note">Works ' + vd.expires_days + ' days / ' + vd.max_downloads + ' downloads — keep the email.</div></div>';
        }
      });
    }
    btn.addEventListener('click', go);
    emailEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  }

  return { PAY, CUR, money, loadConfig, priceFor, buy, mountBuyBox, EMAIL_RE };
})();

