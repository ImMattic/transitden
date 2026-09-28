#!/bin/sh
# update-gtfs.sh — Download and replace the five RTD GTFS static feeds.
#
# RTD publishes a new service period roughly every 3 months.  When trip IDs
# roll over, the on-time dashboard goes dark (0 arrivals logged) because
# classify_arrival() can't match live trip_ids against the expired schedule.
#
# Usage:
#   ./scripts/update-gtfs.sh
#
# After running:
#   docker compose restart backend
#   docker compose exec backend python -m scripts.backfill_ontime

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GTFS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)/gtfs-static"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Updating RTD GTFS static feeds -> $GTFS_ROOT"
echo ""

download_feed() {
    folder="$1"
    url="$2"
    dest="$GTFS_ROOT/$folder"
    zip="$TMP_DIR/${folder}.zip"
    extract_dir="$TMP_DIR/${folder}_extract"

    echo "  -> $folder"
    curl -fsSL --progress-bar "$url" -o "$zip"

    mkdir -p "$extract_dir"
    unzip -q -o "$zip" -d "$extract_dir"

    # RTD zips are sometimes flat, sometimes nested one level deep.
    # Always find the .txt files regardless of depth and copy them in.
    rm -f "$dest"/*.txt
    find "$extract_dir" -name "*.txt" -exec cp {} "$dest/" \;

    feed_end=$(sed -n '2p' "$dest/feed_info.txt" 2>/dev/null | tr -d '"' | awk -F',' '{print $5}' || echo "unknown")
    trip_count=$(( $(wc -l < "$dest/trips.txt") - 1 ))
    echo "    feed_end_date=$feed_end  trips=$trip_count"
    echo ""
}

download_feed light_rail        "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Operated_Light_Rail_GTFS.zip"
download_feed op_commuter_rail  "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Operated_Commuter_Rail_GTFS.zip"
download_feed op_motorbus       "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Operated_Motorbus_GTFS.zip"
download_feed pur_commuter_rail "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Purchased_Transportation_Commuter_Rail_GTFS.zip"
download_feed pur_motorbus      "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Purchased_Transportation_Motorbus_GTFS.zip"

echo "All feeds updated."
echo ""
echo "Next steps:"
echo "  docker compose restart backend"
echo "  docker compose exec backend python -m scripts.backfill_ontime"
