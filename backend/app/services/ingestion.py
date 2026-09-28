"""Ingest one GTFS-RT poll cycle into the database.

Called by the scheduler every N seconds.  Fetches both the VehiclePosition and
TripUpdate feeds, decodes them, and bulk-inserts the new rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.stop_arrival import StopArrivalEvent
from app.models.trip_update import TripUpdate
from app.models.vehicle_position import VehiclePosition
from app.services.gtfs_decoder import (
    extract_trip_updates_from_bytes,
    extract_vehicle_positions_from_bytes,
    load_gtfs_static_data,
)
from app.services.gtfs_health import check_and_alert, observe_trip_ids
from app.services.gtfs_rt_fetcher import fetch_pb
from app.services.ontime import detect_arrivals
from app.services.position_anomaly import PositionAnomalyFilter, filter_batch

logger = logging.getLogger(__name__)

_settings = get_settings()

# One instance for the life of the process, so a suspect jump held on one poll
# can be resolved by the vehicle's fix on the next — see position_anomaly.py.
_anomaly_filter = PositionAnomalyFilter()


def _resolve_gtfs_static_root() -> Path:
    """Find gtfs-static directory across local and container layouts."""
    current = Path(__file__).resolve()

    # Common container and local defaults first.
    candidates = [Path("/app/gtfs-static"), current.parents[3] / "gtfs-static"]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    # Fallback: walk upward from this file and pick the first matching folder.
    for parent in current.parents:
        candidate = parent / "gtfs-static"
        if candidate.exists():
            return candidate

    # Last-resort default keeps behavior deterministic even before volume mounts.
    return Path("/app/gtfs-static")


_GTFS_STATIC_ROOT = _resolve_gtfs_static_root()


async def _load_static() -> (
    tuple[dict[str, dict], dict[str, dict]]
):
    """Load GTFS static routes + stops once per process (cached lazily)."""
    return load_gtfs_static_data(gtfs_static_root=_GTFS_STATIC_ROOT)


# Module-level cache so we don't re-read CSV files on every poll.
_routes_cache: dict[str, dict] | None = None
_stops_cache: dict[str, dict] | None = None


async def _get_static() -> tuple[dict[str, dict], dict[str, dict]]:
    global _routes_cache, _stops_cache
    if _routes_cache is None or _stops_cache is None:
        _routes_cache, _stops_cache = await _load_static()
    return _routes_cache, _stops_cache


async def ingest_cycle() -> None:
    """Run one full ingest cycle: fetch → decode → persist."""
    now = datetime.now(tz=timezone.utc)
    routes, stops = await _get_static()

    # ── Fetch both feeds concurrently ──────────────────────────────────────
    import asyncio

    results = await asyncio.gather(
        _fetch_safe(_settings.gtfs_rt_vehicle_url, "vehicle"),
        _fetch_safe(_settings.gtfs_rt_trip_url, "trip"),
    )
    vp_bytes, tu_bytes = results

    # ── Decode ─────────────────────────────────────────────────────────────
    vp_rows: list[dict] = []
    if vp_bytes:
        try:
            vp_rows = extract_vehicle_positions_from_bytes(vp_bytes, routes, stops, now)
        except Exception:
            logger.exception("Failed to decode VehiclePosition feed")

    tu_rows: list[dict] = []
    if tu_bytes:
        try:
            tu_rows = extract_trip_updates_from_bytes(tu_bytes, now)
        except Exception:
            logger.exception("Failed to decode TripUpdate feed")

    # ── Persist ────────────────────────────────────────────────────────────
    if not vp_rows and not tu_rows:
        logger.warning("No data decoded; skipping DB write")
        return

    def _dedupe_vehicle_rows(rows: list[dict]) -> list[dict]:
        """Remove duplicate vehicle snapshots using a stable key.

        Key: prefer vehicle_id, then trip_id, then route_id, combined with timestamp
        and position to avoid inserting exact duplicates coming from the feed.
        """
        seen: set = set()
        out: list[dict] = []
        for r in rows:
            key_id = r.get("vehicle_id") or r.get("trip_id") or r.get("route_id") or ""
            # Exclude timestamp from dedupe key — ingest_time is unique per poll
            # so keying on it would never catch intra-batch duplicates.
            key = (key_id, r.get("latitude"), r.get("longitude"))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    # Derive observed on-time arrivals from this poll's positions (geofence vs.
    # the static timepoint schedule). De-duped against recent polls in-process.
    arrival_events: list[dict] = []
    if vp_rows:
        vp_rows = _dedupe_vehicle_rows(vp_rows)
        # Gate out implausible GPS jumps before anything else sees this batch:
        # a flagged fix still gets written below (see position_anomaly.py) but
        # must never feed on-time detection, or a single bad fix can record a
        # wrong departure/arrival before the bounce-back is seen.
        vp_rows = filter_batch(_anomaly_filter, vp_rows, now)
        clean_rows = [r for r in vp_rows if not r.get("is_anomalous")]
        # How many of these trip_ids the static schedule still knows about.
        # detect_arrivals is about to ask exactly this question and silently
        # return nothing when the answer is "none of them", which is what a
        # service-pick rollover looks like from in here — see gtfs_health.py.
        observe_trip_ids(r.get("trip_id") for r in clean_rows)
        try:
            arrival_events = detect_arrivals(clean_rows, now)
        except Exception:
            logger.exception("Failed to derive on-time arrival events")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            if vp_rows:
                await session.execute(insert(VehiclePosition), vp_rows)
            if tu_rows:
                await session.execute(insert(TripUpdate), tu_rows)
            if arrival_events:
                await session.execute(insert(StopArrivalEvent), arrival_events)

    health = await check_and_alert()

    logger.info(
        "Ingest cycle complete: %d vehicle positions, %d trip updates, "
        "%d on-time arrivals (schedule %s)",
        len(vp_rows),
        len(tu_rows),
        len(arrival_events),
        health.get("status", "unknown"),
    )


async def _fetch_safe(url: str, label: str) -> bytes | None:
    try:
        return await fetch_pb(url)
    except Exception:
        logger.exception("Failed to fetch %s feed from %s", label, url)
        return None
