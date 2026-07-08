// Reusable resources engine — lets ANY page (a) sell admin-uploaded files and
// (b) carry an admin-only "add resource" panel, so the admin can publish content
// straight from the page it belongs to. Depends on window.EYN (buy.js) for the
// public purchase box. Never touches secrets — all pricing/charging stays
// server-authoritative; this only chooses which ₹ preset to send at upload time.
window.EYNRes = (function () {
  // The only prices the admin may pick, per the product. Buyer's charge is still
  // whatever the server stored at upload; these just constrain what we send.
  const PRESETS_INR = [69, 79, 89, 99, 199, 299];
  // ₹ → $ map so the admin picks once (₹) and USD is derived. ₹99→$4 / ₹199→$10
  // match the existing library/checklist pricing; others scale in between.
  const INR_TO_USD = { 69: 3, 79: 4, 89: 4, 99: 4, 199: 10, 299: 15 };
  const usdForInr = (inr) => (INR_TO_USD[inr] != null ? INR_TO_USD[inr] : Math.max(1, Math.round(inr / 22)));

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const el = (html) => { const t = document.createElement('template'); t.innerHTML = html.trim(); return t.content.firstChild; };
  const readB64 = (file) => new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(',').pop());
    fr.onerror = rej; fr.readAsDataURL(file);
  });

  // One-time CSS for both the shop grid and the admin panel.
  function injectCss() {
    if (document.getElementById('eynres-css')) return;
    const s = document.createElement('style');
    s.id = 'eynres-css';
    s.textContent = `
      .eynres-head{margin-top:8px}
      .eynres-h2{font-size:24px;font-weight:900;letter-spacing:-.5px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
      .eynres-count{font-size:13px;font-weight:800;color:#c084fc}
      .eynres-sub{color:var(--muted,#9a9aa5);font-size:15px;margin:6px 0 2px}
      .eynres-grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));margin-top:18px}
      .eynres-card{text-align:left;cursor:pointer;border:1px solid var(--border,#2a2a30);border-radius:16px;background:rgba(20,20,23,.72);
        padding:20px;display:flex;flex-direction:column;gap:8px;color:var(--text,#f4f4f5);font-family:inherit;
        transition:transform .18s,border-color .25s,box-shadow .25s}
      .eynres-card:hover{transform:translateY(-4px);border-color:#c084fc;box-shadow:0 22px 54px -26px rgba(192,132,252,.5)}
      .eynres-fmt{font-size:11px;font-weight:800;letter-spacing:1px;color:#c084fc}
      .eynres-title{font-size:18px;font-weight:800;letter-spacing:-.3px}
      .eynres-desc{color:var(--muted,#9a9aa5);font-size:14px;line-height:1.5;flex:1}
      .eynres-foot{display:flex;align-items:center;justify-content:space-between;margin-top:6px}
      .eynres-price{font-size:19px;font-weight:900}
      .eynres-buy{font-size:13px;font-weight:800;color:#c084fc}
      .eynres-modal{position:fixed;inset:0;z-index:400;display:none;align-items:center;justify-content:center;padding:20px;
        background:rgba(0,0,0,.62);backdrop-filter:blur(4px)}
      .eynres-modal.open{display:flex}
      .eynres-mcard{position:relative;width:100%;max-width:460px;background:#0f0f13;border:1px solid var(--border,#2a2a30);border-radius:20px;padding:28px}
      .eynres-x{position:absolute;top:14px;right:14px;width:34px;height:34px;border-radius:50%;cursor:pointer;
        background:#17171c;border:1px solid var(--border,#2a2a30);color:var(--text,#f4f4f5);font-size:15px}
      .eynres-mtitle{font-size:21px;font-weight:900;letter-spacing:-.4px;padding-right:30px}
      .eynres-mdesc{color:var(--muted,#9a9aa5);font-size:14px;line-height:1.6;margin:8px 0 18px}
      /* admin panel */
      .eynadm{border:1px dashed rgba(192,132,252,.45);border-radius:16px;background:rgba(168,85,247,.06);padding:16px 18px;margin-top:22px}
      .eynadm-head{display:flex;align-items:center;justify-content:space-between;gap:10px;cursor:pointer}
      .eynadm-head b{font-size:15px;font-weight:800}
      .eynadm-badge{font-size:10px;font-weight:800;letter-spacing:.5px;text-transform:uppercase;color:#d6bcff;
        background:rgba(192,132,252,.14);border:1px solid rgba(192,132,252,.3);padding:2px 8px;border-radius:6px}
      .eynadm-body{margin-top:14px;display:none}
      .eynadm-body.open{display:block}
      .eynadm label{display:block;font-size:12px;font-weight:700;color:var(--muted,#9a9aa5);margin:10px 0 5px;letter-spacing:.3px}
      .eynadm input,.eynadm textarea,.eynadm select{width:100%;background:#17171c;border:1px solid var(--border,#2a2a30);
        border-radius:10px;padding:11px 13px;color:var(--text,#f4f4f5);font-size:14px;font-family:inherit}
      .eynadm input:focus,.eynadm textarea:focus,.eynadm select:focus{outline:none;border-color:#a855f7}
      .eynadm textarea{min-height:60px;resize:vertical}
      .eynadm-row{display:flex;gap:12px;flex-wrap:wrap}
      .eynadm-row>div{flex:1;min-width:130px}
      .eynadm-btn{cursor:pointer;border:none;font-family:inherit;font-weight:800;font-size:14px;color:#fff;
        background:linear-gradient(90deg,#a855f7,#ec4899);padding:12px 18px;border-radius:11px;margin-top:16px}
      .eynadm-btn.ghost{background:transparent;color:var(--text,#f4f4f5);border:1px solid var(--border,#2a2a30)}
      .eynadm-btn:disabled{opacity:.55;cursor:default}
      .eynadm-msg{font-size:13px;font-weight:600;min-height:1em;margin-top:10px;color:var(--muted,#9a9aa5)}
      .eynadm-msg.err{color:#fca5a5}.eynadm-msg.ok{color:#4ade80}
      .eynadm-hint{color:var(--muted,#9a9aa5);font-size:12px;margin-top:4px}`;
    document.head.appendChild(s);
  }

  // Fill a <select> with the ₹ presets and show the derived USD next to it.
  function priceControl(defaultInr) {
    const opts = PRESETS_INR.map((v) => `<option value="${v}"${v === defaultInr ? ' selected' : ''}>₹${v} · $${usdForInr(v)}</option>`).join('');
    return `<label>Price (pick one)</label>
      <select class="eynadm-price">${opts}</select>
      <div class="eynadm-hint">Buyers in India pay the ₹ amount; everyone else pays the matching $.</div>`;
  }

  // ---- Public shop: render this page's paid resources with a buy modal ----
  async function mountShop(container, opts) {
    opts = opts || {};
    const page = opts.page || 'library';
    injectCss();
    if (window.EYN && EYN.loadConfig) { try { await EYN.loadConfig(); } catch (e) {} }
    let items = [];
    try { items = ((await (await fetch('/api/resources?page=' + encodeURIComponent(page))).json()).resources) || []; } catch (e) {}
    render();

    function render() {
      container.innerHTML = '';
      if (!items.length) { container.innerHTML = opts.emptyHtml || ''; return; }
      // Labeled section header so every page presents its downloads clearly.
      const title = opts.title || 'Premium downloads';
      const sub = opts.subtitle || 'Paid, downloadable resources — pay once, we email your download link.';
      const head = el('<div class="eynres-head"></div>');
      head.innerHTML = '<h2 class="eynres-h2">' + esc(title) +
        ' <span class="eynres-count">' + items.length + ' file' + (items.length > 1 ? 's' : '') + '</span></h2>' +
        '<p class="eynres-sub">' + esc(sub) + '</p>';
      container.appendChild(head);
      const grid = el('<div class="eynres-grid"></div>');
      items.forEach((r, i) => {
        const price = (window.EYN && EYN.priceFor) ? EYN.priceFor(r).label
          : ('₹' + (r.price_inr != null ? r.price_inr : 99));
        const card = el(
          '<button class="eynres-card" type="button">' +
            '<div class="eynres-fmt">' + esc(r.fmt || 'FILE') + '</div>' +
            '<div class="eynres-title">' + esc(r.title) + '</div>' +
            '<div class="eynres-desc">' + esc(r.desc || '') + '</div>' +
            '<div class="eynres-foot"><span class="eynres-price">' + price + '</span>' +
            '<span class="eynres-buy">Buy &amp; download →</span></div>' +
          '</button>');
        card.addEventListener('click', () => openModal(r));
        grid.appendChild(card);
      });
      container.appendChild(grid);
    }

    function openModal(r) {
      let modal = document.getElementById('eynres-modal');
      if (!modal) {
        modal = el('<div class="eynres-modal" id="eynres-modal"><div class="eynres-mcard">' +
          '<button class="eynres-x" aria-label="Close">✕</button><div class="eynres-mbody"></div></div></div>');
        document.body.appendChild(modal);
        modal.querySelector('.eynres-x').addEventListener('click', () => modal.classList.remove('open'));
        modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.remove('open'); });
      }
      const body = modal.querySelector('.eynres-mbody');
      body.innerHTML = '<h3 class="eynres-mtitle">' + esc(r.title) + '</h3>' +
        '<p class="eynres-mdesc">' + esc(r.desc || '') + '</p><div class="eynres-buybox"></div>';
      if (window.EYN && EYN.mountBuyBox) EYN.mountBuyBox(body.querySelector('.eynres-buybox'), r);
      else body.querySelector('.eynres-buybox').textContent = 'Checkout unavailable — payments not loaded.';
      modal.classList.add('open');
    }

    return { reload: async () => { try { items = ((await (await fetch('/api/resources?page=' + encodeURIComponent(page))).json()).resources) || []; } catch (e) {} render(); } };
  }

  // ---- Admin-only add panel: publish a file to this page, right here ----
  async function mountAdmin(container, opts) {
    opts = opts || {};
    const page = opts.page || 'library';
    const onAdded = opts.onAdded || function () {};
    injectCss();
    let me;
    try { me = await (await fetch('/api/admin/me')).json(); } catch (e) { me = {}; }
    if (!me || !me.enabled) return;              // admin not configured → panel stays invisible
    if (me.admin) renderForm(); else renderLogin();

    function shell(inner) {
      container.innerHTML = '';
      const box = el('<div class="eynadm"></div>');
      box.appendChild(el('<div class="eynadm-head"><b>＋ Add resource to this page</b><span class="eynadm-badge">Admin</span></div>'));
      const body = el('<div class="eynadm-body"></div>');
      body.innerHTML = inner;
      box.appendChild(body);
      box.querySelector('.eynadm-head').addEventListener('click', () => body.classList.toggle('open'));
      container.appendChild(box);
      return body;
    }

    function renderLogin() {
      const body = shell(
        '<label>Admin password</label>' +
        '<input type="password" class="eynadm-pw" placeholder="••••••••" autocomplete="current-password">' +
        '<button class="eynadm-btn eynadm-login">Log in</button>' +
        '<div class="eynadm-msg"></div>');
      const pw = body.querySelector('.eynadm-pw');
      const msg = body.querySelector('.eynadm-msg');
      const go = async () => {
        msg.textContent = 'Checking…'; msg.className = 'eynadm-msg';
        try {
          const r = await fetch('/api/admin/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: pw.value }) });
          const d = await r.json();
          if (r.ok) renderForm(true);
          else { msg.textContent = d.error || 'Login failed.'; msg.className = 'eynadm-msg err'; }
        } catch (e) { msg.textContent = 'Network error.'; msg.className = 'eynadm-msg err'; }
      };
      body.querySelector('.eynadm-login').addEventListener('click', go);
      pw.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    }

    function renderForm(openNow) {
      const defInr = (window.EYN && EYN.PAY && EYN.PAY.page_prices && EYN.PAY.page_prices[page] && EYN.PAY.page_prices[page].INR) || 99;
      const startInr = PRESETS_INR.indexOf(defInr) >= 0 ? defInr : 99;
      const body = shell(
        '<label>File (any type · 40 MB max)</label>' +
        '<input type="file" class="eynadm-file">' +
        '<label>Title</label>' +
        '<input type="text" class="eynadm-title" placeholder="e.g. Ultimate Launch Checklist">' +
        '<div class="eynadm-row">' +
          '<div><label>Category / tag</label><input type="text" class="eynadm-cat" placeholder="e.g. Guides"></div>' +
          '<div>' + priceControl(startInr) + '</div>' +
        '</div>' +
        '<label>Short description</label>' +
        '<textarea class="eynadm-desc" placeholder="One or two lines about this resource."></textarea>' +
        '<button class="eynadm-btn eynadm-up">⬆ Upload &amp; publish</button>' +
        '<button class="eynadm-btn ghost eynadm-out" style="margin-left:8px">Log out</button>' +
        '<div class="eynadm-msg"></div>');
      if (openNow) body.classList.add('open');
      const msg = body.querySelector('.eynadm-msg');
      const btn = body.querySelector('.eynadm-up');
      btn.addEventListener('click', async () => {
        const file = body.querySelector('.eynadm-file').files[0];
        const title = body.querySelector('.eynadm-title').value.trim();
        const inr = parseInt(body.querySelector('.eynadm-price').value, 10);
        if (!file) { msg.textContent = 'Choose a file first.'; msg.className = 'eynadm-msg err'; return; }
        if (!title) { msg.textContent = 'Give it a title.'; msg.className = 'eynadm-msg err'; return; }
        if (file.size > 40 * 1024 * 1024) { msg.textContent = 'File is over 40 MB.'; msg.className = 'eynadm-msg err'; return; }
        btn.disabled = true; msg.textContent = 'Uploading…'; msg.className = 'eynadm-msg';
        try {
          const data = await readB64(file);
          const r = await fetch('/api/admin/upload', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              title, filename: file.name, page, cat: body.querySelector('.eynadm-cat').value.trim(),
              desc: body.querySelector('.eynadm-desc').value.trim(),
              price_inr: inr, price_usd: usdForInr(inr), data
            })
          });
          const d = await r.json();
          if (r.ok) {
            msg.textContent = '✓ Published “' + d.resource.title + '”.'; msg.className = 'eynadm-msg ok';
            body.querySelector('.eynadm-file').value = ''; body.querySelector('.eynadm-title').value = '';
            body.querySelector('.eynadm-cat').value = ''; body.querySelector('.eynadm-desc').value = '';
            onAdded(d.resource);
          } else { msg.textContent = d.error || 'Upload failed.'; msg.className = 'eynadm-msg err'; }
        } catch (e) { msg.textContent = 'Upload error: ' + e.message; msg.className = 'eynadm-msg err'; }
        btn.disabled = false;
      });
      body.querySelector('.eynadm-out').addEventListener('click', async () => {
        try { await fetch('/api/admin/logout', { method: 'POST' }); } catch (e) {}
        renderLogin();
      });
    }
  }

  return { PRESETS_INR, INR_TO_USD, usdForInr, mountShop, mountAdmin };
})();
