#!/usr/bin/env bash
# Update the agent on the VPS to the latest code on GitHub, safely.
#
#   ./deploy/update.sh            update if origin/main has new commits
#   ./deploy/update.sh --check    only say whether an update is available
#   ./deploy/update.sh --force    rebuild and restart even if already up to date
#
# Steps: fetch -> back up the database and registries -> fast-forward the code -> rebuild the
# image -> migrate the database -> restart the services that were running -> health check.
# If any step after the code moves fails, it rolls back: previous commit, previous image,
# database restored from the backup, services restarted. Exit code 0 = updated (or nothing to
# do), 1 = not updated (rolled back or refused).
#
# It never edits .env, so it cannot unlock live trading. As an extra guard, the health check
# fails (and the update is rolled back) if live trading was locked before and is not after.
#
# Settings (environment): AITRADER_UPDATE_BRANCH (default main), AITRADER_UPDATE_WAIT (seconds
# to let services settle before the health check, default 30), AITRADER_UPDATE_KEEP (full
# backups kept, default 1: each holds the whole price history, about 4 GB).

set -Eeuo pipefail

BRANCH="${AITRADER_UPDATE_BRANCH:-main}"
WAIT="${AITRADER_UPDATE_WAIT:-30}"
KEEP="${AITRADER_UPDATE_KEEP:-1}"
MODE="${1:-}"
IMAGE="aitrader:latest"
ROLLBACK_IMAGE="aitrader:rollback"

cd "$(dirname "$0")/.."
log() { printf '%s update: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "$*"; exit 1; }

mkdir -p data
for d in data data/backups; do
  [ ! -e "$d" ] || [ -w "$d" ] || die "can't write to $d as $(id -un): run sudo chown -R $(id -u):$(id -g) data and set AITRADER_UID=$(id -u) in .env"
done
exec 9>data/.update.lock
flock -n 9 || die "another update is already running"

# ---- what is there to update? -------------------------------------------------------------------

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no
  die "tracked files were edited on this server; commit or 'git stash' them first (.env is not affected)"
fi
[ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ] || die "checkout is not on '$BRANCH' (run: git checkout $BRANCH)"

git fetch --quiet origin "$BRANCH"
PREV="$(git rev-parse HEAD)"
NEXT="$(git rev-parse "origin/$BRANCH")"

if [ "$PREV" = "$NEXT" ]; then
  if [ "$MODE" = "--check" ]; then log "up to date ($(git log -1 --format='%h %s'))"; exit 0; fi
  [ "$MODE" = "--force" ] || { log "already up to date ($(git log -1 --format='%h %s'))"; exit 0; }
fi
git merge-base --is-ancestor "$PREV" "$NEXT" || die "origin/$BRANCH has history this server doesn't; not updating (ask for help)"

if [ "$MODE" = "--check" ]; then
  log "update available: $(git rev-list --count "$PREV..$NEXT") new commit(s)"
  git log --format='  %h %s' "$PREV..$NEXT"
  exit 0
fi

# ---- before: state to compare and to roll back to -----------------------------------------------

live_state() {  # prints "locked", "UNLOCKED" or "unknown"
  docker compose run --rm -T collector status 2>/dev/null \
    | sed -n 's/^live trading: //p' | head -n1 | grep . || echo unknown
}

RUNNING="$(docker compose ps --services --status running 2>/dev/null | sort | tr '\n' ' ' || true)"
LIVE_BEFORE="$(live_state)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP="data/backups/$STAMP-full"  # full: a failed migration restores the database from it
log "updating $(git rev-parse --short "$PREV") -> $(git rev-parse --short "$NEXT"); running: ${RUNNING:-none}; live trading: $LIVE_BEFORE"

mkdir -p "$BACKUP"
if ! docker compose run --rm -T collector backup "$BACKUP" >/dev/null 2>&1; then
  # older images have no `backup` command: copy the files instead
  for f in data/aitrader.db data/accounts.db data/learn/registry.json data/learn/paper_state.json data/ai/models/registry.json; do
    [ -f "$f" ] && cp -p "$f" "$BACKUP/$( [ "$f" = data/aitrader.db ] && echo aitrader.db || echo "${f//\//__}")"
  done
fi
log "backup in $BACKUP"
if docker image inspect "$IMAGE" >/dev/null 2>&1; then docker tag "$IMAGE" "$ROLLBACK_IMAGE"; fi

# ---- update, with rollback on any failure -------------------------------------------------------

MOVED=0
MIGRATED=0
rollback() {
  trap - ERR
  set +e
  log "FAILED: $1 -- rolling back to $(git rev-parse --short "$PREV")"
  docker compose stop >/dev/null 2>&1
  if [ "$MOVED" = 1 ]; then git reset --quiet --hard "$PREV"; fi
  if docker image inspect "$ROLLBACK_IMAGE" >/dev/null 2>&1; then docker tag "$ROLLBACK_IMAGE" "$IMAGE"; fi
  if [ "$MIGRATED" = 1 ] && [ -f "$BACKUP/aitrader.db" ]; then
    cp -p "$BACKUP/aitrader.db" data/aitrader.db && rm -f data/aitrader.db-wal data/aitrader.db-shm
    log "database restored from $BACKUP"
  fi
  # shellcheck disable=SC2086
  [ -n "$RUNNING" ] && docker compose up -d $RUNNING >/dev/null 2>&1
  log "rolled back; the services that were running are running the previous version again"
  exit 1
}
trap 'rollback "step failed at line $LINENO"' ERR

git merge --quiet --ff-only "$NEXT"
MOVED=1
log "code updated:"
git log --format='  %h %s' "$PREV..$NEXT" | head -n 30

docker compose build --pull
MIGRATED=1
docker compose run --rm -T collector migrate

if [ -n "$RUNNING" ]; then
  # shellcheck disable=SC2086
  docker compose up -d --remove-orphans $RUNNING
  log "waiting ${WAIT}s for services to settle"
  sleep "$WAIT"
fi

# ---- health check -------------------------------------------------------------------------------

for svc in $RUNNING; do
  state="$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="$svc" '$1==s {print $2}')"
  [ "$state" = "running" ] || rollback "service '$svc' is '${state:-missing}' after the update"
done
docker compose run --rm -T collector status >/dev/null || rollback "'aitrader status' failed"
LIVE_AFTER="$(live_state)"
if [ "$LIVE_BEFORE" != "UNLOCKED" ] && [ "$LIVE_AFTER" != "locked" ]; then
  rollback "live trading reads '$LIVE_AFTER' after the update (was '$LIVE_BEFORE')"
fi
case " $RUNNING " in
  *" web "*) curl -fsS --max-time 5 http://127.0.0.1:8080/healthz >/dev/null || rollback "dashboard health check failed" ;;
esac

trap - ERR
ls -1d data/backups/*-full/ 2>/dev/null | sort | head -n "-$KEEP" | xargs -r rm -rf  # daily ones: backup.sh
log "done: now on $(git log -1 --format='%h %s'); live trading: $LIVE_AFTER"
