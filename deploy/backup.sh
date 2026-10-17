#!/usr/bin/env bash
# Back up the database, the registries, trade state and user accounts to data/backups/<time>/.
# Keeps the newest AITRADER_BACKUP_KEEP (default 14). With AITRADER_BACKUP_COPY_TO set (any
# rsync destination: another disk, or user@host:/path over SSH) it also copies them off this
# machine. Exchange keys inside accounts.db stay encrypted; keep .env (AITRADER_KEYS_SECRET)
# backed up separately.
#
#   ./deploy/backup.sh
#   cron: 30 3 * * * cd ~/AI_crypto_trader && ./deploy/backup.sh >> data/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
fromenv() { [ -f .env ] && grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true; }
: "${AITRADER_BACKUP_KEEP:=$(fromenv AITRADER_BACKUP_KEEP)}"
: "${AITRADER_BACKUP_COPY_TO:=$(fromenv AITRADER_BACKUP_COPY_TO)}"
KEEP="${AITRADER_BACKUP_KEEP:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
DEST="data/backups/$STAMP"
mkdir -p "$DEST"
docker compose run --rm -T collector backup "$DEST" >/dev/null
echo "$(date -u +%FT%TZ) backup written to $DEST"
ls -1d data/backups/*/ 2>/dev/null | sort | head -n "-$KEEP" | xargs -r rm -rf
if [ -n "${AITRADER_BACKUP_COPY_TO:-}" ]; then
  rsync -a --delete data/backups/ "$AITRADER_BACKUP_COPY_TO/"
  echo "$(date -u +%FT%TZ) copied to $AITRADER_BACKUP_COPY_TO"
fi
