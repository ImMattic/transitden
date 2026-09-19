#!/bin/sh
# Redeploy the TransitDen production stack when either the Docker Hub :latest
# images OR the git checkout of `main` changes.
#
# Cron (every minute):
#   * * * * * /opt/transitden/scripts/prod-update.sh >> /var/log/transitden-update.log 2>&1
#
# This only ever deploys something that has landed on `main`: the :latest
# images are only rebuilt by .github/workflows/docker-build-push.yml on a
# push to main, and the git checkout below only ever tracks origin/main. A
# push to any other branch (including `staging`) changes neither, so this
# script no-ops.
#
# The checkout at $REPO is treated as a DISPOSABLE MIRROR of origin/$BRANCH:
# every run does `fetch` + `reset --hard` + `clean`, so any local edits or stray
# files on the VM are discarded and the box always reflects the repo. (Ignored
# files such as deployment/.env are preserved -- see the `git clean` line.)
#
# Why the gtfs-static handling matters: gtfs-static/ is bind-mounted into the
# backend (not baked into the image) and the backend caches those CSVs in memory
# for the life of the process. A repo update that only refreshes the feeds
# changes neither an image digest nor the Compose config, so `docker compose
# up -d` alone will NOT pick it up -- the backend keeps serving stale routes
# (e.g. commuter rail shows the raw route_id "113B" instead of "B"). This script
# force-recreates the backend whenever gtfs-static/ changes.
#
# Why the on-time backfill is OPT-IN: stop_arrival_events rows are written once,
# at ingest time, by whatever arrival-detection rule (app/services/ontime.py)
# was live that moment. Changing that rule only affects arrivals detected from
# then on -- it does not retroactively fix rows already written by the old rule.
# Replaying every stored vehicle_position through the new rule fixes that, but
# it takes hours, so it has to be ASKED FOR: put [backfill] anywhere in a commit
# message and the replay runs once the new backend image is confirmed live.
# Ordinary feature pushes -- including ones that touch ontime.py -- deploy with
# no replay.
#
# The replay itself is non-destructive: scripts/backfill_ontime.py fills a
# scratch table and swaps it in atomically, so the dashboards serve the old
# history throughout and a crashed replay leaves the live table untouched.

set -eu

# Serialise overlapping cron runs -- a deploy can outlast the 1-minute interval.
if [ "${TRANSITDEN_LOCKED:-}" != "1" ] && command -v flock >/dev/null 2>&1; then
    TRANSITDEN_LOCKED=1
    export TRANSITDEN_LOCKED
    exec flock -n /tmp/transitden-update.lock "$0" "$@"
fi

# cron runs with a minimal PATH; docker / git may not be on it.
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

REPO="/opt/transitden"
BRANCH="main"
COMPOSE_FILE="$REPO/deployment/docker-compose.prod.yml"
ENV_FILE="$REPO/deployment/.env"
BACKEND_IMAGE="aggiematt/transitden-backend:latest"
FRONTEND_IMAGE="aggiematt/transitden-frontend:latest"
HEALTH_URL="http://127.0.0.1:8000/api/v1/realtime/vehicles"
# Deliberately outside $REPO -- the git reset/clean below would otherwise wipe
# it -- so "a backfill is owed" persists across cron ticks if the git checkout
# advances before the new backend image finishes building on Docker Hub.
BACKFILL_MARKER="/tmp/transitden-backfill-pending"

log()    { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
dc()     { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }
digest() { docker inspect --format='{{index .RepoDigests 0}}' "$1" 2>/dev/null || echo none; }

# ── 1. Force the checkout to match origin/$BRANCH exactly ─────────────────
# Local edits, staged changes and untracked files are ALL discarded every run.
# Ignored files (deployment/.env, venv/, ...) are kept -- add -x to `git clean`
# below if you want a totally pristine tree.
if ! git -C "$REPO" fetch --quiet --prune origin; then
    log "ERROR: git fetch failed (network / auth / remote down). NOTHING deployed."
    exit 1
fi
if ! git -C "$REPO" rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
    log "ERROR: origin/$BRANCH not found -- set BRANCH at the top of this script."
    exit 1
fi

GIT_BEFORE=$(git -C "$REPO" rev-parse HEAD)
DISCARDED=$(git -C "$REPO" status --porcelain)
git -C "$REPO" reset --hard --quiet "origin/$BRANCH"
git -C "$REPO" clean -ffd --quiet
GIT_AFTER=$(git -C "$REPO" rev-parse HEAD)

if [ -n "$DISCARDED" ]; then
    log "Discarded local VM changes to match origin/$BRANCH:"
    printf '%s\n' "$DISCARDED" | sed 's/^/  /'
fi

REPO_CHANGED=0
GTFS_CHANGED=0
if [ "$GIT_BEFORE" != "$GIT_AFTER" ]; then
    REPO_CHANGED=1
    log "Checkout $(git -C "$REPO" rev-parse --short "$GIT_BEFORE") -> $(git -C "$REPO" rev-parse --short "$GIT_AFTER"); changed files:"
    git -C "$REPO" diff --name-only "$GIT_BEFORE" "$GIT_AFTER" | sed 's/^/  /'
    if [ -n "$(git -C "$REPO" diff --name-only "$GIT_BEFORE" "$GIT_AFTER" -- gtfs-static)" ]; then
        GTFS_CHANGED=1
    fi
    # Opt-in only -- see the header. -F so the brackets are literal.
    if git -C "$REPO" log --format='%B' "$GIT_BEFORE..$GIT_AFTER" 2>/dev/null \
        | grep -qiF '[backfill]'; then
        log "[backfill] requested in an incoming commit -- backfill owed once the new backend image is live."
        touch "$BACKFILL_MARKER"
    fi
fi

# ── 2. Pull images, note which digests moved ─────────────────────────────
B_BEFORE=$(digest "$BACKEND_IMAGE");  F_BEFORE=$(digest "$FRONTEND_IMAGE")
docker pull --quiet "$BACKEND_IMAGE"  >/dev/null 2>&1 || log "WARN: docker pull $BACKEND_IMAGE failed"
docker pull --quiet "$FRONTEND_IMAGE" >/dev/null 2>&1 || log "WARN: docker pull $FRONTEND_IMAGE failed"
B_AFTER=$(digest "$BACKEND_IMAGE");   F_AFTER=$(digest "$FRONTEND_IMAGE")

BACKEND_IMG_CHANGED=0
if [ "$B_BEFORE" != "$B_AFTER" ]; then BACKEND_IMG_CHANGED=1; log "Backend image updated."; fi
FRONTEND_IMG_CHANGED=0
if [ "$F_BEFORE" != "$F_AFTER" ]; then FRONTEND_IMG_CHANGED=1; log "Frontend image updated."; fi

# ── 3. Deploy only if something actually changed ─────────────────────────
if [ "${REPO_CHANGED}${BACKEND_IMG_CHANGED}${FRONTEND_IMG_CHANGED}" = "000" ]; then
    log "No changes."
    exit 0
fi

log "Reconciling stack..."
if ! dc up -d --no-deps --pull never db frontend backend; then
    log "ERROR: 'docker compose up' failed."
    exit 1
fi

# A gtfs-static refresh with no new backend image is invisible to the reconcile
# above (bind mount + in-process cache), so the running backend must be told to
# restart and reload the feeds.
if [ "$GTFS_CHANGED" = "1" ] && [ "$BACKEND_IMG_CHANGED" = "0" ]; then
    log "gtfs-static changed without a backend image bump -> force-recreating backend."
    dc up -d --no-deps --force-recreate backend
fi

# A pending backfill only runs once the backend image itself has actually
# changed (not merely once the git checkout has) -- BACKEND_IMG_CHANGED means
# the just-pulled image is guaranteed to contain whatever ontime.py change
# earned the marker, whereas the git checkout and the Docker Hub push can land
# on different cron ticks. `-T` disables TTY allocation, required under cron.
# The marker is left in place on failure so the next backend image bump (or a
# manual run) retries it -- see backend/scripts/backfill_ontime.py to run by hand.
if [ -f "$BACKFILL_MARKER" ] && [ "$BACKEND_IMG_CHANGED" = "1" ]; then
    log "New backend image live with a requested on-time backfill -> replaying stop_arrival_events..."
    if dc exec -T -e STATEMENT_TIMEOUT_MS=0 backend python scripts/backfill_ontime.py; then
        rm -f "$BACKFILL_MARKER"
        log "Backfill complete."
    else
        log "ERROR: backfill failed -- stop_arrival_events keeps its previous contents (the swap is all-or-nothing). Will retry on the next backend image update, or run manually:"
        log "  docker compose -f $COMPOSE_FILE --env-file $ENV_FILE exec -e STATEMENT_TIMEOUT_MS=0 backend python scripts/backfill_ontime.py"
    fi
fi

# ── 4. Smoke-test: the endpoint that breaks when routes don't resolve ────
i=0
while [ "$i" -lt 30 ]; do
    if wget -q -T 5 -O /dev/null "$HEALTH_URL" 2>/dev/null; then
        log "Backend healthy. Done."
        exit 0
    fi
    i=$((i + 1))
    sleep 2
done
log "WARN: backend did not answer $HEALTH_URL within 60s after deploy."
