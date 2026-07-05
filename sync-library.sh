#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Reconcile the live Library to reality, then publish.
#
# The site is static, so nothing auto-hides deleted resources. Run this after
# you ADD or REMOVE resources. It removes any resource whose source file is
# gone, rebuilds the downloadable files, and redeploys.
#
# To REMOVE a resource, do either one, then run this:
#   • delete its file from  library/   (its symlink), OR
#   • delete its entry from static/library-data.js
#
# Usage:
#   ./sync-library.sh            # reconcile + deploy
#   ./sync-library.sh --no-deploy  # reconcile only (preview locally)
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import re, json, os, shutil, unicodedata
DATA = 'static/library-data.js'; LIBDIR = 'library'; OUT = 'library-files'
src = open(DATA, encoding='utf-8').read()
arr  = json.loads(re.search(r'window\.LIBRARY\s*=\s*(\[.*?\]);', src, re.S).group(1))
cats = re.search(r'window\.LIBRARY_CATS\s*=\s*(\[.*?\]);', src, re.S).group(1)

def slugify(t, ext, used):
    s = unicodedata.normalize('NFKD', t).encode('ascii','ignore').decode()
    s = re.sub(r'[^a-zA-Z0-9]+','-', s).strip('-').lower()[:60] or 'resource'
    b, n = s, 2
    while s+ext in used: s = f"{b}-{n}"; n += 1
    used.add(s+ext); return s+ext

used, kept, dropped = set(), [], []
for d in arr:
    real = os.path.realpath(os.path.join(LIBDIR, d['file']))
    # keep only resources whose source file still exists and fits free hosting
    if os.path.exists(real) and os.path.getsize(real) <= 50*1024*1024:
        ext = os.path.splitext(d['file'])[1] or '.pdf'
        d['slug'] = slugify(d['title'], ext, used)
        kept.append((d, real))
    else:
        dropped.append(d['title'])

# rebuild library-files/ to EXACTLY match the surviving catalog (removes orphans)
if os.path.isdir(OUT): shutil.rmtree(OUT)
os.makedirs(OUT)
for d, real in kept:
    shutil.copyfile(real, os.path.join(OUT, d['slug']))

lib  = "window.LIBRARY = [\n" + ",\n".join("  "+json.dumps(d, ensure_ascii=False) for d,_ in kept) + "\n];\n"
lib += "window.LIBRARY_CATS = " + cats + ";\n"
open(DATA, 'w', encoding='utf-8').write(lib)

print(f"live now: {len(kept)} resources   removed: {len(dropped)}")
for t in dropped: print("   removed:", t[:55])
PY

if [[ "${1:-}" == "--no-deploy" ]]; then
  echo "Reconcile done (skipped deploy). Run without --no-deploy to publish."
  exit 0
fi
echo "Deploying to Firebase…"
firebase deploy --only hosting --project resource-arjusingh
echo "✅ Live site updated: https://resource-arjusingh.web.app/library.html"
