#!/usr/bin/env bash
# Back up the database, the registries, trade state and user accounts to data/backups/<time>/.
# The price candles are left out (they can be downloaded again: press "Reload every strategy" or
# run `aitrader download`), so each backup is a few MB instead of a full copy of the price
# history; set AITRADER_BACKUP_CANDLES=true to include them. Keeps the newest AITRADER_BACKUP_KEEP (default 14). With AITRADER_BACKUP_COPY_TO set (any
# rsync destination: another disk, or user@host:/path over SSH) it also copies them off this
# machine. Exchange keys inside accounts.db stay encrypted; keep .env (AITRADER_KEYS_SECRET)
# backed up separately.
#
#   ./deploy/backup.sh
#   cron: 30 3 * * * cd ~/AI_crypto_trader && ./deploy/backup.sh >> data/backup.log 2>&1
#   (install.sh adds this cron line for you)
set -euo pipefail
umask 077  # backups hold password hashes and encrypted keys: readable by you only
cd "$(dirname "$0")/.."
fromenv() { [ -f .env ] && grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true; }
: "${AITRADER_BACKUP_KEEP:=$(fromenv AITRADER_BACKUP_KEEP)}"
: "${AITRADER_BACKUP_COPY_TO:=$(fromenv AITRADER_BACKUP_COPY_TO)}"
: "${AITRADER_BACKUP_CANDLES:=$(fromenv AITRADER_BACKUP_CANDLES)}"
KEEP="${AITRADER_BACKUP_KEEP:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
DEST="data/backups/$STAMP"
mkdir -p "$DEST"
chmod 700 data/backups
CANDLES=(--no-candles)
[ "${AITRADER_BACKUP_CANDLES:-}" = "true" ] && CANDLES=()
${DOCKER:-docker} compose run --rm -T collector backup "$DEST" "${CANDLES[@]}" >/dev/null
echo "$(date -u +%FT%TZ) backup written to $DEST"
ls -1d data/backups/*/ 2>/dev/null | sort | head -n "-$KEEP" | xargs -r rm -rf
if [ -n "${AITRADER_BACKUP_COPY_TO:-}" ]; then
  rsync -a --delete data/backups/ "$AITRADER_BACKUP_COPY_TO/"
  echo "$(date -u +%FT%TZ) copied to $AITRADER_BACKUP_COPY_TO"
fi
