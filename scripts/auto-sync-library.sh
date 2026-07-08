#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Wrapper invoked by launchd (WatchPaths on rom/) whenever the rom/ folder
# changes. Reconciles the catalog to reality and deploys to Firebase, so a
# PDF deleted from rom/ automatically disappears from the live website.
#
# Managed by:  ~/Library/LaunchAgents/com.arjusingh.library-sync.plist
# Log:         .library-sync.log   (in the repo)
#
# Manual controls:
#   launchctl unload ~/Library/LaunchAgents/com.arjusingh.library-sync.plist  # pause
#   launchctl load   ~/Library/LaunchAgents/com.arjusingh.library-sync.plist  # resume
# ---------------------------------------------------------------------------
set -uo pipefail
REPO="/Users/arju/Downloads/Resource"
LOG="$REPO/.library-sync.log"
LOCK="$REPO/.library-sync.lock"

# launchd runs with a bare environment — make the CLIs resolvable.
export PATH="/Users/arju/.npm-global/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/3.14/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd "$REPO" || exit 1

# Single-instance lock (mkdir is atomic) so bursts of file changes and an
# in-flight deploy don't stack up into overlapping firebase deploys.
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') skipped — a sync is already running" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Debounce: let a burst of adds/removes settle before acting.
sleep 5

echo "$(date '+%F %T') === rom/ changed → reconcile + deploy ===" >> "$LOG"
./scripts/sync-library.sh >> "$LOG" 2>&1
status=$?
echo "$(date '+%F %T') === finished (exit $status) ===" >> "$LOG"
exit $status
