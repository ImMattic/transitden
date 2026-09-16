"""Observed on-time performance from vehicle positions vs. the static schedule.

RTD's GTFS-RT TripUpdate feed leaves ``arrival.delay`` unset (it only sends the
predicted ``arrival.time``), so the old delay-based on-time number read ~100%
on-time — useless.  Instead we measure adherence the way RTD itself does: take
where a bus *actually* is (``vehicle_positions``, polled every ~30s), project it
onto the inter-timepoint route polyline, and compare the observed arrival time
to the scheduled time.

The projection approach (vs. the old haversine circle) gives one improvement
over a plain radius: route-direction awareness — vehicles on parallel streets
that happen to be within the geofence crow-flies of a stop are filtered out by
the lateral-distance check before the per-timepoint search even runs.  Beyond
that, the rule is deliberately dumb: whichever timepoint the vehicle's
along-route position is closest to, within ``arrival_radius_m``, is the
arrival — full stop.  An earlier version also gated on the GTFS-RT
``current_status``/``current_stop_sequence`` fields (skipping a timepoint
while the feed still said IN_TRANSIT_TO it), meant to stop a vehicle idling
short of a stop from locking in an early match.  In practice RTD's feed often
reports IN_TRANSIT_TO the stop a vehicle is *currently sitting at* — it only
advances once the vehicle pulls away — so that gate was silently discarding
the correct in-radius match far more often than it caught a genuine early one,
leaving stops with a clear geofence hit in the position track (visible in the
trip replay) unclassified.  Removed; see git history for the old behaviour.

The **origin** timepoint is the one exception, and it is timed differently — see
``OriginDepartureTracker`` below.

The public entry points:
  * ``classify_arrival`` — pure function: one position + the 6-tuple shape-dist
    schedule + an observation time → one arrival event dict (or ``None``).
    No I/O, easy to unit-test.
  * ``OriginDepartureTracker`` — stateful: a stream of positions in time order →
    one *departure* event per trip origin.
  * ``TerminusFallbackTracker`` — stateful: a wider last-resort circle at the
    *final* timepoint, for trips whose feed cuts out on approach.
  * ``detect_arrivals`` — runs all of them over a poll's worth of positions,
    loading the cached schedule and de-duping so a bus loitering near a
    timepoint yields a single event.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services.gtfs_decoder import load_gtfs_static_data
from app.services.gtfs_schedule import (
    load_stop_arrivals_index,
    load_trip_origin_timepoints,
    load_trip_shape_dist_schedule,
)

_settings = get_settings()
_DENVER = ZoneInfo("America/Denver")
_EARTH_RADIUS_M = 6_371_000.0

# In-memory record of arrivals already emitted, keyed by (trip_id, stop_sequence,
# service_date).  The DB unique index is the authoritative dedup across process
# restarts; this just avoids re-emitting on every 30s poll while a bus sits at a
# stop.  Pruned to recent service dates so it can't grow unbounded.
_recorded: set[tuple[str, int, date]] = set()

# Origin-departure state for the live ingest loop.  Built on first poll (see
# _live_tracker) because it needs the GTFS static caches to be warm.
_live_tracker_instance: OriginDepartureTracker | None = None

# Terminus-fallback state for the live ingest loop, same lifecycle.
_live_terminus_tracker: TerminusFallbackTracker | None = None

# Each trip's most recent (position row, observation time), so the next poll
# can interpolate which timepoints were passed in between — see
# classify_segment_arrivals.  Pruned alongside _recorded.
_last_fix: dict[str, tuple[dict[str, Any], datetime]] = {}

# (trip_id, service_date) runs whose terminus arrival has been recorded.  The
# run is over at that point, so nothing later can add to it: a vehicle that
# turns around at the end of the line — or loops back past stops it already
# served — keeps the old trip_id in the feed for a while, and every one of
# those sightings used to be free to write more rows against the finished trip.
_finished: set[tuple[str, date]] = set()


# Which rule wrote a row, stored on the event as ``detection_method``.  The
# first three are measurements — the vehicle was seen at, or demonstrably
# travelled across, the stop.  TERMINUS_FALLBACK is not: it is the closest
# approach inside a much wider circle, recorded only because the feed went
# quiet before the vehicle reached the real one, so it is a lower bound on the
# true arrival time.  Keeping the distinction in the row means a later question
# about precision can be answered without re-deriving anything.
DETECTION_GEOFENCE = "geofence"
DETECTION_SEGMENT = "segment"
DETECTION_ORIGIN_DEPARTURE = "origin_departure"
DETECTION_TERMINUS_FALLBACK = "terminus_fallback"

# GTFS route_type: 0 tram/light rail, 1 subway, 2 rail.  3 is bus.
_RAIL_ROUTE_TYPES = frozenset({"0", "1", "2"})
_rail_route_ids: frozenset[str] | None = None


class ActiveTrips:
    """Which trip_ids the feed has carried lately, and when it last did.

    This is the evidence the misassignment guard was missing.  The guard's whole
    premise is that a sighting landing on trip Y's slot might mean our vehicle is
    really running Y under the wrong trip_id — but if some *other* vehicle is out
    there reporting as Y at the same time, that reading is untenable and ours is
    simply a late-running trip.  See ``_build_event``.

    Recency rather than a per-poll snapshot, so the live loop and the backfill
    behave identically: both walk positions in time order, so both populate this
    the same way, and neither needs to see the future.
    """

    def __init__(self, window: timedelta | None = None) -> None:
        self._window = window or timedelta(
            minutes=_settings.arrival_misassignment_active_window_minutes
        )
        self._seen: dict[str, datetime] = {}

    def observe(self, trip_id: str | None, when: datetime) -> None:
        """Note that the feed is carrying this trip."""
        if trip_id:
            previous = self._seen.get(trip_id)
            if previous is None or when > previous:
                self._seen[trip_id] = when

    def observe_all(self, vp_rows: list[dict[str, Any]], default_time: datetime) -> None:
        for row in vp_rows:
            self.observe(row.get("trip_id"), row.get("timestamp") or default_time)

    def is_active(self, trip_id: str, when: datetime) -> bool:
        """Was this trip reporting within the window either side of ``when``?"""
        seen = self._seen.get(trip_id)
        return seen is not None and abs(seen - when) <= self._window

    def prune(self, now: datetime) -> None:
        cutoff = now - self._window
        stale = [tid for tid, seen in self._seen.items() if seen < cutoff]
        for tid in stale:
            del self._seen[tid]

    def clear(self) -> None:
        self._seen.clear()


# The ingest loop's registry.  The backfill script builds its own.
_active_trips = ActiveTrips()


def _rail_routes() -> frozenset[str]:
    """Route ids served by rail, cached on first use."""
    global _rail_route_ids
    if _rail_route_ids is None:
        try:
            routes, _ = load_gtfs_static_data()
            _rail_route_ids = frozenset(
                rid for rid, r in routes.items()
                if r.get("route_type") in _RAIL_ROUTE_TYPES
            )
        except Exception:  # missing/unreadable static feed — fall back to bus radius
            _rail_route_ids = frozenset()
    return _rail_route_ids


def _radius_for(route_id: str | None) -> float:
    """Geofence radius for this route: rail gets the wider one.

    Rail reports position further from the platform than a bus does from the
    kerb, and its stations sit kilometres apart (median 1775 m), so the tight
    bus circle misses arrivals a train plainly made.  Buses stay tighter:
    adjacent bus timepoints get as close as 51 m, where a rail-sized circle
    really would blur neighbouring stops.
    """
    if route_id and route_id in _rail_routes():
        return _settings.arrival_radius_rail_m
    return _settings.arrival_radius_m


def _terminus_fallback_radius_for(route_id: str | None) -> float:
    """Last-resort terminus circle for this route — see TerminusFallbackTracker."""
    if route_id and route_id in _rail_routes():
        return _settings.arrival_terminus_fallback_radius_rail_m
    return _settings.arrival_terminus_fallback_radius_m


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


def _scheduled_utc(service_date: date, arrival_secs: int) -> datetime:
    """Absolute UTC time of a GTFS arrival (seconds-since-local-midnight).

    GTFS allows ``arrival_secs >= 86400`` for trips running past midnight; adding
    a timedelta to the service date's local midnight handles that (and DST)
    correctly before converting to UTC.
    """
    midnight = datetime.combine(service_date, time(0, 0), tzinfo=_DENVER)
    return (midnight + timedelta(seconds=arrival_secs)).astimezone(timezone.utc)


def _project_onto_route(
    lat: float,
    lon: float,
    timepoints: list[tuple[int, str, int, float, float, float]],
) -> tuple[float, float]:
    """Project (lat, lon) onto the inter-timepoint polyline.

    Treats each consecutive pair of timepoints as a straight line segment,
    finds the closest projected point across all segments, and returns:
      (vehicle_dist_m, lateral_dist_m)

    vehicle_dist_m — cumulative route distance of the closest projected point
    lateral_dist_m — perpendicular distance from the route (metres, haversine)

    For a single-timepoint trip the lateral distance is just the haversine to
    that timepoint and vehicle_dist_m is 0.
    """
    if len(timepoints) == 1:
        _, _, _, tp_lat, tp_lon, tp_dist = timepoints[0]
        return tp_dist, _haversine_m(lat, lon, tp_lat, tp_lon)

    best_perp = float("inf")
    best_proj_d = 0.0

    for i in range(len(timepoints) - 1):
        alat, alon, adist = timepoints[i][3], timepoints[i][4], timepoints[i][5]
        blat, blon, bdist = timepoints[i + 1][3], timepoints[i + 1][4], timepoints[i + 1][5]

        dx = blon - alon
        dy = blat - alat
        seg_sq = dx * dx + dy * dy

        t = (
            max(0.0, min(1.0, ((lon - alon) * dx + (lat - alat) * dy) / seg_sq))
            if seg_sq > 1e-18
            else 0.0
        )

        clat = alat + t * dy
        clon = alon + t * dx
        perp = _haversine_m(lat, lon, clat, clon)

        if perp < best_perp:
            best_perp = perp
            best_proj_d = adist + t * (bdist - adist)

    return best_proj_d, best_perp


def _pick_service_date(
    actual_time: datetime, arrival_secs: int
) -> tuple[float, date, datetime]:
    """(delay_seconds, service_date, scheduled_time) for one scheduled stop time.

    Picks the service date whose scheduled time is closest to the observation —
    handles trips whose schedule crosses local midnight.
    """
    local_date = actual_time.astimezone(_DENVER).date()
    best: tuple[float, date, datetime] | None = None
    for sd in (local_date, local_date - timedelta(days=1)):
        scheduled = _scheduled_utc(sd, arrival_secs)
        delay = (actual_time - scheduled).total_seconds()
        if best is None or abs(delay) < abs(best[0]):
            best = (delay, sd, scheduled)
    assert best is not None
    return best


def _build_event(
    *,
    trip_id: str,
    route_id: str | None,
    stop_id: str,
    stop_sequence: int,
    arrival_secs: int,
    actual_time: datetime,
    lat: float,
    lon: float,
    bearing: float | None,
    max_delay_s: int,
    stop_arrivals: dict[tuple[str, str], list[tuple[int, str]]] | None,
    detection_method: str,
    active_trips: ActiveTrips | None = None,
) -> dict[str, Any] | None:
    """Turn a matched (stop, observation time) pair into a stop-event row.

    Shared by arrival and origin-departure detection: both apply the same
    service-date resolution, implausible-match guard and cross-trip
    misassignment check before a row is worth storing.
    """
    delay_seconds, service_date, scheduled_time = _pick_service_date(actual_time, arrival_secs)
    if abs(delay_seconds) > max_delay_s:
        return None

    # Guard against GTFS-RT trip_id misassignment: another trip on the same
    # route may explain this sighting better than the trip_id the feed gave us.
    #
    # This has to be applied narrowly.  ``competing`` holds *every* trip's
    # scheduled arrival at this stop on this route, so on an H-minute headway
    # there is always one about H minutes either side of ours — which means the
    # previous rule (drop when any competing arrival is merely *closer* than our
    # own delay) fired for any bus more than half a headway late.  Measured
    # against RTD's bundled schedule that silently deleted 68% of terminus
    # arrivals for a bus 8 minutes late, and 81% for one 15 minutes late;
    # termini are worst hit because every trip on the route shares that one
    # stop_id, so headways there are the tightest.  The visible damage was
    # stops with no arrival colour on the trip page, trips marked "Incomplete"
    # for want of a terminus arrival, and on-time percentages biased optimistic
    # because it was specifically the late arrivals being thrown away.
    #
    # So: trust the feed's trip_id by default, and only override it on the
    # actual headway-aliasing signature — the sighting landing almost exactly
    # on a competing trip's slot (within ..._max_gap_seconds) while ours would
    # have to be substantially late.  Against the bundled schedule that takes
    # deletions at terminus stops from 64% to 0% for a bus 8 minutes late,
    # while still catching a bus "late" by within a minute of a whole headway.
    #
    # Even that signature is ambiguous on its own, because the two readings it
    # cannot separate are "our vehicle is really running trip Y" and "our
    # vehicle is running our own trip, one whole headway late" — by definition
    # those put the vehicle at the same place at the same time.  What tells them
    # apart is not the schedule at all but the rest of the feed: if Y is itself
    # out on the road being reported by some other vehicle, our vehicle is not
    # Y, and a bus a headway late is exactly what we are looking at.  Measured
    # on stored positions, checking this recovers arrivals for buses a median of
    # 14 minutes down on the frequent routes (15, 15L, SKIP, 107R, FF1) that the
    # bare schedule test was deleting while they sat on the stop.
    if abs(delay_seconds) >= _settings.arrival_misassignment_min_delay_seconds and route_id:
        arrivals = stop_arrivals if stop_arrivals is not None else load_stop_arrivals_index()
        competing = arrivals.get((route_id, stop_id))
        if competing:
            midnight = datetime.combine(service_date, time(0, 0), tzinfo=_DENVER)
            actual_secs = (actual_time.astimezone(_DENVER) - midnight).total_seconds()
            registry = _active_trips if active_trips is None else active_trips
            # Our own scheduled arrival is in this index too, and is excluded by
            # trip_id: a competing *trip* has to be a different one.
            aliases = [
                tid
                for s, tid in competing
                if tid != trip_id
                and abs(s - actual_secs) <= _settings.arrival_misassignment_max_gap_seconds
            ]
            if aliases and not any(registry.is_active(tid, actual_time) for tid in aliases):
                return None

    return {
        "trip_id": trip_id,
        "route_id": route_id,
        "stop_id": stop_id,
        "stop_sequence": stop_sequence,
        "scheduled_time": scheduled_time,
        "actual_time": actual_time,
        "delay_seconds": round(delay_seconds),
        "service_date": service_date,
        "timestamp": actual_time,
        "actual_lat": lat,
        "actual_lon": lon,
        "actual_bearing": bearing,
        "detection_method": detection_method,
    }


def classify_arrival(
    vp_row: dict[str, Any],
    schedule: dict[str, list[tuple[int, str, int, float, float, float]]],
    actual_time: datetime,
    *,
    radius_m: float | None = None,
    max_delay_s: int | None = None,
    stop_arrivals: dict[tuple[str, str], list[tuple[int, str]]] | None = None,
    skip_sequence: int | None = None,
    active_trips: ActiveTrips | None = None,
) -> dict[str, Any] | None:
    """Turn one vehicle position into a stop-arrival event, or ``None``.

    ``schedule`` maps trip_id → list of 6-tuples:
      (stop_sequence, stop_id, arrival_secs, stop_lat, stop_lon, cumulative_dist_m)

    ``stop_arrivals`` maps (route_id, stop_id) → sorted (arrival_secs, trip_id)
    for all trips on that route.  When provided (or loaded from cache when
    ``None``), a substantially late arrival landing almost exactly on another
    trip's slot is suppressed as a probable GTFS-RT trip_id misassignment —
    unless ``active_trips`` says that other trip is itself out on the road, in
    which case ours is an ordinary late bus.  Pass an empty dict to disable the
    check (useful in tests).

    ``skip_sequence`` excludes one stop_sequence from the nearest-timepoint
    search.  Callers pass the trip's origin here: that stop is timed by
    ``OriginDepartureTracker`` (departure, not arrival), and matching it as an
    arrival too would record the layover instead.

    Returns ``None`` when the position has no usable trip/coords, the trip isn't
    in the schedule, the vehicle is more than ``radius_m`` off-route (lateral
    distance), no timepoint is within ``radius_m`` along the route, the closest
    schedule match is implausibly far off (``max_delay_s``), or a better-matching
    trip exists on the same route at this stop.

    Deliberately simple: whichever timepoint the vehicle is geographically
    closest to (by along-route distance) wins the match, provided that's within
    ``radius_m``. GTFS-RT's ``current_status``/``current_stop_sequence`` fields
    are not consulted — RTD's feed often still reports IN_TRANSIT_TO a stop the
    vehicle is already sitting at, so gating on it silently dropped real
    arrivals far more often than it caught an early false one.

    ``delay_seconds`` is positive when late, negative when early.
    """
    if radius_m is None:
        radius_m = _radius_for(vp_row.get("route_id"))
    max_delay_s = _settings.arrival_max_delay_seconds if max_delay_s is None else max_delay_s

    trip_id = vp_row.get("trip_id")
    lat = vp_row.get("latitude")
    lon = vp_row.get("longitude")
    if not trip_id or lat is None or lon is None:
        return None

    timepoints = schedule.get(trip_id)
    if not timepoints:
        return None

    # Project vehicle onto the inter-timepoint route polyline.
    vehicle_dist_m, lateral_m = _project_onto_route(lat, lon, timepoints)

    # Gate on lateral distance: vehicle must be close to the route itself.
    if lateral_m > radius_m:
        return None

    # Nearest timepoint by route-distance, whichever direction it's in.
    best_gap: float | None = None
    best_tp: tuple[int, str, int, float, float, float] | None = None
    for tp in timepoints:
        seq, _, _, _, _, tp_dist_m = tp
        if skip_sequence is not None and seq == skip_sequence:
            continue
        gap = abs(vehicle_dist_m - tp_dist_m)
        if best_gap is None or gap < best_gap:
            best_gap, best_tp = gap, tp

    if best_tp is None or best_gap > radius_m:
        return None

    seq, stop_id, arrival_secs, _, _, _ = best_tp

    return _build_event(
        trip_id=trip_id,
        route_id=vp_row.get("route_id"),
        stop_id=stop_id,
        stop_sequence=seq,
        arrival_secs=arrival_secs,
        actual_time=actual_time,
        lat=lat,
        lon=lon,
        bearing=vp_row.get("bearing"),
        max_delay_s=max_delay_s,
        stop_arrivals=stop_arrivals,
        detection_method=DETECTION_GEOFENCE,
        active_trips=active_trips,
    )


# ── Pass-through arrivals (between two fixes) ────────────────────────────────
#
# ``classify_arrival`` tests one fix at a time, so it can only see a stop the
# vehicle happened to be *inside the geofence for* at the instant the feed
# sampled it.  RTD's positions are only ~30 s apart, and a bus at 35 mph covers
# ~450 m in that time, so a bus that doesn't actually stop (nobody boarding at
# a timepoint) routinely steps straight over the ~152 m-wide circle: fix at
# 125 ft before, next fix at 125 ft after, no arrival recorded.  The trip
# replay interpolates between fixes, which is why the map plainly shows the bus
# reaching the stop that the table left uncoloured.
#
# The fix is to match the *interval* between two consecutive fixes rather than
# the fixes themselves.  Each fix already projects to a distance along the
# route, so a timepoint whose own route distance falls between them was passed,
# and the crossing time interpolates by distance across the gap — 125 ft either
# side puts the arrival at the midpoint of the two timestamps.  Note this needs
# no geofence radius at all: the bus need only have gone past the stop, so
# widening ``arrival_radius_m`` (which costs accuracy) is not the lever.

# How far off the inter-timepoint chord a fix may sit and still count as being
# on this corridor.  Looser than the arrival radius on purpose: the chord cuts
# corners the road does not, so a fix mid-way between two timepoints a few km
# apart is legitimately far from the straight line between them.
_SEGMENT_CORRIDOR_FACTOR = 3.0


def classify_segment_arrivals(
    prev_row: dict[str, Any],
    prev_time: datetime,
    vp_row: dict[str, Any],
    actual_time: datetime,
    schedule: dict[str, list[tuple[int, str, int, float, float, float]]],
    *,
    radius_m: float | None = None,
    max_delay_s: int | None = None,
    stop_arrivals: dict[tuple[str, str], list[tuple[int, str]]] | None = None,
    skip_sequence: int | None = None,
    active_trips: ActiveTrips | None = None,
) -> list[dict[str, Any]]:
    """Timepoints the vehicle passed *between* two consecutive fixes.

    Both fixes must belong to the same trip, be close enough in time to read as
    one continuous movement (``arrival_segment_max_gap_seconds``), sit on the
    route corridor, and show forward progress along it.  Every timepoint whose
    route distance falls in ``(prev, current]`` yields an event timed by linear
    interpolation across the gap; position and bearing are interpolated too, so
    ``actual_lat``/``actual_lon`` land where the replay draws the bus.

    Returns events oldest-first.  Callers de-duplicate against
    ``classify_arrival``'s own matches — see ``detect_arrivals``.
    """
    route_id = vp_row.get("route_id") or prev_row.get("route_id")
    if radius_m is None:
        radius_m = _radius_for(route_id)
    max_delay_s = _settings.arrival_max_delay_seconds if max_delay_s is None else max_delay_s

    trip_id = vp_row.get("trip_id")
    if not trip_id or prev_row.get("trip_id") != trip_id:
        return []

    lat, lon = vp_row.get("latitude"), vp_row.get("longitude")
    plat, plon = prev_row.get("latitude"), prev_row.get("longitude")
    if lat is None or lon is None or plat is None or plon is None:
        return []

    span_s = (actual_time - prev_time).total_seconds()
    if span_s <= 0 or span_s > _settings.arrival_segment_max_gap_seconds:
        return []

    timepoints = schedule.get(trip_id)
    if not timepoints:
        return []

    prev_dist, prev_lateral = _project_onto_route(plat, plon, timepoints)
    curr_dist, curr_lateral = _project_onto_route(lat, lon, timepoints)

    corridor_m = radius_m * _SEGMENT_CORRIDOR_FACTOR
    if prev_lateral > corridor_m or curr_lateral > corridor_m:
        return []

    # Only forward travel: a backwards step is projection noise (or a loop
    # doubling back), and `classify_arrival` still covers a fix sitting at a
    # stop.  Equal distances mean a stationary vehicle — nothing was passed.
    if curr_dist <= prev_dist:
        return []

    span_m = curr_dist - prev_dist
    events: list[dict[str, Any]] = []
    for seq, stop_id, arrival_secs, _, _, tp_dist_m in timepoints:
        if skip_sequence is not None and seq == skip_sequence:
            continue
        if not (prev_dist < tp_dist_m <= curr_dist):
            continue

        frac = (tp_dist_m - prev_dist) / span_m
        event = _build_event(
            trip_id=trip_id,
            route_id=route_id,
            stop_id=stop_id,
            stop_sequence=seq,
            arrival_secs=arrival_secs,
            actual_time=prev_time + (actual_time - prev_time) * frac,
            lat=plat + (lat - plat) * frac,
            lon=plon + (lon - plon) * frac,
            bearing=vp_row.get("bearing"),
            max_delay_s=max_delay_s,
            stop_arrivals=stop_arrivals,
            detection_method=DETECTION_SEGMENT,
            active_trips=active_trips,
        )
        if event is not None:
            events.append(event)

    return events


# ── Origin departures ────────────────────────────────────────────────────────
#
# At every stop but the first, the arrival is the event worth timing: the vehicle
# shows up, dwells ~20 s, leaves.  The origin terminal is the opposite.  A bus
# lays over at the gate — already carrying its *next* trip_id in the GTFS-RT feed
# — for minutes before it pulls out, so its first geofenced snapshot there times
# the layover, not the trip.  (Observed: an FF1 leaving Downtown Boulder Station
# Gate 1 at 18:30 sharp was logged as departing at 18:27.)
#
# So the origin is timed by *departure*: the moment the vehicle crossed
# ``origin_departure_radius_m`` from the origin stop, linearly interpolated
# between the last snapshot inside that circle and the first one outside.  The
# interpolation is what keeps this accurate — at a 30 s poll interval, taking
# either raw snapshot instead would be up to half a minute off, and the two
# biases it splits (the vehicle sitting still for part of the gap, then
# accelerating away) largely cancel.
#
# Straight-line distance from the stop, not route distance, is deliberate: a
# vehicle parked in a bay or staging behind the stop projects onto the route
# corridor unpredictably, but "how far from the gate" is unambiguous.


@dataclass
class _PendingDeparture:
    """A vehicle seen at its trip's origin, waiting to be seen leaving."""

    trip_id: str
    route_id: str | None
    stop_id: str
    stop_sequence: int
    arrival_secs: int
    service_date: date
    # Latest snapshot still inside the departure circle — the interpolation's
    # inner endpoint, and the position recorded on the event.
    last_inside_time: datetime
    last_inside_dist_m: float
    lat: float
    lon: float
    bearing: float | None
    # First snapshot seen *outside* the circle since the vehicle was last at the
    # stop — the interpolation's outer endpoint.  Held separately from whichever
    # later fix finally confirms the direction of travel: the crossing happened
    # between the last inside fix and the first outside one, so interpolating
    # against a fix two or three polls further down the road would drag the
    # departure time late.  Cleared whenever the vehicle is seen inside again.
    first_outside_time: datetime | None = None
    first_outside_dist_m: float | None = None
    # Set when the vehicle has left the circle without making progress down the
    # route — provisionally a yard move.  Not fatal (see ``feed``), but it stops
    # ``flush`` from later passing the move off as a departure.
    left_without_progress: bool = False


def _interpolate_crossing(
    t_in: datetime,
    d_in: float,
    t_out: datetime,
    d_out: float,
    radius_m: float,
) -> datetime:
    """When the vehicle crossed ``radius_m``, between an inside and outside fix.

    Linear in distance over the gap between the two snapshots, clamped to the
    gap itself so a GPS jump far from the stop can't project the crossing
    outside the interval we actually observed.
    """
    span = d_out - d_in
    if span <= 0:
        return t_out
    frac = max(0.0, min(1.0, (radius_m - d_in) / span))
    return t_in + (t_out - t_in) * frac


class OriginDepartureTracker:
    """Streams positions in time order and emits one departure per trip origin.

    Unlike ``classify_arrival`` this cannot be a pure per-position function: a
    departure is only knowable from *two* snapshots, one at the stop and one
    away from it.  Feed every position for a poll (or a backfill batch) through
    ``feed``, then call ``flush`` with the current watermark.

    The same instance is reused across polls by ``detect_arrivals``; the backfill
    script builds its own.  State is small — one entry per trip currently sitting
    at its origin, plus the (trip, stop, date) keys already emitted.
    """

    def __init__(
        self,
        origins: dict[str, tuple[int, str, int, float, float]] | None = None,
        *,
        schedule: dict[str, list[tuple[int, str, int, float, float, float]]] | None = None,
        radius_m: float | None = None,
        max_delay_s: int | None = None,
        stop_arrivals: dict[tuple[str, str], list[tuple[int, str]]] | None = None,
        stale_after: timedelta | None = None,
        active_trips: ActiveTrips | None = None,
    ) -> None:
        self._origins = origins
        self._schedule = schedule
        self._radius_m = (
            _settings.origin_departure_radius_m if radius_m is None else radius_m
        )
        self._max_delay_s = (
            _settings.arrival_max_delay_seconds if max_delay_s is None else max_delay_s
        )
        self._stop_arrivals = stop_arrivals
        self._active_trips = active_trips
        self._stale_after = stale_after or timedelta(
            minutes=_settings.origin_departure_stale_minutes
        )
        self._pending: dict[tuple[str, date], _PendingDeparture] = {}
        # (trip_id, stop_sequence, service_date) already emitted — stops a loop
        # route that passes its origin again from re-arming.
        self._done: set[tuple[str, int, date]] = set()

    @property
    def origins(self) -> dict[str, tuple[int, str, int, float, float]]:
        if self._origins is None:
            self._origins = load_trip_origin_timepoints()
        return self._origins

    @property
    def schedule(self) -> dict[str, list[tuple[int, str, int, float, float, float]]]:
        """Route geometry, used to tell a departure from a move to the yard."""
        if self._schedule is None:
            self._schedule = load_trip_shape_dist_schedule()
        return self._schedule

    def _left_along_the_route(self, trip_id: str, seq: int, lat: float, lon: float) -> bool:
        """Did this vehicle leave the origin circle *onward down the route*?

        Leaving the circle is not the same as departing.  A rail car heading
        back to the yard also clears it — backwards, or off the corridor
        entirely — and used to be recorded as the trip's departure.  Projecting
        onto the route separates the two: onward travel advances the along-route
        distance past the origin, while a reverse or sideways move clamps to
        roughly the origin's own distance (``_project_onto_route`` bounds the
        projection to the polyline, so there is no negative progress to read).

        Trips with fewer than two timepoints carry no direction to test, so they
        keep the old leave-the-circle behaviour.

        Note this is a *provisional* test, asked again on each later fix — see
        ``feed``.  Progress is measured against the straight chord from the
        origin to the next timepoint, which may be kilometres away and point
        somewhere the first block of the route does not, so the answer on the
        first fix outside the circle is frequently a false negative.
        """
        timepoints = self.schedule.get(trip_id)
        if not timepoints or len(timepoints) < 2:
            return True
        vehicle_dist_m, _ = _project_onto_route(lat, lon, timepoints)
        origin_dist_m = next(
            (tp[5] for tp in timepoints if tp[0] == seq), timepoints[0][5]
        )
        # Half the circle: a vehicle that genuinely pulled out is a full radius
        # beyond the stop, while jitter or a sideways move barely registers.
        return (vehicle_dist_m - origin_dist_m) >= self._radius_m * 0.5

    def feed(self, vp_row: dict[str, Any], actual_time: datetime) -> dict[str, Any] | None:
        """Absorb one position; return a departure event when one just resolved."""
        trip_id = vp_row.get("trip_id")
        lat = vp_row.get("latitude")
        lon = vp_row.get("longitude")
        if not trip_id or lat is None or lon is None:
            return None

        origin = self.origins.get(trip_id)
        if origin is None:
            return None
        seq, stop_id, arrival_secs, origin_lat, origin_lon = origin

        _, service_date, _ = _pick_service_date(actual_time, arrival_secs)
        if (trip_id, seq, service_date) in self._done:
            return None

        dist_m = _haversine_m(lat, lon, origin_lat, origin_lon)
        key = (trip_id, service_date)
        pending = self._pending.get(key)

        if dist_m <= self._radius_m:
            # Still at the origin — keep the newest fix as the inner endpoint.
            if pending is None:
                pending = _PendingDeparture(
                    trip_id=trip_id,
                    route_id=vp_row.get("route_id"),
                    stop_id=stop_id,
                    stop_sequence=seq,
                    arrival_secs=arrival_secs,
                    service_date=service_date,
                    last_inside_time=actual_time,
                    last_inside_dist_m=dist_m,
                    lat=lat,
                    lon=lon,
                    bearing=vp_row.get("bearing"),
                )
                self._pending[key] = pending
            else:
                pending.route_id = vp_row.get("route_id") or pending.route_id
                pending.last_inside_time = actual_time
                pending.last_inside_dist_m = dist_m
                pending.lat, pending.lon = lat, lon
                pending.bearing = vp_row.get("bearing")
                # Back at the gate: whatever it did out there, this is the new
                # inner endpoint and the excursion no longer counts against it.
                pending.first_outside_time = None
                pending.first_outside_dist_m = None
                pending.left_without_progress = False
            return None

        # Outside the circle.  Without a sighting at the stop we have no inner
        # endpoint and no idea when it left — the trip_id was attached too late.
        if pending is None:
            return None

        # …but the feed still placing the vehicle at or before the origin means
        # it hasn't started: at a big station (Union, Downtown Boulder) the gates
        # are far enough apart that repositioning between them clears the circle.
        # Wait for a later fix rather than timing the shuffle as a departure.
        current_stop_seq = vp_row.get("current_stop_sequence")
        if current_stop_seq is not None and current_stop_seq <= seq:
            return None

        # First fix outside the circle since the vehicle was last at the stop:
        # remember it as the interpolation's outer endpoint, whether or not this
        # is the fix that settles the direction question below.
        if pending.first_outside_time is None:
            pending.first_outside_time = actual_time
            pending.first_outside_dist_m = dist_m

        if not self._left_along_the_route(trip_id, seq, lat, lon):
            # Left the circle without advancing down the route.  That is the
            # signature of a yard move — but it is also what a perfectly ordinary
            # departure looks like on its *first* fix outside the circle, because
            # along-route progress is measured against the chord to the next
            # timepoint (often kilometres away, and rarely in the direction of
            # the first block or two of actual driving).  A bus pulling east out
            # of a station bay before turning north up its route projects to zero
            # progress, and deciding here used to delete it: 18% of runs lost
            # their origin that way, 79% of them reading *exactly* zero progress
            # while sitting a median of 163 m from the stop — barely outside the
            # circle rather than off to the yard.
            #
            # So this is not the moment to decide.  Keep the pending entry and
            # ask again on the next fix, by which point a real departure has
            # unambiguously advanced.  The only thing the failed test costs is
            # the right to resolve via `flush`: a vehicle that leaves without
            # ever making progress and then goes quiet really did go to the yard,
            # and must not be written up as a departure.
            pending.left_without_progress = True
            return None

        del self._pending[key]
        self._done.add((trip_id, seq, service_date))
        departed_at = _interpolate_crossing(
            pending.last_inside_time,
            pending.last_inside_dist_m,
            pending.first_outside_time or actual_time,
            pending.first_outside_dist_m if pending.first_outside_dist_m is not None else dist_m,
            self._radius_m,
        )
        return self._emit(pending, departed_at)

    def flush(self, now: datetime, *, force: bool = False) -> list[dict[str, Any]]:
        """Resolve origins we never saw leave, once the trip has gone quiet.

        A trip can vanish from the feed mid-layover (cancelled, or the vehicle
        reassigned).  Rather than drop the event, record the last moment it was
        seen at the stop — a lower bound, and still far closer than the first
        sighting was.  ``force`` drains everything, for end-of-backfill.

        A trip last seen *outside* the circle having made no progress down the
        route is the exception: that is a vehicle which left for the yard, not
        one that never left at all, so its entry is discarded rather than timed.
        It is not marked done, so a vehicle that comes back and pulls out
        properly still re-arms.
        """
        out: list[dict[str, Any]] = []
        for key, pending in list(self._pending.items()):
            if not force and now - pending.last_inside_time < self._stale_after:
                continue
            del self._pending[key]
            if pending.left_without_progress:
                continue
            self._done.add((pending.trip_id, pending.stop_sequence, pending.service_date))
            event = self._emit(pending, pending.last_inside_time)
            if event is not None:
                out.append(event)
        self._prune_done(now.astimezone(_DENVER).date())
        return out

    def _emit(self, pending: _PendingDeparture, departed_at: datetime) -> dict[str, Any] | None:
        return _build_event(
            trip_id=pending.trip_id,
            route_id=pending.route_id,
            stop_id=pending.stop_id,
            stop_sequence=pending.stop_sequence,
            arrival_secs=pending.arrival_secs,
            actual_time=departed_at,
            lat=pending.lat,
            lon=pending.lon,
            bearing=pending.bearing,
            max_delay_s=self._max_delay_s,
            stop_arrivals=self._stop_arrivals,
            detection_method=DETECTION_ORIGIN_DEPARTURE,
            active_trips=self._active_trips,
        )

    def _prune_done(self, today: date) -> None:
        cutoff = today - timedelta(days=1)
        if any(sd < cutoff for _, _, sd in self._done):
            self._done.difference_update({k for k in self._done if k[2] < cutoff})


# ── Terminus fallback ────────────────────────────────────────────────────────
#
# A trip is judged "complete" by one thing: an arrival recorded at its final
# timepoint.  So the failure mode with the loudest symptom is a vehicle whose
# feed simply stops a few hundred metres short of the end of the line — parked
# at the terminal but no longer reporting, or dropped by the feed on approach.
# Nothing later can rescue it, the terminus stays blank, and the trip is filed
# as "Incomplete" even though the replay plainly shows it almost there.
#
# Widening ``arrival_radius_m`` is the wrong lever: it is used at *every*
# timepoint, so paying for these at the ordinary radius would blur every
# mid-route stop on the system.  Instead the terminus — and only the terminus —
# gets a second, much wider circle, consulted only after the trip has gone
# quiet and only when the ordinary circle never fired there.  The arrival
# recorded is the vehicle's *closest approach*, which is a lower bound on the
# real arrival: it stops short by however long the vehicle still needed for the
# remaining distance.  That is why the row is stamped TERMINUS_FALLBACK rather
# than passed off as a measurement.
#
# The wide circle is capped at the distance from the second-to-last timepoint,
# so it can never grow far enough to mistake the previous stop for the last
# one, and a candidate fix must also have projected past that timepoint along
# the route — which is what keeps a loop route that passes near its own
# terminus early in the trip from resolving on the way out.


@dataclass
class _TerminusApproach:
    """A trip's closest sighting to its final timepoint, so far."""

    trip_id: str
    route_id: str | None
    stop_id: str
    stop_sequence: int
    arrival_secs: int
    service_date: date
    best_dist_m: float
    best_time: datetime
    lat: float
    lon: float
    bearing: float | None
    last_seen: datetime


class TerminusFallbackTracker:
    """Streams positions and rescues terminus arrivals the geofence missed.

    ``feed`` never returns an event: whether a trip ended short is only knowable
    once it stops reporting, so events come out of ``flush`` after the trip has
    been silent for ``arrival_terminus_fallback_stale_minutes``.  A trip seen
    inside the *ordinary* terminus geofence is dropped from tracking on the
    spot — the normal path has it, and this is strictly a fallback.

    The same instance is reused across polls by ``detect_arrivals``; the
    backfill script builds its own.
    """

    def __init__(
        self,
        schedule: dict[str, list[tuple[int, str, int, float, float, float]]] | None = None,
        *,
        radius_m: float | None = None,
        max_delay_s: int | None = None,
        stop_arrivals: dict[tuple[str, str], list[tuple[int, str]]] | None = None,
        stale_after: timedelta | None = None,
        active_trips: ActiveTrips | None = None,
    ) -> None:
        self._schedule = schedule
        self._radius_m = radius_m
        self._max_delay_s = (
            _settings.arrival_max_delay_seconds if max_delay_s is None else max_delay_s
        )
        self._stop_arrivals = stop_arrivals
        self._active_trips = active_trips
        self._stale_after = stale_after or timedelta(
            minutes=_settings.arrival_terminus_fallback_stale_minutes
        )
        self._pending: dict[tuple[str, date], _TerminusApproach] = {}
        # Runs that need no fallback — either already emitted one, or seen
        # inside the ordinary geofence, which means the normal path has it.
        self._done: set[tuple[str, date]] = set()

    @property
    def schedule(self) -> dict[str, list[tuple[int, str, int, float, float, float]]]:
        if self._schedule is None:
            self._schedule = load_trip_shape_dist_schedule()
        return self._schedule

    def _fallback_radius(
        self,
        route_id: str | None,
        timepoints: list[tuple[int, str, int, float, float, float]],
    ) -> float:
        """The wide circle for this trip's terminus, capped at half the last leg.

        Whatever the configured radius, the circle never extends past the
        midpoint between the last two timepoints, so every fix inside it is
        unambiguously nearer the terminus than the stop before it — the same
        nearest-timepoint rule the ordinary detector uses.  Without the cap, a
        vehicle that actually died at the second-to-last stop would be credited
        with reaching the end of the line.

        RTD's final legs are long (median 1.7 km for bus, 1.9 km for rail), so
        the cap binds only where the last two stops really are close together.
        """
        configured = (
            _terminus_fallback_radius_for(route_id)
            if self._radius_m is None
            else self._radius_m
        )
        if len(timepoints) < 2:
            return configured
        last_leg_m = timepoints[-1][5] - timepoints[-2][5]
        return min(configured, last_leg_m / 2)

    def feed(self, vp_row: dict[str, Any], actual_time: datetime) -> None:
        """Absorb one position, keeping the closest approach to the terminus."""
        trip_id = vp_row.get("trip_id")
        lat = vp_row.get("latitude")
        lon = vp_row.get("longitude")
        if not trip_id or lat is None or lon is None:
            return

        timepoints = self.schedule.get(trip_id)
        if not timepoints:
            return
        seq, stop_id, arrival_secs, term_lat, term_lon, _ = timepoints[-1]

        _, service_date, _ = _pick_service_date(actual_time, arrival_secs)
        key = (trip_id, service_date)
        if key in self._done:
            return

        route_id = vp_row.get("route_id")
        dist_m = _haversine_m(lat, lon, term_lat, term_lon)

        # Inside the ordinary geofence: classify_arrival owns this one.  Stop
        # tracking rather than carrying a candidate the dedup would discard.
        if dist_m <= _radius_for(route_id):
            self._done.add(key)
            self._pending.pop(key, None)
            return

        pending = self._pending.get(key)
        if pending is not None:
            pending.last_seen = actual_time
            pending.route_id = route_id or pending.route_id

        if dist_m > self._fallback_radius(route_id, timepoints):
            return
        # Must be *approaching* the end of the line, not merely near it: a loop
        # route passes its own terminus on the way out, and a vehicle staging
        # nearby before the trip starts has not arrived at anything.
        if len(timepoints) >= 2:
            vehicle_dist_m, _ = _project_onto_route(lat, lon, timepoints)
            if vehicle_dist_m < timepoints[-2][5]:
                return

        if pending is None:
            self._pending[key] = _TerminusApproach(
                trip_id=trip_id,
                route_id=route_id,
                stop_id=stop_id,
                stop_sequence=seq,
                arrival_secs=arrival_secs,
                service_date=service_date,
                best_dist_m=dist_m,
                best_time=actual_time,
                lat=lat,
                lon=lon,
                bearing=vp_row.get("bearing"),
                last_seen=actual_time,
            )
        elif dist_m < pending.best_dist_m:
            pending.best_dist_m = dist_m
            pending.best_time = actual_time
            pending.lat, pending.lon = lat, lon
            pending.bearing = vp_row.get("bearing")

    def forget(self, trip_id: str, service_date: date) -> None:
        """Drop a run whose terminus arrival has been recorded by another rule."""
        key = (trip_id, service_date)
        self._done.add(key)
        self._pending.pop(key, None)

    def flush(self, now: datetime, *, force: bool = False) -> list[dict[str, Any]]:
        """Emit fallback arrivals for trips that have gone quiet on approach."""
        out: list[dict[str, Any]] = []
        for key, pending in list(self._pending.items()):
            if not force and now - pending.last_seen < self._stale_after:
                continue
            del self._pending[key]
            self._done.add(key)
            event = _build_event(
                trip_id=pending.trip_id,
                route_id=pending.route_id,
                stop_id=pending.stop_id,
                stop_sequence=pending.stop_sequence,
                arrival_secs=pending.arrival_secs,
                actual_time=pending.best_time,
                lat=pending.lat,
                lon=pending.lon,
                bearing=pending.bearing,
                max_delay_s=self._max_delay_s,
                stop_arrivals=self._stop_arrivals,
                detection_method=DETECTION_TERMINUS_FALLBACK,
                active_trips=self._active_trips,
            )
            if event is not None:
                out.append(event)
        self._prune_done(now.astimezone(_DENVER).date())
        return out

    def _prune_done(self, today: date) -> None:
        cutoff = today - timedelta(days=1)
        if any(sd < cutoff for _, sd in self._done):
            self._done.difference_update({k for k in self._done if k[1] < cutoff})


def _prune_recorded(today: date) -> None:
    """Drop dedup keys older than yesterday so the set stays small."""
    cutoff = today - timedelta(days=1)
    if any(sd < cutoff for _, _, sd in _recorded):
        stale = {k for k in _recorded if k[2] < cutoff}
        _recorded.difference_update(stale)


def _prune_finished(today: date) -> None:
    """Drop finished-run keys older than yesterday, alongside _recorded."""
    cutoff = today - timedelta(days=1)
    stale = {k for k in _finished if k[1] < cutoff}
    _finished.difference_update(stale)


def _prune_last_fix(now: datetime) -> None:
    """Forget trips that have gone quiet.

    Anything older than the interpolation window can never pair with a new fix
    (``classify_segment_arrivals`` rejects the gap), so holding it only grows
    the dict — one entry per trip seen, for the life of the process.
    """
    cutoff = now - timedelta(seconds=_settings.arrival_segment_max_gap_seconds)
    stale = [tid for tid, (_, seen) in _last_fix.items() if seen < cutoff]
    for tid in stale:
        del _last_fix[tid]


def _live_tracker(
    origins: dict[str, tuple[int, str, int, float, float]],
    arrivals_index: dict[tuple[str, str], list[tuple[int, str]]],
) -> OriginDepartureTracker:
    """The ingest loop's tracker — one instance, so dwells span polls."""
    global _live_tracker_instance
    if _live_tracker_instance is None:
        _live_tracker_instance = OriginDepartureTracker(
            origins, stop_arrivals=arrivals_index, active_trips=_active_trips
        )
    return _live_tracker_instance


def _live_terminus(
    schedule: dict[str, list[tuple[int, str, int, float, float, float]]],
    arrivals_index: dict[tuple[str, str], list[tuple[int, str]]],
) -> TerminusFallbackTracker:
    """The ingest loop's terminus tracker — one instance, so approaches persist."""
    global _live_terminus_tracker
    if _live_terminus_tracker is None:
        _live_terminus_tracker = TerminusFallbackTracker(
            schedule, stop_arrivals=arrivals_index, active_trips=_active_trips
        )
    return _live_terminus_tracker


def detect_arrivals(
    vp_rows: list[dict[str, Any]],
    default_time: datetime,
) -> list[dict[str, Any]]:
    """Derive de-duplicated stop events for one poll's positions.

    Mid-route timepoints yield arrivals; each trip's origin yields a departure
    once the vehicle is seen leaving (so an origin event usually lands a poll or
    two after the vehicle was last at the gate).

    Each row's own ``timestamp`` is used as the observation time when present
    (so the same code backfills historical positions); ``default_time`` is the
    fallback.
    """
    schedule = load_trip_shape_dist_schedule()
    origins = load_trip_origin_timepoints()
    if not schedule and not origins:
        return []

    arrivals_index = load_stop_arrivals_index()
    tracker = _live_tracker(origins, arrivals_index)
    terminus = _live_terminus(schedule, arrivals_index)

    # Register every trip in this poll *before* classifying any of it, so the
    # misassignment guard can see the whole feed cycle rather than only the
    # trips that happen to sort earlier in the batch.
    _active_trips.observe_all(vp_rows, default_time)

    candidates: list[dict[str, Any]] = []
    for row in vp_rows:
        actual = row.get("timestamp") or default_time
        terminus.feed(row, actual)
        departure = tracker.feed(row, actual)
        if departure is not None:
            candidates.append(departure)
        origin = origins.get(row.get("trip_id") or "")
        skip_sequence = origin[0] if origin else None

        # Stops passed since this trip's previous fix, timed by interpolation.
        # Listed before the point match so that when both see the same stop,
        # the interpolated crossing time wins the de-dup below.
        trip_id = row.get("trip_id")
        previous = _last_fix.get(trip_id) if trip_id else None
        if previous is not None:
            candidates.extend(
                classify_segment_arrivals(
                    previous[0],
                    previous[1],
                    row,
                    actual,
                    schedule,
                    stop_arrivals=arrivals_index,
                    skip_sequence=skip_sequence,
                    active_trips=_active_trips,
                )
            )
        if trip_id:
            _last_fix[trip_id] = (row, actual)

        arrival = classify_arrival(
            row,
            schedule,
            actual,
            stop_arrivals=arrivals_index,
            skip_sequence=skip_sequence,
            active_trips=_active_trips,
        )
        if arrival is not None:
            candidates.append(arrival)

    candidates.extend(tracker.flush(default_time))
    # Last, so a terminus arrival found by any of the rules above closes the run
    # first and this poll's fallback candidates for it fall out below.
    candidates.extend(terminus.flush(default_time))

    events: list[dict[str, Any]] = []
    for event in candidates:
        trip_id = event["trip_id"]
        run = (trip_id, event["service_date"])
        if run in _finished:
            continue
        key = (trip_id, event["stop_sequence"], event["service_date"])
        if key in _recorded:
            continue
        _recorded.add(key)
        events.append(event)
        timepoints = schedule.get(trip_id)
        if timepoints and event["stop_sequence"] == timepoints[-1][0]:
            _finished.add(run)
            # The run is over — release the fallback's state for it rather than
            # letting it carry an approach that could never be emitted.
            terminus.forget(trip_id, event["service_date"])

    today = default_time.astimezone(_DENVER).date()
    _prune_recorded(today)
    _prune_finished(today)
    _prune_last_fix(default_time)
    _active_trips.prune(default_time)
    return events


def reset_detection_state() -> None:
    """Drop all in-process dedup/pending state (tests; not used in production)."""
    global _live_tracker_instance, _live_terminus_tracker
    _recorded.clear()
    _finished.clear()
    _last_fix.clear()
    _active_trips.clear()
    _live_tracker_instance = None
    _live_terminus_tracker = None
