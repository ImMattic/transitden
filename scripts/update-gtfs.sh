#!/bin/sh
# update-gtfs.sh — compatibility shim for scripts/refresh_gtfs.py.
#
# This script used to download all five RTD feeds and overwrite gtfs-static/
# unconditionally.  That turns out to be the wrong thing to do on a schedule:
# RTD publishes the NEXT service pick days before it takes effect and
# consecutive picks share no trip_ids, so installing what RTD is serving today
# can blind on-time detection until the changeover actually happens.
#
# refresh_gtfs.py does the same downloads but stages a pick that isn't in
# service yet and promotes it on its feed_start_date.  It runs daily from
# .github/workflows/gtfs-refresh.yml, so hand-running this should be rare.
#
# Usage:
#   ./scripts/update-gtfs.sh            # fetch; stage or promote by date
#   ./scripts/update-gtfs.sh --force    # install what RTD serves now, regardless

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

exec python3 "$SCRIPT_DIR/refresh_gtfs.py" "$@"
