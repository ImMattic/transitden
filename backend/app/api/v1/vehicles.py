"""Vehicle drill-down: active vehicles for a time window + per-vehicle trip detail."""
from __future__ import annotations

import asyncio
import time as _time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import bindparam, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.vehicle_position import VehiclePosition
from app.models.stop_arrival import StopArrivalEvent
from app.models.trip_update import TripUpdate
from app.services.gtfs_decoder import load_gtfs_static_data, load_trip_endpoint_sequences
from app.services.gtfs_schedule import load_trip_origin_timepoints, load_trip_stop_sequence

router = APIRouter(prefix="/vehicles", tags=["vehicles"])

_settings = get_settings()
_DENVER = ZoneInfo("America/Denver")


def _validated_range(
    start: datetime | None,
    end: datetime | None,
    default_span: timedelta,
) -> tuple[datetime, datetime]:
    """Fill in defaults and reject inverted or abusively wide time ranges."""
    now = datetime.now(tz=timezone.utc)
    end = end or now
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start or (end - default_span)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    if end - start > timedelta(hours=_settings.vehicles_max_span_hours):
        raise HTTPException(
            status_code=422,
            detail=f"time range too large: max {_settings.vehicles_max_span_hours}h",
        )
    return start, end


# A trip can begin before / end after the requested window.  We scan this much
# extra on each side so each trip's *true* first→last position is captured and
# we can decide whether its start or end lands in the window.  3h comfortably
# covers RTD's longest routes.
_MAX_TRIP_DURATION = timedelta(hours=3)

# {trip_id: (first_stop_id, last_stop_id)} / {trip_id: (min_seq, max_seq)} from
# the static schedule.  Parsing stop_times.txt is expensive, so cache both at
# module level (same pattern as stats.py) and load lazily on first use.
_TRIP_ENDPOINT_SEQUENCES: dict[str, tuple[int, int]] | None = None
_TRIP_ENDPOINT_STOPS: dict[str, tuple[str | None, str | None]] | None = None


def _load_trip_endpoints() -> tuple[
    dict[str, tuple[int, int]], dict[str, tuple[str | None, str | None]]
]:
    global _TRIP_ENDPOINT_SEQUENCES, _TRIP_ENDPOINT_STOPS
    if _TRIP_ENDPOINT_STOPS is None:
        _TRIP_ENDPOINT_SEQUENCES, _TRIP_ENDPOINT_STOPS = load_trip_endpoint_sequences()
    return _TRIP_ENDPOINT_SEQUENCES or {}, _TRIP_ENDPOINT_STOPS


def _trip_endpoint_stops() -> dict[str, tuple[str | None, str | None]]:
    _, stop_ids = _load_trip_endpoints()
    return stop_ids


def _trip_endpoint_sequences() -> dict[str, tuple[int, int]]:
    sequences, _ = _load_trip_endpoints()
    return sequences


# One CTE query that:
#   1. Aggregates vehicle_positions in the scan window into one row per
#      (vehicle_label, trip_id) using TimescaleDB LAST() — replaces the old
#      DISTINCT ON + separate GROUP BY double-scan.
#   2. Joins stop_arrival_events (scoped to qualified trips) for arrival counts
#      and the furthest stop_sequence each trip was seen at.
#   3. Applies the time-window and quality filters entirely in SQL.
#
# The {route_clause} placeholder is either empty or a route_id restriction.
# The {window_clause} placeholder bounds a trip to the requested window: by
# default a trip qualifies if its start OR end lands inside [start, end];  in
# strict mode both its start AND end must land inside it (trips that begin
# before or run past the window are dropped entirely).
#
# Paging happens in Python rather than here.  Trip status and mode depend on the
# static schedule, which SQL has no view of, and filtering *after* a SQL page
# would drop matches sitting on later pages.  Materialising the window's trips —
# one aggregated row each, not one per position — is cheap next to the scan that
# produced them, and it is also what lets the response carry honest facet counts.
_ACTIVE_VEHICLES_SQL = """
WITH agg AS (
    SELECT
        vehicle_label,
        trip_id,
        MIN(timestamp)                    AS start_time,
        MAX(timestamp)                    AS end_time,
        COUNT(*)                          AS observation_count,
        LAST(route_id,         timestamp) AS route_id,
        LAST(vehicle_id,       timestamp) AS vehicle_id,
        LAST(latitude,         timestamp) AS latitude,
        LAST(longitude,        timestamp) AS longitude,
        LAST(occupancy_status, timestamp) AS occupancy_status
    FROM vehicle_positions
    WHERE timestamp >= :scan_start
      AND timestamp <= :scan_end
      {route_clause}
    GROUP BY vehicle_label, trip_id
    HAVING COUNT(*) >= 10
),
arrivals AS (
    SELECT
        sae.trip_id,
        COUNT(*)                 AS arrival_count,
        MAX(sae.stop_sequence)   AS max_stop_sequence
    FROM stop_arrival_events sae
    WHERE sae.timestamp >= :scan_start
      AND sae.timestamp <= :scan_end
      AND sae.trip_id IN (SELECT trip_id FROM agg WHERE trip_id IS NOT NULL)
    GROUP BY sae.trip_id
),
filtered AS (
    SELECT
        a.*,
        COALESCE(ar.arrival_count, 0) AS stop_arrival_count,
        ar.max_stop_sequence          AS max_stop_sequence
    FROM agg a
    LEFT JOIN arrivals ar ON ar.trip_id = a.trip_id
    WHERE (
        {window_clause}
    )
      AND COALESCE(ar.arrival_count, 0) > 1
)
SELECT * FROM filtered
ORDER BY start_time ASC, route_id ASC NULLS LAST
LIMIT :scan_cap
"""

# Time-window clauses substituted into {window_clause} above.
_WINDOW_CLAUSE_OVERLAP = (
    "(a.start_time >= :start AND a.start_time <= :end)"
    " OR (a.end_time >= :start AND a.end_time <= :end)"
)
_WINDOW_CLAUSE_STRICT = "a.start_time >= :start AND a.end_time <= :end"

# Ceiling on trips materialised for one request.  A full day of RTD service is
# roughly 10k trips, so this leaves generous headroom while still bounding what
# a single request can pull into memory.
_SCAN_ROW_CAP = 40_000

# A trip counts as still running while its newest position is this recent.  The
# realtime feed is built from a 60s window (api/v1/realtime), so anything inside
# that window is on the live map too; the extra margin absorbs the lag between
# this query and the browser's own poll of the feed.
_LIVE_TRIP_WINDOW = timedelta(seconds=90)

_RAIL_ROUTE_TYPES = {"0", "1", "2"}

_TRIP_STATUSES = {"in_progress", "complete", "incomplete"}
_MODES = {"rail", "bus", "other"}


def _mode_of(route_type: str | None) -> str:
    """GTFS route_type → the rail / bus / other split the filter menus offer."""
    if route_type in _RAIL_ROUTE_TYPES:
        return "rail"
    return "bus" if route_type == "3" else "other"


def _split_csv(raw: str | None) -> list[str]:
    """Comma-separated query value → list, dropping blanks."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _validated_choice(raw: str | None, allowed: set[str], name: str) -> set[str]:
    values = set(_split_csv(raw))
    unknown = values - allowed
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown {name}: {', '.join(sorted(unknown))}",
        )
    return values


# Computed windows, per worker process.  One entry is every trip in a window
# (~10 MB at the 72 h ceiling), so the count is capped rather than the age.
_WINDOW_CACHE_MAX = 8
_STABLE_MARGIN = timedelta(minutes=10)

_window_cache: OrderedDict[tuple, tuple[float, list[dict], dict]] = OrderedDict()
_window_locks: dict[tuple, asyncio.Lock] = {}


def _window_ttl(end: datetime, now: datetime) -> float:
    """Seconds a window ending at ``end`` may be reused.

    Trips are read out to ``_MAX_TRIP_DURATION`` past ``end``, so until that has
    elapsed a trip can still be growing and the window has to stay fresh.
    """
    if end + _MAX_TRIP_DURATION + _STABLE_MARGIN < now:
        return _settings.active_vehicles_cache_stable_seconds
    return _settings.active_vehicles_cache_live_seconds


def _cache_get(key: tuple) -> tuple[list[dict], dict] | None:
    entry = _window_cache.get(key)
    if entry is None:
        return None
    expires_at, trips, facets = entry
    if expires_at <= _time.monotonic():
        del _window_cache[key]
        return None
    _window_cache.move_to_end(key)
    return trips, facets


def _cache_put(key: tuple, ttl: float, trips: list[dict], facets: dict) -> None:
    _window_cache[key] = (_time.monotonic() + ttl, trips, facets)
    _window_cache.move_to_end(key)
    while len(_window_cache) > _WINDOW_CACHE_MAX:
        _window_cache.popitem(last=False)


async def _cached_window(
    key: tuple,
    ttl: float,
    build: Callable[[], Awaitable[tuple[list[dict], dict]]],
) -> tuple[list[dict], dict]:
    """``build()``'s result, reused for ``ttl`` seconds.

    Concurrent callers for the same window share one build: paging quickly, or
    two people opening the same range, would otherwise each pay for the scan.
    A failed build is not cached.
    """
    if ttl <= 0:
        return await build()
    if (hit := _cache_get(key)) is not None:
        return hit
    lock = _window_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            if (hit := _cache_get(key)) is not None:
                return hit
            trips, facets = await build()
            _cache_put(key, ttl, trips, facets)
            return trips, facets
    finally:
        if not lock.locked():
            _window_locks.pop(key, None)


async def _build_window(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    wanted_routes: set[str],
    strict: bool,
) -> tuple[list[dict], dict]:
    """Every trip in [start, end] with its delay stats, plus the window's facets."""
    scan_start = start - _MAX_TRIP_DURATION
    scan_end = end + _MAX_TRIP_DURATION

    # Route is the one filter worth pushing into SQL: it is by far the most
    # selective, and it shrinks the aggregate before it crosses the wire.
    params: dict = {
        "scan_start": scan_start,
        "scan_end": scan_end,
        "start": start,
        "end": end,
        "scan_cap": _SCAN_ROW_CAP,
    }
    if len(wanted_routes) == 1:
        route_clause = "AND route_id = :route_id"
        params["route_id"] = next(iter(wanted_routes))
    elif wanted_routes:
        route_clause = "AND route_id IN :route_id_list"
        params["route_id_list"] = tuple(sorted(wanted_routes))
    else:
        route_clause = ""

    window_clause = _WINDOW_CLAUSE_STRICT if strict else _WINDOW_CLAUSE_OVERLAP
    sql = text(
        _ACTIVE_VEHICLES_SQL.format(
            route_clause=route_clause,
            window_clause=window_clause,
        )
    )
    if "route_id_list" in params:
        sql = sql.bindparams(bindparam("route_id_list", expanding=True))

    rows = (await db.execute(sql, params)).mappings().all()

    routes_static, stops_static = load_gtfs_static_data()
    endpoint_stops = _trip_endpoint_stops()
    endpoint_sequences = _trip_endpoint_sequences()
    live_cutoff = datetime.now(tz=timezone.utc) - _LIVE_TRIP_WINDOW

    trips = [
        _describe_trip(
            r,
            routes_static,
            stops_static,
            endpoint_stops,
            endpoint_sequences,
            live_cutoff,
        )
        for r in rows
    ]

    # Avg delay / on-time % are filterable, so — unlike last_delay_seconds below
    # — they have to be known for every trip in the window *before* filtering
    # and paging run, not just for the page being returned.
    window_trip_ids = {t["trip_id"] for t in trips if t["trip_id"]}
    stats_map = await _trip_delay_stats(db, scan_start, scan_end, window_trip_ids)
    for t in trips:
        stats = stats_map.get(t["trip_id"] or "")
        t["avg_delay_seconds"] = stats[0] if stats else None
        t["on_time_pct"] = stats[1] if stats else None

    # Route is the one filter the SQL already applied, so the facets it feeds
    # describe only the selected routes.  Say so, rather than letting a menu
    # read those counts as if they covered the window.
    facets = _build_facets(trips, route_scoped=bool(route_clause))
    return trips, facets


@router.get("/active")
async def get_active_vehicles(
    db: Annotated[AsyncSession, Depends(get_db)],
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    vehicle_labels: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    occupancy: Annotated[str | None, Query()] = None,
    min_duration_minutes: Annotated[float | None, Query(ge=0)] = None,
    max_duration_minutes: Annotated[float | None, Query(ge=0)] = None,
    min_avg_delay_seconds: Annotated[float | None, Query()] = None,
    max_avg_delay_seconds: Annotated[float | None, Query()] = None,
    min_on_time_pct: Annotated[float | None, Query(ge=0, le=100)] = None,
    max_on_time_pct: Annotated[float | None, Query(ge=0, le=100)] = None,
    strict: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 15,
    offset: Annotated[int, Query(ge=0, le=5_000)] = 0,
) -> dict:
    """Trips whose start or end falls within [start, end].

    A *trip* is one ``(vehicle_label, trip_id)`` leg.  We scan a window padded
    by ``_MAX_TRIP_DURATION`` on each side so a trip's full extent
    (first→last position) is captured even when it begins before / ends after
    the requested window, then keep only trips whose start or end timestamp is
    actually inside ``[start, end]``.

    When ``strict`` is true, only trips that lie *entirely* within
    ``[start, end]`` are kept — both start and end must fall inside the window,
    so a trip that begins before or runs past the window is excluded.

    Quality filter (same criteria as before) is applied in SQL:
    observation_count >= 10 AND stop_arrival_count > 1.

    Everything after that is a *filter*, and every one of them is applied before
    paging, so page 2 means the second page of matches rather than the second
    page of the window with the filter re-run on it.  The comma-separated
    parameters (``route_ids``, ``modes``, ``vehicle_labels``, ``status``,
    ``occupancy``) each OR within themselves and AND with the others; an omitted
    one doesn't narrow anything.  ``facets`` in the response counts the window
    *before* those filters, so the menu's per-option numbers hold still while
    someone is choosing — with one exception it flags as ``route_scoped``, since
    the route restriction is the only filter that runs in SQL.
    """
    end_given = end is not None
    start, end = _validated_range(start, end, default_span=timedelta(hours=1))

    wanted_routes = set(_split_csv(route_ids))
    if route_id:
        wanted_routes.add(route_id)
    wanted_modes = _validated_choice(modes, _MODES, "modes")
    wanted_labels = set(_split_csv(vehicle_labels))
    wanted_statuses = _validated_choice(status, _TRIP_STATUSES, "status")
    wanted_occupancy = set(_split_csv(occupancy))

    now = datetime.now(tz=timezone.utc)
    # A window with no explicit end is anchored to "now", so it never repeats.
    ttl = _window_ttl(end, now) if end_given else 0
    key = (start, end, strict, tuple(sorted(wanted_routes)))
    trips, facets = await _cached_window(
        key, ttl, lambda: _build_window(db, start, end, wanted_routes, strict)
    )
    scan_start = start - _MAX_TRIP_DURATION
    scan_end = end + _MAX_TRIP_DURATION

    matches = [
        t
        for t in trips
        if _matches_filters(
            t,
            routes=wanted_routes,
            modes=wanted_modes,
            labels=wanted_labels,
            statuses=wanted_statuses,
            occupancy=wanted_occupancy,
            min_duration_minutes=min_duration_minutes,
            max_duration_minutes=max_duration_minutes,
            min_avg_delay_seconds=min_avg_delay_seconds,
            max_avg_delay_seconds=max_avg_delay_seconds,
            min_on_time_pct=min_on_time_pct,
            max_on_time_pct=max_on_time_pct,
        )
    ]

    page = [dict(t) for t in matches[offset:offset + limit]]

    # last_delay_seconds isn't filterable, so it's the one field still looked
    # up only for the rows actually being returned.
    page_trip_routes = {t["trip_id"]: t["route_id"] for t in page if t["trip_id"]}
    delay_start, delay_end = _delay_window(page, scan_start, scan_end)
    delay_map = await _delay_map(db, delay_start, delay_end, page_trip_routes)
    for t in page:
        t["last_delay_seconds"] = delay_map.get(t["trip_id"] or "")

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "vehicle_count": len(matches),
        "window_count": len(trips),
        "vehicles": page,
        "facets": facets,
    }


def _describe_trip(
    row,
    routes_static: dict,
    stops_static: dict,
    endpoint_stops: dict[str, tuple[str | None, str | None]],
    endpoint_sequences: dict[str, tuple[int, int]],
    live_cutoff: datetime,
) -> dict:
    """One aggregated SQL row → the trip object the API returns."""
    trip_id = row["trip_id"] or ""
    first_sid, last_sid = endpoint_stops.get(trip_id, (None, None))
    route = routes_static.get(row["route_id"], {})

    # True once a geofenced arrival exists at the trip's *last* stop_sequence —
    # i.e. the vehicle actually reached the terminus, not just any stop sharing
    # its stop_id (loop routes can revisit the same physical stop).  Since the
    # terminus is by definition the highest sequence, the furthest arrival
    # reaching it is the same test.  Defaults to True when we have no static
    # schedule for the trip at all, so an unrelated data gap doesn't read as an
    # incident.
    terminus_seq = endpoint_sequences.get(trip_id)
    if terminus_seq is None:
        reached_terminus = True
    else:
        max_seq = row["max_stop_sequence"]
        reached_terminus = max_seq is not None and max_seq >= terminus_seq[1]

    start_time: datetime = row["start_time"]
    end_time: datetime = row["end_time"]
    if end_time.tzinfo is None:
        end_time_utc = end_time.replace(tzinfo=timezone.utc)
    else:
        end_time_utc = end_time
    in_progress = end_time_utc >= live_cutoff

    return {
        "vehicle_label": row["vehicle_label"],
        "vehicle_id": row["vehicle_id"],
        "trip_id": row["trip_id"],
        "route_id": row["route_id"],
        "route_short_name": route.get("route_short_name"),
        "route_color": route.get("route_color"),
        "route_type": route.get("route_type"),
        "mode": _mode_of(route.get("route_type")),
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_minutes": round((end_time - start_time).total_seconds() / 60, 1),
        "start_stop_name": (
            stops_static.get(first_sid, {}).get("stop_name") if first_sid else None
        ),
        "end_stop_name": (
            stops_static.get(last_sid, {}).get("stop_name") if last_sid else None
        ),
        "reached_terminus": reached_terminus,
        "in_progress": in_progress,
        "trip_status": (
            "in_progress" if in_progress else ("complete" if reached_terminus else "incomplete")
        ),
        "last_latitude": row["latitude"],
        "last_longitude": row["longitude"],
        "last_occupancy_status": row["occupancy_status"],
        "last_delay_seconds": None,
        "avg_delay_seconds": None,
        "on_time_pct": None,
        "observation_count": row["observation_count"],
        "stop_arrival_count": row["stop_arrival_count"],
    }


def _matches_filters(
    trip: dict,
    *,
    routes: set[str],
    modes: set[str],
    labels: set[str],
    statuses: set[str],
    occupancy: set[str],
    min_duration_minutes: float | None,
    max_duration_minutes: float | None,
    min_avg_delay_seconds: float | None = None,
    max_avg_delay_seconds: float | None = None,
    min_on_time_pct: float | None = None,
    max_on_time_pct: float | None = None,
) -> bool:
    """AND across groups, OR inside each — an empty group never narrows."""
    if routes and trip["route_id"] not in routes:
        return False
    if modes and trip["mode"] not in modes:
        return False
    if labels and (trip["vehicle_label"] or "") not in labels:
        return False
    if statuses and trip["trip_status"] not in statuses:
        return False
    if occupancy and (trip["last_occupancy_status"] or "UNKNOWN") not in occupancy:
        return False
    duration = trip["duration_minutes"]
    if min_duration_minutes is not None and duration < min_duration_minutes:
        return False
    if max_duration_minutes is not None and duration > max_duration_minutes:
        return False
    # Both bounds require an actual reading — a trip with no observed arrivals
    # (avg_delay_seconds/on_time_pct null) can't be shown to satisfy either one.
    if min_avg_delay_seconds is not None or max_avg_delay_seconds is not None:
        delay = trip["avg_delay_seconds"]
        if delay is None:
            return False
        if min_avg_delay_seconds is not None and delay < min_avg_delay_seconds:
            return False
        if max_avg_delay_seconds is not None and delay > max_avg_delay_seconds:
            return False
    if min_on_time_pct is not None or max_on_time_pct is not None:
        pct = trip["on_time_pct"]
        if pct is None:
            return False
        if min_on_time_pct is not None and pct < min_on_time_pct:
            return False
        if max_on_time_pct is not None and pct > max_on_time_pct:
            return False
    return True


def _build_facets(trips: list[dict], *, route_scoped: bool = False) -> dict:
    """Per-option counts for the filter menu, taken before any filter is applied.

    The routes and fleet numbers that actually ran in this window are the only
    ones worth offering, and a count beside each says whether ticking it is
    worth the trip.

    ``route_scoped`` marks the one case where these numbers do *not* describe
    the whole window: the route restriction runs in SQL (it has an index behind
    it, and skipping it would turn every single-route request into a full scan),
    so when one is set the counts cover the chosen routes only.
    """
    routes: dict[str, dict] = {}
    vehicles: dict[str, dict] = {}
    statuses = {"in_progress": 0, "complete": 0, "incomplete": 0}
    modes = {"rail": 0, "bus": 0, "other": 0}
    occupancy: dict[str, int] = {}
    longest = 0.0

    for t in trips:
        statuses[t["trip_status"]] += 1
        modes[t["mode"]] += 1
        occ = t["last_occupancy_status"] or "UNKNOWN"
        occupancy[occ] = occupancy.get(occ, 0) + 1
        longest = max(longest, t["duration_minutes"])

        rid = t["route_id"]
        route = routes.get(rid)
        if route is None:
            routes[rid] = {
                "route_id": rid,
                "route_short_name": t["route_short_name"],
                "route_color": t["route_color"],
                "mode": t["mode"],
                "trip_count": 1,
            }
        else:
            route["trip_count"] += 1

        label = t["vehicle_label"]
        if not label:
            continue
        vehicle = vehicles.get(label)
        if vehicle is None:
            vehicles[label] = {
                "vehicle_label": label,
                "route_short_names": [t["route_short_name"]] if t["route_short_name"] else [],
                "route_color": t["route_color"],
                "mode": t["mode"],
                "trip_count": 1,
            }
        else:
            vehicle["trip_count"] += 1
            short = t["route_short_name"]
            if short and short not in vehicle["route_short_names"]:
                vehicle["route_short_names"].append(short)

    def _sort_key(name: str | None) -> tuple[int, int, str]:
        # Numeric route names sort as numbers ("15" before "120"), lettered rail
        # lines after them, so the list reads the way the system is signed.
        text_name = name or ""
        return (0, int(text_name), "") if text_name.isdigit() else (1, 0, text_name)

    return {
        "trip_count": len(trips),
        "route_scoped": route_scoped,
        "routes": sorted(routes.values(), key=lambda r: _sort_key(r["route_short_name"])),
        "vehicles": sorted(vehicles.values(), key=lambda v: _sort_key(v["vehicle_label"])),
        "statuses": statuses,
        "modes": modes,
        "occupancy": occupancy,
        "max_duration_minutes": round(longest, 1),
    }


@router.get("/{vehicle_label}/trip")
async def get_vehicle_trip(
    vehicle_label: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    trip_id: Annotated[str | None, Query()] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
) -> dict:
    """Stop arrival timeline and position track for a specific vehicle's trip."""
    start, end = _validated_range(start, end, default_span=timedelta(hours=6))

    pos_filter = [
        VehiclePosition.vehicle_label == vehicle_label,
        VehiclePosition.timestamp >= start,
        VehiclePosition.timestamp <= end,
    ]
    if trip_id:
        pos_filter.append(VehiclePosition.trip_id == trip_id)

    pos_stmt = (
        select(
            VehiclePosition.vehicle_id,
            VehiclePosition.trip_id,
            VehiclePosition.route_id,
            VehiclePosition.latitude,
            VehiclePosition.longitude,
            VehiclePosition.bearing,
            VehiclePosition.current_status,
            VehiclePosition.occupancy_status,
            VehiclePosition.timestamp,
        )
        .where(*pos_filter)
        .order_by(VehiclePosition.timestamp.asc())
    )

    pos_rows = (await db.execute(pos_stmt)).all()

    if not pos_rows:
        return {
            "vehicle_label": vehicle_label,
            "vehicle_id": None,
            "trip_id": trip_id,
            "route_id": None,
            "route_short_name": None,
            "route_long_name": None,
            "route_color": None,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "stops": [],
            "scheduled_stop_count": 0,
            "observed_stop_count": 0,
            "positions": [],
            "avg_delay_seconds": None,
            "on_time_pct": None,
            "observation_count": 0,
        }

    last_row = pos_rows[-1]
    resolved_trip_id = trip_id or last_row.trip_id
    resolved_route_id = last_row.route_id
    vehicle_id = last_row.vehicle_id

    routes_static, stops_static = load_gtfs_static_data()
    route_info = routes_static.get(resolved_route_id, {})

    # Occupancy timeline from the position snapshots, used to label each stop
    # with the occupancy reported closest in time to the observed arrival.
    occ_timeline = [
        (r.timestamp, r.occupancy_status)
        for r in pos_rows
        if r.occupancy_status is not None
    ]

    def _occupancy_at(t: datetime) -> str | None:
        if not occ_timeline:
            return None
        return min(occ_timeline, key=lambda pair: abs((pair[0] - t).total_seconds()))[1]

    stops_list: list[dict] = []
    if resolved_trip_id:
        arrival_stmt = (
            select(StopArrivalEvent)
            .where(
                StopArrivalEvent.trip_id == resolved_trip_id,
                StopArrivalEvent.timestamp >= start,
                StopArrivalEvent.timestamp <= end,
            )
            .order_by(StopArrivalEvent.stop_sequence.asc())
        )
        # Geofenced arrivals, keyed by stop_sequence — the subset of stops we
        # actually observed the vehicle reach.
        observed: dict[int, StopArrivalEvent] = {
            ev.stop_sequence: ev
            for ev in (await db.execute(arrival_stmt)).scalars().all()
        }

        # The full RTD schedule for this trip: every stop, origin → destination.
        schedule = load_trip_stop_sequence(resolved_trip_id)

        # The origin is timed by departure, not arrival (services/ontime.py), so
        # label it as such rather than letting the UI imply the bus "arrived" at
        # the stop it started from.
        origin = load_trip_origin_timepoints().get(resolved_trip_id)
        origin_seq = origin[0] if origin else None

        def _event_type(seq: int) -> str:
            return "departure" if seq == origin_seq else "arrival"

        if schedule:
            # Anchor the GTFS service day so stops we never geofenced still get
            # an absolute scheduled time.
            anchor = _service_day_anchor(schedule, observed, pos_rows)
            scheduled_seqs = {s["stop_sequence"] for s in schedule}
            for s in schedule:
                seq = s["stop_sequence"]
                ev = observed.get(seq)
                if ev is not None:
                    scheduled_iso = ev.scheduled_time.isoformat()
                elif anchor is not None and s["arrival_secs"] is not None:
                    scheduled_iso = (
                        anchor + timedelta(seconds=s["arrival_secs"])
                    ).isoformat()
                else:
                    scheduled_iso = None
                stops_list.append(
                    {
                        "stop_id": s["stop_id"],
                        "stop_name": s["stop_name"],
                        "stop_lat": s["stop_lat"],
                        "stop_lon": s["stop_lon"],
                        "stop_sequence": seq,
                        "stop_headsign": s["stop_headsign"],
                        "is_timepoint": s["is_timepoint"],
                        "pickup_type": s["pickup_type"],
                        "drop_off_type": s["drop_off_type"],
                        "scheduled_time": scheduled_iso,
                        "observed": ev is not None,
                        "event_type": _event_type(seq) if ev else None,
                        "actual_time": ev.actual_time.isoformat() if ev else None,
                        "delay_seconds": ev.delay_seconds if ev else None,
                        "occupancy_status": _occupancy_at(ev.actual_time) if ev else None,
                        "actual_lat": ev.actual_lat if ev else None,
                        "actual_lon": ev.actual_lon if ev else None,
                        "actual_bearing": ev.actual_bearing if ev else None,
                        "detection_method": ev.detection_method if ev else None,
                    }
                )

            # Keep any observed arrival whose stop_sequence isn't in the current
            # schedule (GTFS bundle drift) rather than silently dropping it.
            for seq, ev in sorted(observed.items()):
                if seq in scheduled_seqs:
                    continue
                stop_info = stops_static.get(ev.stop_id, {})
                stops_list.append(
                    {
                        "stop_id": ev.stop_id,
                        "stop_name": stop_info.get("stop_name"),
                        "stop_lat": stop_info.get("stop_lat"),
                        "stop_lon": stop_info.get("stop_lon"),
                        "stop_sequence": ev.stop_sequence,
                        "stop_headsign": None,
                        "is_timepoint": True,
                        "pickup_type": "0",
                        "drop_off_type": "0",
                        "scheduled_time": ev.scheduled_time.isoformat(),
                        "observed": True,
                        "event_type": _event_type(ev.stop_sequence),
                        "actual_time": ev.actual_time.isoformat(),
                        "delay_seconds": ev.delay_seconds,
                        "occupancy_status": _occupancy_at(ev.actual_time),
                        "actual_lat": ev.actual_lat,
                        "actual_lon": ev.actual_lon,
                        "actual_bearing": ev.actual_bearing,
                        "detection_method": ev.detection_method,
                    }
                )
            stops_list.sort(key=lambda s: s["stop_sequence"])
        else:
            # Trip absent from the bundled static schedule — fall back to the
            # geofenced arrivals alone (pre-full-timeline behaviour).
            for seq, ev in sorted(observed.items()):
                stop_info = stops_static.get(ev.stop_id, {})
                stops_list.append(
                    {
                        "stop_id": ev.stop_id,
                        "stop_name": stop_info.get("stop_name"),
                        "stop_lat": stop_info.get("stop_lat"),
                        "stop_lon": stop_info.get("stop_lon"),
                        "stop_sequence": ev.stop_sequence,
                        "stop_headsign": None,
                        "is_timepoint": True,
                        "pickup_type": "0",
                        "drop_off_type": "0",
                        "scheduled_time": ev.scheduled_time.isoformat(),
                        "observed": True,
                        "event_type": _event_type(ev.stop_sequence),
                        "actual_time": ev.actual_time.isoformat(),
                        "delay_seconds": ev.delay_seconds,
                        "occupancy_status": _occupancy_at(ev.actual_time),
                        "actual_lat": ev.actual_lat,
                        "actual_lon": ev.actual_lon,
                        "actual_bearing": ev.actual_bearing,
                        "detection_method": ev.detection_method,
                    }
                )

    avg_delay: float | None = None
    on_time_pct: float | None = None
    observed_delays = [
        s["delay_seconds"] for s in stops_list if s["observed"] and s["delay_seconds"] is not None
    ]
    if observed_delays:
        avg_delay = sum(observed_delays) / len(observed_delays)
        on_time_count = sum(1 for d in observed_delays if abs(d) <= 300)
        on_time_pct = round(on_time_count / len(observed_delays) * 100, 1)

    positions = [
        {
            "latitude": r.latitude,
            "longitude": r.longitude,
            "bearing": r.bearing,
            "timestamp": r.timestamp.isoformat(),
            "current_status": r.current_status,
            "occupancy_status": r.occupancy_status,
        }
        for r in pos_rows
        if r.latitude is not None and r.longitude is not None
    ]

    return {
        "vehicle_label": vehicle_label,
        "vehicle_id": vehicle_id,
        "trip_id": resolved_trip_id,
        "route_id": resolved_route_id,
        "route_short_name": route_info.get("route_short_name"),
        "route_long_name": route_info.get("route_long_name"),
        "route_color": route_info.get("route_color"),
        "route_type": route_info.get("route_type"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "stops": stops_list,
        "scheduled_stop_count": len(stops_list),
        "observed_stop_count": sum(1 for s in stops_list if s["observed"]),
        "positions": positions,
        "avg_delay_seconds": avg_delay,
        "on_time_pct": on_time_pct,
        "observation_count": len(pos_rows),
    }


def _service_day_anchor(
    schedule: tuple[dict, ...],
    observed: dict[int, StopArrivalEvent],
    pos_rows: list,
) -> datetime | None:
    """UTC instant of this trip's GTFS service-day midnight (America/Denver).

    Lets us put an absolute clock on scheduled stops the vehicle was never
    geofenced at.  Prefer an observed arrival (its ``scheduled_time`` minus the
    stop's seconds-since-midnight offset is exact); otherwise infer the service
    date from the position track, testing the day before / after as well so a
    trip that runs past midnight still resolves.
    """
    secs_by_seq = {s["stop_sequence"]: s["arrival_secs"] for s in schedule}

    for seq, ev in observed.items():
        offset = secs_by_seq.get(seq)
        if offset is not None:
            return ev.scheduled_time - timedelta(seconds=offset)

    if not pos_rows or not schedule:
        return None
    first_offset = schedule[0]["arrival_secs"]
    if first_offset is None:
        return None
    track_start = pos_rows[0].timestamp
    if track_start.tzinfo is None:
        track_start = track_start.replace(tzinfo=timezone.utc)
    local_date = track_start.astimezone(_DENVER).date()

    best: datetime | None = None
    best_gap: float | None = None
    for delta_days in (0, -1, 1):
        midnight = datetime.combine(
            local_date + timedelta(days=delta_days), time(0, 0), tzinfo=_DENVER
        ).astimezone(timezone.utc)
        gap = abs(
            ((midnight + timedelta(seconds=first_offset)) - track_start).total_seconds()
        )
        if best_gap is None or gap < best_gap:
            best_gap, best = gap, midnight
    return best


_DELAY_WINDOW_MARGIN = timedelta(minutes=5)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _delay_window(
    page: list[dict], floor: datetime, ceil: datetime
) -> tuple[datetime, datetime]:
    """The span the page's own trips cover, clamped to the scan window.

    The scan window is padded 3h each side to catch a trip's full extent, but
    the page's trips only need their own first-to-last position.
    """
    if not page:
        return floor, ceil
    first = min(_aware(datetime.fromisoformat(t["start_time"])) for t in page)
    last = max(_aware(datetime.fromisoformat(t["end_time"])) for t in page)
    return (
        max(first - _DELAY_WINDOW_MARGIN, floor),
        min(last + _DELAY_WINDOW_MARGIN, ceil),
    )


async def _delay_map(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    trip_routes: dict[str, str | None],
) -> dict[str, int]:
    if not trip_routes:
        return {}
    conditions = [
        TripUpdate.trip_id.in_(set(trip_routes)),
        TripUpdate.timestamp >= start,
        TripUpdate.timestamp <= end,
        TripUpdate.arrival_delay.is_not(None),
    ]
    # Chunks older than a day are compressed and segmented by route_id, and the
    # trip_id index only exists on uncompressed ones. Without a route predicate a
    # trip_id lookup decompresses every route's rows for the whole window, which
    # blew the statement timeout on yesterday's data.
    if all(trip_routes.values()):
        conditions.append(TripUpdate.route_id.in_(set(trip_routes.values())))
    stmt = (
        select(TripUpdate.trip_id, TripUpdate.arrival_delay)
        .where(*conditions)
        .order_by(TripUpdate.trip_id, TripUpdate.timestamp.desc())
        .distinct(TripUpdate.trip_id)
    )
    result = await db.execute(stmt)
    return {tid: delay for tid, delay in result.all() if tid is not None}


async def _trip_delay_stats(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    trip_ids: set[str],
) -> dict[str, tuple[float, float]]:
    """Per-trip (avg_delay_seconds, on_time_pct) for the Trip Explorer table.

    Reads ``stop_arrival_events`` — the same geofenced arrivals the on-time
    dashboard stats are built from — rather than ``_delay_map`` above, which is
    only the single latest ``trip_updates.arrival_delay`` reading. Like
    ``_delay_map``, only looked up for the page actually being returned.
    """
    if not trip_ids:
        return {}
    on_time = case(
        (func.abs(StopArrivalEvent.delay_seconds) <= _settings.ontime_threshold_seconds, 1.0),
        else_=0.0,
    )
    stmt = (
        select(
            StopArrivalEvent.trip_id,
            func.avg(StopArrivalEvent.delay_seconds),
            func.avg(on_time) * 100,
        )
        .where(
            StopArrivalEvent.trip_id.in_(trip_ids),
            StopArrivalEvent.timestamp >= start,
            StopArrivalEvent.timestamp <= end,
        )
        .group_by(StopArrivalEvent.trip_id)
    )
    result = await db.execute(stmt)
    return {
        tid: (round(avg_delay, 1), round(on_time_pct, 1))
        for tid, avg_delay, on_time_pct in result.all()
        if tid is not None
    }
