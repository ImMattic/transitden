"""Real-time vehicle position endpoints."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.vehicle_position import VehiclePosition
from app.models.trip_update import TripUpdate
from app.schemas.vehicle_position import RealtimeResponse, VehiclePositionOut
from app.services.gtfs_decoder import load_gtfs_static_data

router = APIRouter(prefix="/realtime", tags=["realtime"])

# ── GTFS static helpers ────────────────────────────────────────────────────

_VEHICLE_STATUS_LABELS = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}

_VEHICLE_TYPE_NAMES = {
    "0": "light_rail",
    "1": "heavy_rail",
    "2": "commuter_rail",
    "3": "bus",
}


def _routes_and_stops() -> tuple[dict, dict]:
    from app.services.ingestion import _routes_cache, _stops_cache

    if _routes_cache and _stops_cache:
        return _routes_cache, _stops_cache
    return load_gtfs_static_data()


# ── Helpers ────────────────────────────────────────────────────────────────

async def _latest_positions(
    db: AsyncSession,
    route_id: str | None = None,
) -> list[VehiclePosition]:
    """Return the most-recent snapshot per vehicle_id within the last 60 seconds.

    Since timestamps now reflect ingest time (not GPS clock), this window
    reliably captures the last ~2 poll cycles at the default 30s interval.

    Uses Postgres ``DISTINCT ON`` (one index-friendly scan of the hot chunk)
    instead of a self-join against a ``max(timestamp)`` subquery.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
    # Stable key for vehicles: prefer `vehicle_id` but fall back to `trip_id`.
    veh_key_expr = func.coalesce(VehiclePosition.vehicle_id, VehiclePosition.trip_id)

    stmt = (
        select(VehiclePosition)
        .where(
            VehiclePosition.timestamp >= cutoff,
            VehiclePosition.is_anomalous.is_(False),
        )
        .order_by(veh_key_expr, VehiclePosition.timestamp.desc())
        .distinct(veh_key_expr)
    )
    if route_id:
        stmt = stmt.where(VehiclePosition.route_id == route_id)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _latest_delays(
    db: AsyncSession,
) -> dict[str, int]:
    """Return latest arrival_delay (seconds) keyed by trip_id.

    All stop-time updates for a trip share one ingest timestamp, so we pick, per
    trip, the row at the latest timestamp preferring a non-null delay at the
    earliest upcoming stop.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    stmt = (
        select(TripUpdate.trip_id, TripUpdate.arrival_delay)
        .where(
            TripUpdate.timestamp >= cutoff,
            TripUpdate.trip_id.is_not(None),
        )
        .order_by(
            TripUpdate.trip_id,
            TripUpdate.timestamp.desc(),
            TripUpdate.arrival_delay.is_(None),  # non-null delays first
            TripUpdate.stop_sequence,
        )
        .distinct(TripUpdate.trip_id)
    )
    result = await db.execute(stmt)
    return {
        trip_id: delay
        for trip_id, delay in result.all()
        if trip_id is not None and delay is not None
    }


def _compute_headways(
    positions: list[VehiclePosition],
) -> dict[str, float]:
    """Estimate headway (minutes) per route from active vehicle count.

    Since all positions share the same ingest timestamp we can't compute
    time gaps. Instead we use vehicle count as a frequency proxy:
      headway ≈ 120 min (assumed round-trip cycle) / active vehicle count
    Examples: 10 vehicles → 12 min (green), 5 → 24 min (orange), 2 → 60 min (red).
    """
    from collections import defaultdict

    route_count: dict[str, int] = defaultdict(int)
    for vp in positions:
        route_count[vp.route_id] += 1

    headways: dict[str, float] = {}
    for route_id, count in route_count.items():
        headways[route_id] = round(120.0 / count, 1) if count >= 2 else 0.0

    return headways


def _enrich(
    vp: VehiclePosition,
    routes: dict,
    stops: dict,
    delays: dict[str, int],
    headways: dict[str, float],
) -> VehiclePositionOut:
    route_info = routes.get(vp.route_id, {})
    stop_info = stops.get(vp.stop_id or "", {})
    delay = delays.get(vp.trip_id or "") if vp.trip_id else None
    late_threshold = 300  # 5 minutes in seconds
    is_late = (delay > late_threshold) if delay is not None else None

    return VehiclePositionOut(
        vehicle_id=vp.vehicle_id,
        vehicle_label=vp.vehicle_label,
        trip_id=vp.trip_id,
        route_id=vp.route_id,
        route_short_name=route_info.get("route_short_name", vp.route_id),
        route_long_name=route_info.get("route_long_name", ""),
        route_color=route_info.get("route_color", "888888"),
        route_type=route_info.get("route_type", ""),
        latitude=vp.latitude,
        longitude=vp.longitude,
        bearing=vp.bearing,
        current_stop_sequence=vp.current_stop_sequence,
        current_status=vp.current_status,
        current_status_label=_VEHICLE_STATUS_LABELS.get(vp.current_status or -1),
        stop_id=vp.stop_id,
        stop_name=stop_info.get("stop_name"),
        occupancy_status=vp.occupancy_status,
        timestamp=vp.timestamp,
        delay_seconds=delay,
        is_late=is_late,
        headway_minutes=headways.get(vp.route_id),
    )


# ── Short-lived response cache ─────────────────────────────────────────────
# The underlying data only changes every poll cycle (~30s), but every browser
# tab polls every ~30s independently. Caching the assembled response for a few
# seconds lets concurrent clients share a single DB computation. Keyed by
# route_id; the key space is bounded by the number of routes (~190).

_CACHE_TTL_SECONDS = 5.0
# route_id comes from the URL path, so the key space is attacker-controlled —
# bound the cache and evict expired entries once it grows past real usage.
_CACHE_MAX_KEYS = 512
_vehicles_cache: dict[str, tuple[float, RealtimeResponse]] = {}
_vehicles_locks: dict[str, asyncio.Lock] = {}


async def _build_vehicles_response(
    db: AsyncSession,
    route_id: str | None,
) -> RealtimeResponse:
    routes, stops = _routes_and_stops()
    positions = await _latest_positions(db, route_id=route_id)
    delays = await _latest_delays(db)
    headways = _compute_headways(positions)

    # Enrich and filter to only vehicles with a valid position for the map.
    enriched = [_enrich(vp, routes, stops, delays, headways) for vp in positions]
    vehicles_with_loc = [v for v in enriched if v.latitude is not None and v.longitude is not None]

    return RealtimeResponse(
        updated_at=datetime.now(tz=timezone.utc),
        vehicles=vehicles_with_loc,
        route_headways=headways,
        total_vehicles=len(enriched),
        vehicles_with_location=len(vehicles_with_loc),
        unique_vehicle_keys=None,
    )


async def _cached_vehicles_response(
    db: AsyncSession,
    route_id: str | None,
) -> RealtimeResponse:
    key = route_id or "__all__"
    hit = _vehicles_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]

    # Single-flight: concurrent misses for the same key wait for one rebuild
    # instead of each running the DISTINCT ON scan.
    lock = _vehicles_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _vehicles_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
        resp = await _build_vehicles_response(db, route_id)
        if len(_vehicles_cache) >= _CACHE_MAX_KEYS:
            _evict_expired()
        _vehicles_cache[key] = (time.monotonic(), resp)
        return resp


def _evict_expired() -> None:
    now = time.monotonic()
    for k in [k for k, (t, _) in _vehicles_cache.items() if now - t >= _CACHE_TTL_SECONDS]:
        _vehicles_cache.pop(k, None)
        lock = _vehicles_locks.get(k)
        if lock is not None and not lock.locked():
            _vehicles_locks.pop(k, None)


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/vehicles", response_model=RealtimeResponse)
async def get_all_vehicles(db: Annotated[AsyncSession, Depends(get_db)]) -> RealtimeResponse:
    return await _cached_vehicles_response(db, None)


@router.get("/vehicles/{route_id}", response_model=RealtimeResponse)
async def get_vehicles_by_route(
    route_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RealtimeResponse:
    return await _cached_vehicles_response(db, route_id)
