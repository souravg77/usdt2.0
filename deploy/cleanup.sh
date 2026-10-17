#!/usr/bin/env bash
# Free disk space without touching anything the platform needs. Removes only:
#   - old Docker images left behind by earlier installs (each install builds a new one; the one
#     running now is kept) and Docker build cache older than a week,
#   - the server's own leftovers: downloaded system packages and system logs over 200 MB,
#   - the platform's cron logs (data/*.log) cut to their last 5,000 lines.
# The price database, strategies, trades, wallets, settings, backups and .env are never touched.
#
#   ./deploy/cleanup.sh
#   cron: 0 5 * * 0 cd ~/AI_crypto_trader && ./deploy/cleanup.sh >> data/cleanup.log 2>&1
#   (install.sh runs it and adds this weekly cron line for you)
set -uo pipefail
cd "$(dirname "$0")/.."
DOCKER="${DOCKER:-docker}"
before=$(df -Pk . | awk 'NR==2 {print $4}')
$DOCKER image prune -f >/dev/null || true                         # unused old builds only
$DOCKER builder prune -f --filter until=168h >/dev/null || true    # build cache over a week old
if sudo -n true 2>/dev/null; then                                  # only when no password is asked
  sudo -n apt-get clean >/dev/null 2>&1 || true
  sudo -n journalctl --vacuum-size=200M >/dev/null 2>&1 || true
fi
for f in data/*.log; do
  [ -f "$f" ] || continue
  if [ "$(wc -l < "$f")" -gt 5000 ]; then
    tail -n 5000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  fi
done
after=$(df -Pk . | awk 'NR==2 {print $4}')
echo "$(date -u +%FT%TZ) cleanup freed $(( (after - before) / 1024 )) MB; $(( after / 1024 / 1024 )) GB free now"
