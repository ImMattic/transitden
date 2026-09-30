"""Single-trip diagnostic export — everything behind one trip page, as one JSON file.

The trip page shows a *derived* view: anomalous fixes dropped, arrivals already
decided, the schedule already joined in. That's the right thing to show a rider
and the wrong thing to debug from, because the question is usually "why did the
detector decide that?" This bundle carries the raw inputs next to the derived
output so the decision can be re-traced offline:

- ``timeline`` — exactly what the page rendered (same function, same window)
- ``raw.vehicle_positions`` — every fix of this vehicle in a padded window,
  *including* rows flagged ``is_anomalous``, plus fixes from any other vehicle
  that reported the same trip_id (the signature of a trip_id alias)
- ``raw.stop_arrival_events`` / ``raw.trip_updates`` — the stored rows, unedited
- ``diagnostics.fixes`` — per fix: gap and implied speed since the previous one,
  and distance to the nearest timepoint vs. the geofence radius in force
- ``schedule`` — the trip's full stop list, its timepoints, which of those the
  detector actually uses (``load_trip_shape_dist_schedule`` drops timepoints
  that sit too close together), and its block siblings
- ``diagnostics.competing_slots`` — other trips scheduled at the same timepoints
  within ±15 min, and whether each was live in the feed — the inputs to the
  misassignment guard in ``services/ontime.py``
- ``config`` / ``gtfs_feed`` / ``build`` — the thresholds, static-feed health
  and code version in force, since a row is only as right as the rule that
  wrote it

Static-data lookups never fail the export: each is attempted separately and a
failure lands in ``errors`` instead, since a broken static feed is precisely
one of the things this file exists to diagnose.
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.vehicles import _validated_range, get_vehicle_trip
from app.config import get_settings
from app.database import get_db
from app.models.stop_arrival import StopArrivalEvent
from app.models.trip_update import TripUpdate
from app.models.vehicle_position import VehiclePosition
from app.services import gtfs_health
from app.services.gtfs_decoder import TRANSIT_FOLDERS, resolve_gtfs_static_root
from app.services.gtfs_schedule import (
    load_stop_arrivals_index,
    load_trip_shape_dist_schedule,
    load_trip_stop_schedule,
    load_trip_stop_sequence,
)
from app.services.ontime import (
    _haversine_m,
    _radius_for,
    _terminus_fallback_radius_for,
    is_rail_route,
)

router = APIRouter(prefix="/export", tags=["export"])

_settings = get_settings()

EXPORT_VERSION = 1

# trip_updates repeats every stop's prediction on every poll, so a long trip can
# run to tens of thousands of rows. The cap keeps the file openable; the bundle
# says when it was hit.
_TRIP_UPDATE_ROW_CAP = 20_000

# Competing-trip window for the misassignment view. Wider than the guard's own
# margin on purpose, so near-misses the guard *didn't* act on show up too.
_COMPETING_SLOT_WINDOW_S = 15 * 60

# Settings that shape what gets written to stop_arrival_events or shown on the
# trip page. An allowlist, not a dump: config also holds the DB password, the
# ntfy topic and the /sim password.
_CONFIG_PREFIXES = (
    "arrival_",
    "ontime_",
    "origin_departure_",
    "trip_signal_gap_",
    "polling_interval_",
    "vehicles_max_span_",
)

T = TypeVar("T")


@router.get("/trip")
async def export_trip(
    db: Annotated[AsyncSession, Depends(get_db)],
    vehicle_label: Annotated[str, Query(min_length=1, max_length=64)],
    trip_id: Annotated[str | None, Query(max_length=64)] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    pad_minutes: Annotated[int, Query(ge=0, le=120)] = 30,
) -> Response:
    """Download one trip's diagnostic bundle. Takes the trip page's own params."""
    start, end = _validated_range(start, end, default_span=timedelta(hours=6))
    errors: list[str] = []

    timeline = await get_vehicle_trip(
        vehicle_label=vehicle_label, db=db, trip_id=trip_id, start=start, end=end
    )
    resolved_trip_id: str | None = timeline.get("trip_id")
    route_id: str | None = timeline.get("route_id")

    # Raw rows get a margin either side of the page's window: the approach to
    # the origin and the run-in to the terminus are where detection most often
    # goes wrong, and the page's bounds are cut tight to the leg.
    pad = timedelta(minutes=pad_minutes)
    raw_start, raw_end = start - pad, end + pad

    positions = await _vehicle_positions(db, vehicle_label, resolved_trip_id, route_id, raw_start, raw_end)
    arrivals = await _stop_arrival_events(db, resolved_trip_id, raw_start, raw_end)
    trip_updates, tu_truncated = await _trip_updates(db, resolved_trip_id, route_id, raw_start, raw_end)
    route_activity = await _route_activity(db, route_id, raw_start, raw_end)

    full_stops = _attempt(errors, "full stop sequence", lambda: list(load_trip_stop_sequence(resolved_trip_id or "")), [])
    timepoints = _attempt(errors, "timepoint schedule", lambda: load_trip_stop_schedule().get(resolved_trip_id or "", []), [])
    detection_tps = _attempt(errors, "detection schedule", lambda: load_trip_shape_dist_schedule().get(resolved_trip_id or "", []), [])
    trip_meta, block_trips = _attempt(errors, "trips.txt", lambda: _trip_and_block(resolved_trip_id), (None, []))
    competing = _attempt(
        errors,
        "competing slots",
        lambda: _competing_slots(route_id, resolved_trip_id, timepoints, route_activity),
        [],
    )
    feed = _attempt(errors, "static feed health", gtfs_health.snapshot, None)
    rail = _attempt(errors, "route mode", lambda: is_rail_route(route_id), None)

    detection_seqs = {tp[0] for tp in detection_tps}
    own_fixes = [p for p in positions if p["vehicle_label"] == vehicle_label]

    bundle: dict[str, Any] = {
        "export_version": EXPORT_VERSION,
        "generated_at": datetime.now(tz=timezone.utc),
        # Baked in at image build (backend/Dockerfile); None on a local run.
        "build": {"git_sha": os.environ.get("GIT_SHA") or None},
        "request": {
            "vehicle_label": vehicle_label,
            "trip_id": trip_id,
            "start": start,
            "end": end,
            "pad_minutes": pad_minutes,
            "raw_window": {"start": raw_start, "end": raw_end},
        },
        "resolved": {
            "trip_id": resolved_trip_id,
            "route_id": route_id,
            "is_rail": rail,
            "arrival_radius_m": _radius_for(route_id),
            "terminus_fallback_radius_m": _terminus_fallback_radius_for(route_id),
        },
        "config": _config_snapshot(),
        "gtfs_feed": feed,
        "timeline": timeline,
        "schedule": {
            "trip": trip_meta,
            "stops": full_stops,
            "timepoints": [
                {
                    "stop_sequence": seq,
                    "stop_id": stop_id,
                    "arrival_secs": secs,
                    "arrival_hms": _hms(secs),
                    "stop_lat": lat,
                    "stop_lon": lon,
                    # False means the detector never looks for this stop, so a
                    # missing arrival here is by design rather than a miss.
                    "used_by_detector": seq in detection_seqs,
                }
                for seq, stop_id, secs, lat, lon in timepoints
            ],
            "block_trips": block_trips,
        },
        "raw": {
            "vehicle_positions": positions,
            "stop_arrival_events": arrivals,
            "trip_updates": trip_updates,
            "trip_updates_truncated": tu_truncated,
        },
        "diagnostics": {
            "trip_ids_seen_on_vehicle": _trip_id_runs(own_fixes),
            "fixes": _fix_diagnostics(own_fixes, timepoints, _radius_for(route_id)),
            "duplicate_arrival_rows": _duplicate_arrivals(arrivals),
            "route_activity": route_activity,
            "competing_slots": competing,
        },
        "errors": errors,
    }

    body = json.dumps(bundle, default=_json_default, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_filename(vehicle_label, resolved_trip_id, start)}"'},
    )


# ── Raw rows ────────────────────────────────────────────────────────────────


def _row(obj: Any) -> dict[str, Any]:
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}


async def _vehicle_positions(
    db: AsyncSession,
    vehicle_label: str,
    trip_id: str | None,
    route_id: str | None,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """This vehicle's fixes, anomalous included, plus other vehicles on the same trip_id."""
    own = select(VehiclePosition).where(
        VehiclePosition.vehicle_label == vehicle_label,
        VehiclePosition.timestamp >= start,
        VehiclePosition.timestamp <= end,
    )
    rows = list((await db.execute(own)).scalars().all())

    if trip_id:
        others = select(VehiclePosition).where(
            VehiclePosition.trip_id == trip_id,
            VehiclePosition.vehicle_label != vehicle_label,
            VehiclePosition.timestamp >= start,
            VehiclePosition.timestamp <= end,
        )
        # route_id lets Timescale skip compressed segments of every other route.
        if route_id:
            others = others.where(VehiclePosition.route_id == route_id)
        rows.extend((await db.execute(others)).scalars().all())

    rows.sort(key=lambda r: (r.timestamp, r.vehicle_label or ""))
    return [_row(r) for r in rows]


async def _stop_arrival_events(
    db: AsyncSession, trip_id: str | None, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    if not trip_id:
        return []
    stmt = (
        select(StopArrivalEvent)
        .where(
            StopArrivalEvent.trip_id == trip_id,
            StopArrivalEvent.timestamp >= start,
            StopArrivalEvent.timestamp <= end,
        )
        .order_by(StopArrivalEvent.stop_sequence, StopArrivalEvent.timestamp)
    )
    return [_row(r) for r in (await db.execute(stmt)).scalars().all()]


async def _trip_updates(
    db: AsyncSession,
    trip_id: str | None,
    route_id: str | None,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    if not trip_id:
        return [], False
    stmt = select(TripUpdate).where(
        TripUpdate.trip_id == trip_id,
        TripUpdate.timestamp >= start,
        TripUpdate.timestamp <= end,
    )
    # Compressed chunks are segmented by route_id; see _delay_map in vehicles.py.
    if route_id:
        stmt = stmt.where(TripUpdate.route_id == route_id)
    stmt = stmt.order_by(TripUpdate.timestamp, TripUpdate.stop_sequence).limit(_TRIP_UPDATE_ROW_CAP + 1)
    rows = (await db.execute(stmt)).scalars().all()
    return [_row(r) for r in rows[:_TRIP_UPDATE_ROW_CAP]], len(rows) > _TRIP_UPDATE_ROW_CAP


async def _route_activity(
    db: AsyncSession, route_id: str | None, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Every (trip_id, vehicle) the feed reported on this route in the window."""
    if not route_id:
        return []
    stmt = (
        select(
            VehiclePosition.trip_id,
            VehiclePosition.vehicle_label,
            func.min(VehiclePosition.timestamp),
            func.max(VehiclePosition.timestamp),
            func.count(),
        )
        .where(
            VehiclePosition.route_id == route_id,
            VehiclePosition.timestamp >= start,
            VehiclePosition.timestamp <= end,
        )
        .group_by(VehiclePosition.trip_id, VehiclePosition.vehicle_label)
        .order_by(func.min(VehiclePosition.timestamp))
    )
    return [
        {"trip_id": t, "vehicle_label": v, "first_seen": _aware(lo), "last_seen": _aware(hi), "fixes": n}
        for t, v, lo, hi, n in (await db.execute(stmt)).all()
    ]


# ── Derived diagnostics ─────────────────────────────────────────────────────


def _trip_id_runs(fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive runs of one trip_id on the vehicle — shows mid-leg trip_id flips."""
    runs: list[dict[str, Any]] = []
    for f in fixes:
        if runs and runs[-1]["trip_id"] == f["trip_id"] and runs[-1]["route_id"] == f["route_id"]:
            runs[-1]["last_seen"] = f["timestamp"]
            runs[-1]["fixes"] += 1
        else:
            runs.append(
                {
                    "trip_id": f["trip_id"],
                    "route_id": f["route_id"],
                    "first_seen": f["timestamp"],
                    "last_seen": f["timestamp"],
                    "fixes": 1,
                }
            )
    return runs


def _fix_diagnostics(
    fixes: list[dict[str, Any]],
    timepoints: list[tuple[int, str, int, float, float]],
    radius_m: float,
) -> list[dict[str, Any]]:
    """Per fix: how it relates to the previous fix and to the nearest timepoint.

    "Previous" is the previous *trusted* fix, matching how position_anomaly.py
    measures a jump — otherwise one glitch makes its honest successor look like
    a second jump back.
    """
    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    for f in fixes:
        d: dict[str, Any] = {
            "id": f["id"],
            "timestamp": f["timestamp"],
            "trip_id": f["trip_id"],
            "is_anomalous": f["is_anomalous"],
            "seconds_since_prev": None,
            "meters_from_prev": None,
            "implied_speed_kmh": None,
            "nearest_timepoint_seq": None,
            "nearest_timepoint_stop_id": None,
            "nearest_timepoint_m": None,
            "inside_arrival_radius": None,
        }
        lat, lon = f["latitude"], f["longitude"]
        if lat is not None and lon is not None:
            if prev is not None:
                dt = (_aware(f["timestamp"]) - _aware(prev["timestamp"])).total_seconds()
                dist = _haversine_m(prev["latitude"], prev["longitude"], lat, lon)
                d["seconds_since_prev"] = round(dt, 1)
                d["meters_from_prev"] = round(dist, 1)
                d["implied_speed_kmh"] = round(dist / dt * 3.6, 1) if dt > 0 else None
            if timepoints:
                seq, stop_id, _, s_lat, s_lon = min(
                    timepoints, key=lambda tp: _haversine_m(lat, lon, tp[3], tp[4])
                )
                gap = _haversine_m(lat, lon, s_lat, s_lon)
                d["nearest_timepoint_seq"] = seq
                d["nearest_timepoint_stop_id"] = stop_id
                d["nearest_timepoint_m"] = round(gap, 1)
                d["inside_arrival_radius"] = gap <= radius_m
            if not f["is_anomalous"]:
                prev = f
        out.append(d)
    return out


def _duplicate_arrivals(arrivals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """(stop_sequence, service_date) pairs written more than once — dedup is app-side."""
    seen: dict[tuple[int, Any], list[int]] = {}
    for a in arrivals:
        seen.setdefault((a["stop_sequence"], a["service_date"]), []).append(a["id"])
    return [
        {"stop_sequence": seq, "service_date": sd, "row_ids": ids}
        for (seq, sd), ids in seen.items()
        if len(ids) > 1
    ]


def _competing_slots(
    route_id: str | None,
    trip_id: str | None,
    timepoints: list[tuple[int, str, int, float, float]],
    route_activity: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Other trips due at this trip's timepoints within ±15 min, and whether they were live."""
    if not route_id or not trip_id or not timepoints:
        return []
    index = load_stop_arrivals_index()
    live = {r["trip_id"] for r in route_activity if r["trip_id"]}
    out: list[dict[str, Any]] = []
    for seq, stop_id, secs, _, _ in timepoints:
        rivals = [
            {
                "trip_id": other,
                "arrival_secs": other_secs,
                "offset_seconds": other_secs - secs,
                "seen_in_feed": other in live,
            }
            for other_secs, other in index.get((route_id, stop_id), [])
            if other != trip_id and abs(other_secs - secs) <= _COMPETING_SLOT_WINDOW_S
        ]
        if rivals:
            out.append({"stop_sequence": seq, "stop_id": stop_id, "arrival_secs": secs, "rivals": rivals})
    return out


def _trip_and_block(trip_id: str | None) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    """This trip's trips.txt row, and every trip sharing its block_id in run order.

    A block is one vehicle's day, so its siblings are the trips the vehicle ran
    immediately before and after — which is where a stale or early trip_id on
    the feed comes from.
    """
    if not trip_id:
        return None, []
    root = resolve_gtfs_static_root()
    rows: list[dict[str, str]] = []
    for folder in TRANSIT_FOLDERS:
        path = root / folder / "trips.txt"
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as handle:
                rows.extend(csv.DictReader(handle))

    meta = next((r for r in rows if r.get("trip_id") == trip_id), None)
    block_id = (meta or {}).get("block_id")
    if not block_id:
        return meta, []

    schedule = load_trip_stop_schedule()
    siblings = []
    for r in rows:
        if r.get("block_id") != block_id or r.get("service_id") != meta.get("service_id"):
            continue
        tps = schedule.get(r["trip_id"], [])
        siblings.append(
            {
                "trip_id": r["trip_id"],
                "route_id": r.get("route_id"),
                "direction_id": r.get("direction_id"),
                "trip_headsign": r.get("trip_headsign"),
                "first_timepoint_secs": tps[0][2] if tps else None,
                "first_timepoint_hms": _hms(tps[0][2]) if tps else None,
                "last_timepoint_hms": _hms(tps[-1][2]) if tps else None,
                "is_this_trip": r["trip_id"] == trip_id,
            }
        )
    siblings.sort(key=lambda s: (s["first_timepoint_secs"] is None, s["first_timepoint_secs"] or 0))
    return meta, siblings


# ── Helpers ─────────────────────────────────────────────────────────────────


def _attempt(errors: list[str], what: str, fn: Callable[[], T], fallback: T) -> T:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one bad lookup mustn't sink the export
        errors.append(f"{what}: {type(exc).__name__}: {exc}")
        return fallback


def _config_snapshot() -> dict[str, Any]:
    return {
        k: v
        for k, v in _settings.model_dump().items()
        if k.startswith(_CONFIG_PREFIXES)
    }


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _hms(secs: int | None) -> str | None:
    """GTFS seconds-since-service-midnight as HH:MM:SS (may exceed 24h, as GTFS does)."""
    if secs is None:
        return None
    return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return _aware(o).isoformat()
    if isinstance(o, date):
        return o.isoformat()
    return str(o)


def _filename(vehicle_label: str, trip_id: str | None, start: datetime) -> str:
    safe = lambda s: re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-") or "x"  # noqa: E731
    stamp = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")
    return f"trip_{safe(vehicle_label)}_{safe(trip_id or 'unknown')}_{stamp}.json"
