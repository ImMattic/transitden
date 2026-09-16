"""Scheduled-service analytics derived from the GTFS *static* feeds.

GTFS-RT tells us what actually happened; the static schedule tells us what was
*supposed* to happen.  Comparing the two is what makes the dashboard useful for
budget/service decisions: scheduled trips vs. trips actually operated, and
scheduled headway vs. delivered frequency.

This module parses ``trips.txt`` + ``calendar.txt`` + ``stop_times.txt`` once and
caches a compact per-route summary in module globals — the same pattern as
``gtfs_decoder.load_gtfs_static_data``.  To stay light on a small VM we only keep
*one* departure per trip (its origin/first-stop departure), which is enough to
estimate trips-per-day, headway-by-hour, and span of service per direction.
"""
from __future__ import annotations

import csv
import functools
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.services.gtfs_decoder import (
    TRANSIT_FOLDERS,
    load_gtfs_static_data,
    resolve_gtfs_static_root,
)

# {route_id: route_schedule_dict}.  None until first load.
_schedule_cache: dict[str, dict[str, Any]] | None = None

# {trip_id: [(stop_sequence, stop_id, arrival_secs, stop_lat, stop_lon), ...]}.
# Only timepoint stops are kept (see load_trip_stop_schedule).  None until first
# load.
_trip_stop_schedule_cache: dict[str, list[tuple[int, str, int, float, float]]] | None = None

# {route_id: {"0": {"headsign": str, "trip_ids": [str]}, "1": {...}}}.
# None until first load.
_direction_info_cache: dict[str, dict[str, dict[str, Any]]] | None = None

# {trip_id: (stop_sequence, stop_id, arrival_secs, stop_lat, stop_lon)} for the
# *first* timepoint of each trip — its origin terminal.  None until first load.
_trip_origin_cache: dict[str, tuple[int, str, int, float, float]] | None = None


def _parse_gtfs_time(value: str | None) -> int | None:
    """Parse a GTFS ``HH:MM:SS`` time into seconds since midnight.

    GTFS allows hours >= 24 for trips that run past midnight; we keep them as-is
    (callers fold into 0-23 via modulo when bucketing by hour-of-day)."""
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def _fmt_hm(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    h = (seconds // 3600) % 24
    m = (seconds % 3600) // 60
    return f"{h:02d}:{m:02d}"


_DOW = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _load_service_daytypes(root: Path) -> dict[str, dict[str, bool]]:
    """{service_id: {dow: bool}} for all seven days of the week.

    Tracking each day separately avoids double-counting when a route has
    mutually-exclusive service_ids for different day subsets (e.g. RTD's
    MT = Mon-Thu, FR = Friday-only — both have 'weekday=True' under the
    old collapsed approach, inflating scheduled trips by ~2x).
    """
    daytypes: dict[str, dict[str, bool]] = {}
    for folder in TRANSIT_FOLDERS:
        f = root / folder / "calendar.txt"
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                sid = row.get("service_id")
                if not sid:
                    continue
                daytypes[sid] = {dow: row.get(dow) == "1" for dow in _DOW}
    return daytypes


def _load_trip_meta(root: Path) -> dict[str, tuple[str, str, str]]:
    """{trip_id: (route_id, service_id, direction_id)}."""
    meta: dict[str, tuple[str, str, str]] = {}
    for folder in TRANSIT_FOLDERS:
        f = root / folder / "trips.txt"
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                tid = row.get("trip_id")
                rid = row.get("route_id")
                if not tid or not rid:
                    continue
                meta[tid] = (rid, row.get("service_id", ""), row.get("direction_id", "0"))
    return meta


def _load_trip_origin_departures(root: Path) -> dict[str, int]:
    """{trip_id: origin_departure_seconds} — the departure at each trip's
    first (minimum stop_sequence) stop."""
    best_seq: dict[str, int] = {}
    origin_dep: dict[str, int] = {}
    for folder in TRANSIT_FOLDERS:
        f = root / folder / "stop_times.txt"
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                tid = row.get("trip_id")
                seq_str = row.get("stop_sequence")
                if not tid or not seq_str:
                    continue
                try:
                    seq = int(seq_str)
                except ValueError:
                    continue
                if tid in best_seq and seq >= best_seq[tid]:
                    continue
                dep = _parse_gtfs_time(row.get("departure_time") or row.get("arrival_time"))
                if dep is None:
                    continue
                best_seq[tid] = seq
                origin_dep[tid] = dep
    return origin_dep


def _build_schedule(gtfs_static_root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = gtfs_static_root or resolve_gtfs_static_root()

    daytypes = _load_service_daytypes(root)
    trip_meta = _load_trip_meta(root)
    origin_dep = _load_trip_origin_departures(root)

    # Per route: trip counts per day-of-week and Monday origin departures for
    # headway estimation.  Tracking all 7 DOWs separately avoids double-counting
    # mutually-exclusive service_ids (e.g. RTD MT vs FR).
    _zero_dow: dict[str, int] = {d: 0 for d in _DOW}
    trips_per_dow: dict[str, dict[str, int]] = defaultdict(lambda: dict(_zero_dow))
    weekday_deps: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list))  # route_id -> direction -> [dep_secs]

    for tid, (rid, sid, direction) in trip_meta.items():
        dt = daytypes.get(sid)
        if dt:
            for dow in _DOW:
                if dt[dow]:
                    trips_per_dow[rid][dow] += 1
        dep = origin_dep.get(tid)
        # Use Monday as the representative weekday for headway estimation so we
        # don't double-count Mon-Thu + Friday-only trips.
        if dep is not None and dt is not None and dt["monday"]:
            weekday_deps[rid][direction].append(dep)

    schedule: dict[str, dict[str, Any]] = {}
    all_route_ids = set(trips_per_dow) | set(weekday_deps)
    for rid in all_route_ids:
        tdow = trips_per_dow.get(rid, dict(_zero_dow))
        dirs = weekday_deps.get(rid, {})

        # Headway by hour-of-day: per direction, count departures per hour and
        # take 60/count; average across directions that run that hour.
        hour_headways: dict[int, float | None] = {}
        all_deps: list[int] = []
        for hour in range(24):
            per_dir: list[float] = []
            for deps in dirs.values():
                cnt = sum(1 for d in deps if (d // 3600) % 24 == hour)
                if cnt > 0:
                    per_dir.append(60.0 / cnt)
            hour_headways[hour] = round(sum(per_dir) / len(per_dir), 1) if per_dir else None
        for deps in dirs.values():
            all_deps.extend(deps)

        span_start = min(all_deps) if all_deps else None
        span_end = max(all_deps) if all_deps else None

        schedule[rid] = {
            "route_id": rid,
            # Monday as representative weekday for display; per-DOW dict used
            # for accurate scheduled-trip counting in service delivery.
            "weekday_trips": tdow["monday"],
            "saturday_trips": tdow["saturday"],
            "sunday_trips": tdow["sunday"],
            "trips_per_dow": tdow,
            "service_span": {"start": _fmt_hm(span_start), "end": _fmt_hm(span_end)},
            "headways_by_hour": hour_headways,
        }
    return schedule


def load_schedule_summary(gtfs_static_root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return the cached {route_id: schedule_summary} map, building it on first call."""
    global _schedule_cache
    if _schedule_cache is None:
        _schedule_cache = _build_schedule(gtfs_static_root)
    return _schedule_cache


def _build_trip_stop_schedule(
    gtfs_static_root: Path | None = None,
) -> dict[str, list[tuple[int, str, int, float, float]]]:
    """{trip_id: [(stop_sequence, stop_id, arrival_secs, stop_lat, stop_lon)]}.

    Only *timepoint* stops (``timepoint == "1"``) are kept.  Timepoints are the
    stops RTD itself uses to measure schedule adherence, so they are both the
    correct place to gauge on-time performance and a ~10x smaller slice of
    ``stop_times.txt`` than every stop — important on a small VM.

    Stop coordinates are joined from the GTFS *stops* table so the observed
    on-time logic can geofence a live vehicle against each timepoint.
    """
    root = gtfs_static_root or resolve_gtfs_static_root()
    _, stops = load_gtfs_static_data(gtfs_static_root=root)

    schedule: dict[str, list[tuple[int, str, int, float, float]]] = defaultdict(list)
    for folder in TRANSIT_FOLDERS:
        f = root / folder / "stop_times.txt"
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("timepoint") != "1":
                    continue
                tid = row.get("trip_id")
                stop_id = (row.get("stop_id") or "").strip()
                seq_str = row.get("stop_sequence")
                if not tid or not stop_id or not seq_str:
                    continue
                arr_secs = _parse_gtfs_time(
                    row.get("arrival_time") or row.get("departure_time")
                )
                if arr_secs is None:
                    continue
                stop = stops.get(stop_id)
                if not stop or not stop.get("stop_lat") or not stop.get("stop_lon"):
                    continue
                try:
                    seq = int(seq_str)
                except ValueError:
                    continue
                schedule[tid].append(
                    (seq, stop_id, arr_secs, stop["stop_lat"], stop["stop_lon"])
                )

    # Sort each trip's timepoints by sequence for deterministic iteration.
    for stops_list in schedule.values():
        stops_list.sort(key=lambda s: s[0])
    return dict(schedule)


def load_trip_stop_schedule(
    gtfs_static_root: Path | None = None,
) -> dict[str, list[tuple[int, str, int, float, float]]]:
    """Return the cached per-trip timepoint schedule, building it on first call."""
    global _trip_stop_schedule_cache
    if _trip_stop_schedule_cache is None:
        _trip_stop_schedule_cache = _build_trip_stop_schedule(gtfs_static_root)
    return _trip_stop_schedule_cache


def load_trip_origin_timepoints(
    gtfs_static_root: Path | None = None,
) -> dict[str, tuple[int, str, int, float, float]]:
    """{trip_id: (stop_sequence, stop_id, arrival_secs, stop_lat, stop_lon)} — origins.

    The trip's *first* timepoint: where the run starts and where the scheduled
    time is a **departure**, not an arrival.  ``services/ontime.py`` times this
    stop by watching the vehicle leave rather than by geofencing its first
    sighting (a bus lays over at the gate for minutes before it pulls out).

    Built from the unfiltered timepoint schedule on purpose — the shape-dist
    schedule drops timepoints that sit too close together, and dropping a trip's
    origin there must not turn its departure into an anonymous mid-route stop.
    """
    global _trip_origin_cache
    if _trip_origin_cache is None:
        base = load_trip_stop_schedule(gtfs_static_root)
        _trip_origin_cache = {tid: tps[0] for tid, tps in base.items() if tps}
    return _trip_origin_cache


@functools.lru_cache(maxsize=512)
def load_trip_stop_sequence(
    trip_id: str,
    gtfs_static_root: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Every scheduled stop for one trip — origin terminus → destination terminus.

    Unlike ``load_trip_stop_schedule`` (timepoint stops only, cached for *all*
    trips at once), this keeps **every** stop the trip serves.  Holding that for
    all ~20k trips would be ~800k rows — too much for a small VM — so instead
    each call scans ``stop_times.txt`` for the one ``trip_id`` (a cheap
    ``str.startswith`` prefilter, then ``csv`` only parses the ~20-60 matching
    lines) and the result is LRU-cached for repeat views of the same trip.

    Returns a tuple (immutable, so ``lru_cache`` can hold it) of dicts ordered
    by ``stop_sequence``::

        {stop_sequence, stop_id, stop_name, stop_lat, stop_lon, arrival_secs,
         stop_headsign, is_timepoint, pickup_type, drop_off_type}

    Empty tuple when the trip is not in the bundled static schedule (e.g. RTD
    added it after this GTFS bundle was cut).
    """
    if not trip_id:
        return ()

    root = gtfs_static_root or resolve_gtfs_static_root()
    _, stops = load_gtfs_static_data(gtfs_static_root=root)

    prefix = f'"{trip_id}",'
    rows: list[dict[str, Any]] = []
    for folder in TRANSIT_FOLDERS:
        f = root / folder / "stop_times.txt"
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8", newline="") as handle:
            header = handle.readline()
            matched = [line for line in handle if line.startswith(prefix)]
        if not matched:
            continue
        for row in csv.DictReader([header, *matched]):
            seq_str = row.get("stop_sequence")
            stop_id = (row.get("stop_id") or "").strip()
            if not seq_str or not stop_id:
                continue
            try:
                seq = int(seq_str)
            except ValueError:
                continue
            stop = stops.get(stop_id, {})
            rows.append(
                {
                    "stop_sequence": seq,
                    "stop_id": stop_id,
                    "stop_name": stop.get("stop_name") or None,
                    "stop_lat": stop.get("stop_lat"),
                    "stop_lon": stop.get("stop_lon"),
                    "arrival_secs": _parse_gtfs_time(
                        row.get("arrival_time") or row.get("departure_time")
                    ),
                    "stop_headsign": (row.get("stop_headsign") or "").strip() or None,
                    "is_timepoint": row.get("timepoint") == "1",
                    "pickup_type": (row.get("pickup_type") or "0").strip() or "0",
                    "drop_off_type": (row.get("drop_off_type") or "0").strip() or "0",
                }
            )
        break  # a trip_id lives in exactly one feed folder

    rows.sort(key=lambda r: r["stop_sequence"])
    return tuple(rows)


def scheduled_trips_for_daytype(route_id: str, daytype: str) -> int | None:
    """Scheduled trips for one route on a given day-type (weekday/saturday/sunday)."""
    summary = load_schedule_summary().get(route_id)
    if not summary:
        return None
    return summary.get(f"{daytype}_trips")


def _build_direction_info(gtfs_static_root: Path | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Build {route_id: {direction_id: {headsign, trip_ids}}} from trips.txt.

    For each route+direction pair, picks the most common trip_headsign as the
    canonical direction label (e.g. "Union Station" vs "Englewood Station").
    """
    root = gtfs_static_root or resolve_gtfs_static_root()

    # route_id -> direction_id -> {headsign -> count, trip_ids: [...]}
    raw: dict[str, dict[str, dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {"counts": defaultdict(int), "trip_ids": []}))

    for folder in TRANSIT_FOLDERS:
        f = root / folder / "trips.txt"
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                tid = row.get("trip_id", "").strip()
                rid = row.get("route_id", "").strip()
                did = row.get("direction_id", "0").strip() or "0"
                headsign = row.get("trip_headsign", "").strip()
                if not tid or not rid:
                    continue
                raw[rid][did]["counts"][headsign] += 1
                raw[rid][did]["trip_ids"].append(tid)

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for rid, dirs in raw.items():
        result[rid] = {}
        for did, info in dirs.items():
            top_headsign = max(info["counts"], key=lambda h: info["counts"][h], default="")
            result[rid][did] = {"headsign": top_headsign, "trip_ids": info["trip_ids"]}
    return result


def load_route_direction_info(
    gtfs_static_root: Path | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return cached direction info, building it on first call."""
    global _direction_info_cache
    if _direction_info_cache is None:
        _direction_info_cache = _build_direction_info(gtfs_static_root)
    return _direction_info_cache


# ── Route-corridor schedule: timepoints with cumulative inter-stop distances ──
# {trip_id: [(stop_sequence, stop_id, arrival_secs, stop_lat, stop_lon, dist_m)]}
# dist_m is the cumulative haversine distance from the trip's first timepoint to
# this one, measured as straight lines between consecutive timepoints.  This lets
# classify_arrival() project a vehicle position onto the inter-timepoint polyline
# and find which timepoint it is nearest to along the route rather than by simple
# crow-flies circle — reducing false matches on parallel streets and curved routes.
_trip_shape_dist_cache: dict[str, list[tuple[int, str, int, float, float, float]]] | None = None

# {(route_id, stop_id): sorted [(arrival_secs, trip_id), ...] across all trips on
# that route}.  Used to detect trip_id misassignment: when a sighting lands almost
# exactly on a competing trip's slot, the bus may be running that trip instead.
# The trip_id travels with the time because knowing *which* trip is the competitor
# is what lets the guard ask whether that trip is itself out on the road — see
# ``_build_event`` in services/ontime.py.
_stop_arrivals_cache: dict[tuple[str, str], list[tuple[int, str]]] | None = None

# Minimum distance (metres) between adjacent timepoints for a stop to be used
# in on-time detection.  Both members of a too-close pair are dropped.
#
# This used to be a *schedule-time* gap of 120 s, on the reasoning that stops
# only a minute or two apart couldn't be told apart.  That was calibrated for
# buses, whose timepoints are sparse; rail stations are 1.5-2.5 min apart, so
# it silently removed 14.6% of all rail timepoints -- entire runs of stations
# (Empower Field, Auraria West, Decatur/Federal, Knox on the W line) could
# never be detected no matter how squarely a train parked on them.
#
# Detection matches on distance along the route and picks the nearest
# timepoint, so what actually creates ambiguity is closeness in *space*, not
# in time.  Hence a metric threshold, set near the bus geofence radius: two
# timepoints closer together than the circle used to find them genuinely
# cannot be distinguished.  Rail is untouched by it -- the closest pair of
# adjacent rail timepoints in the bundled feed is 649 m.
_MIN_TIMEPOINT_GAP_M = 150.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    from math import asin, cos, radians, sin, sqrt

    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * 6_371_000.0 * asin(sqrt(a))


def _build_trip_shape_dist_schedule(
    gtfs_static_root: Path | None = None,
) -> dict[str, list[tuple[int, str, int, float, float, float]]]:
    """Build a per-trip timepoint schedule enriched with cumulative route distances.

    Starting from the existing timepoint schedule (timepoint==1 stops only),
    compute the cumulative straight-line distance between consecutive timepoints
    for each trip.  Also filter out any timepoint within _MIN_TIMEPOINT_GAP_M of
    either neighbour — those stops sit too close together to tell apart by
    position, which is how arrivals are matched.
    """
    base = load_trip_stop_schedule(gtfs_static_root)

    result: dict[str, list[tuple[int, str, int, float, float, float]]] = {}
    for trip_id, tps in base.items():
        # Compute cumulative distance along the inter-timepoint polyline.
        with_dist: list[tuple[int, str, int, float, float, float]] = []
        cum = 0.0
        for i, (seq, stop_id, arr_secs, lat, lon) in enumerate(tps):
            if i > 0:
                _, _, _, plat, plon = tps[i - 1]
                cum += _haversine_m(plat, plon, lat, lon)
            with_dist.append((seq, stop_id, arr_secs, lat, lon, cum))

        # Drop timepoints within _MIN_TIMEPOINT_GAP_M of either neighbour.
        #
        # The origin and terminus are exempt.  The filter exists to stop two
        # near-coincident timepoints from stealing each other's observations,
        # but at the ends of a trip there is no ambiguity about which stop a
        # vehicle sitting at the end of the line is at — and dropping them has
        # a much worse failure mode: the trip page shows the stop with no
        # arrival colour at all, and a missing *terminus* arrival is exactly
        # what flags a completed trip as "Incomplete".  Measured against RTD's
        # bundled feed, this filter was removing the terminus on 3.3% of trips.
        # load_trip_origin_timepoints() already sidesteps it for the same reason.
        last_i = len(with_dist) - 1
        filtered: list[tuple[int, str, int, float, float, float]] = []
        for i, tp in enumerate(with_dist):
            if i == 0 or i == last_i:
                filtered.append(tp)
                continue
            dist = tp[5]
            prev_gap = dist - with_dist[i - 1][5]
            next_gap = with_dist[i + 1][5] - dist
            if min(prev_gap, next_gap) >= _MIN_TIMEPOINT_GAP_M:
                filtered.append(tp)

        if filtered:
            result[trip_id] = filtered

    return result


def load_trip_shape_dist_schedule(
    gtfs_static_root: Path | None = None,
) -> dict[str, list[tuple[int, str, int, float, float, float]]]:
    """Cached per-trip timepoint schedule with cumulative inter-stop distances."""
    global _trip_shape_dist_cache
    if _trip_shape_dist_cache is None:
        _trip_shape_dist_cache = _build_trip_shape_dist_schedule(gtfs_static_root)
    return _trip_shape_dist_cache


def _build_stop_arrivals_index(
    gtfs_static_root: Path | None = None,
) -> dict[tuple[str, str], list[tuple[int, str]]]:
    """Build {(route_id, stop_id): sorted [(arrival_secs, trip_id)]} across all trips.

    Joining the shape-dist schedule (trip_id → timepoints) with trip metadata
    (trip_id → route_id) gives us all scheduled arrival times at each stop for
    each route, and which trip owns each one.  classify_arrival() uses this to
    detect when a competing trip explains an observed arrival better than the
    trip_id the feed supplied — the signature of a GTFS-RT trip_id misassignment
    on high-frequency routes — and to check whether that competitor is itself
    reporting, which is what distinguishes an alias from an ordinary late bus.
    """
    root = gtfs_static_root or resolve_gtfs_static_root()
    shape_dist_schedule = load_trip_shape_dist_schedule(gtfs_static_root)
    trip_meta = _load_trip_meta(root)

    raw: dict[tuple[str, str], set[tuple[int, str]]] = defaultdict(set)
    for trip_id, timepoints in shape_dist_schedule.items():
        meta = trip_meta.get(trip_id)
        if not meta:
            continue
        route_id = meta[0]
        for _, stop_id, arrival_secs, _, _, _ in timepoints:
            raw[(route_id, stop_id)].add((arrival_secs, trip_id))

    return {k: sorted(v) for k, v in raw.items()}


def load_stop_arrivals_index(
    gtfs_static_root: Path | None = None,
) -> dict[tuple[str, str], list[tuple[int, str]]]:
    """Cached {(route_id, stop_id): sorted [(arrival_secs, trip_id)]} for misassignment detection."""
    global _stop_arrivals_cache
    if _stop_arrivals_cache is None:
        _stop_arrivals_cache = _build_stop_arrivals_index(gtfs_static_root)
    return _stop_arrivals_cache
