"""Unit tests for observed on-time classification (app/services/ontime.py).

Pure functions — no DB, no GTFS filesystem. We hand-build a one-trip timepoint
schedule in the 6-tuple format (seq, stop_id, arr_secs, lat, lon, dist_m) and
assert the route-corridor projection + delay + service-date logic.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from app.services.ontime import (
    ActiveTrips,
    OriginDepartureTracker,
    TerminusFallbackTracker,
    _haversine_m,
    _project_onto_route,
    _scheduled_utc,
    classify_arrival,
    classify_segment_arrivals,
    detect_arrivals,
    reset_detection_state,
)

# A single trip with one timepoint at Denver Union Station, scheduled 08:00 local.
_STOP_LAT, _STOP_LON = 39.7392, -104.9903
_ARR_SECS = 8 * 3600  # 08:00:00
# 6-tuple: (seq, stop_id, arr_secs, lat, lon, cumulative_dist_m)
_SCHEDULE: dict = {"T1": [(5, "S1", _ARR_SECS, _STOP_LAT, _STOP_LON, 0.0)]}
_SERVICE_DATE = date(2026, 6, 20)


def _vp(lat=_STOP_LAT, lon=_STOP_LON, trip_id="T1", route_id="R1"):
    return {"trip_id": trip_id, "route_id": route_id, "latitude": lat, "longitude": lon}


def test_haversine_known_distance():
    # ~0.0009 deg longitude at this latitude is ~77m; sanity-check the metric.
    d = _haversine_m(_STOP_LAT, _STOP_LON, _STOP_LAT, _STOP_LON + 0.0009)
    assert 60 < d < 95


def test_project_onto_route_single_point():
    # Single-point schedule: lateral dist is haversine to the stop.
    tps = [(5, "S1", _ARR_SECS, _STOP_LAT, _STOP_LON, 0.0)]
    proj_d, lateral = _project_onto_route(_STOP_LAT, _STOP_LON, tps)
    assert proj_d == 0.0
    assert lateral < 1.0  # essentially zero at the exact stop location


def test_project_onto_route_two_points():
    # Vehicle halfway between two timepoints should land at ~half the segment dist.
    lat_a, lon_a = 39.730, -104.990
    lat_b, lon_b = 39.740, -104.990
    seg_len = _haversine_m(lat_a, lon_a, lat_b, lon_b)
    tps = [
        (1, "A", 0, lat_a, lon_a, 0.0),
        (2, "B", 600, lat_b, lon_b, seg_len),
    ]
    mid_lat = (lat_a + lat_b) / 2
    proj_d, lateral = _project_onto_route(mid_lat, lon_a, tps)
    assert abs(proj_d - seg_len / 2) < 10  # within 10 m of midpoint
    assert lateral < 5  # on the line


def test_on_time_arrival():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled + timedelta(seconds=40)
    event = classify_arrival(_vp(), _SCHEDULE, actual)
    assert event is not None
    assert event["stop_id"] == "S1"
    assert event["stop_sequence"] == 5
    assert event["route_id"] == "R1"
    assert event["delay_seconds"] == 40
    assert event["service_date"] == _SERVICE_DATE


def test_arrival_includes_actual_position():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    event = classify_arrival(
        {**_vp(), "bearing": 270.0},
        _SCHEDULE,
        scheduled,
    )
    assert event is not None
    assert event["actual_lat"] == _STOP_LAT
    assert event["actual_lon"] == _STOP_LON
    assert event["actual_bearing"] == 270.0


def test_actual_bearing_none_when_absent():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    event = classify_arrival(_vp(), _SCHEDULE, scheduled)
    assert event is not None
    assert event["actual_bearing"] is None


def test_late_arrival_positive_delay():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    event = classify_arrival(_vp(), _SCHEDULE, scheduled + timedelta(seconds=180))
    assert event is not None and event["delay_seconds"] == 180  # 3 min late


def test_early_arrival_negative_delay():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    event = classify_arrival(_vp(), _SCHEDULE, scheduled - timedelta(seconds=240))
    assert event is not None and event["delay_seconds"] == -240  # 4 min early


def test_lateral_distance_rejects_off_route_vehicle():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    far = _vp(lat=_STOP_LAT + 0.01)  # ~1.1 km north — well outside the 152 m lateral radius
    assert classify_arrival(far, _SCHEDULE, scheduled) is None


def test_unknown_trip_returns_none():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    assert classify_arrival(_vp(trip_id="ghost"), _SCHEDULE, scheduled) is None


def test_missing_coords_returns_none():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    assert classify_arrival({"trip_id": "T1", "latitude": None, "longitude": None},
                            _SCHEDULE, scheduled) is None


def test_implausible_match_dropped():
    # Vehicle at the stop but observed 5 hours off any plausible schedule.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    assert classify_arrival(_vp(), _SCHEDULE, scheduled + timedelta(hours=5)) is None


def test_after_midnight_picks_prior_service_date():
    # GTFS arrival 25:10 (01:10 next calendar day) belongs to 2026-06-20 service.
    schedule: dict = {"T1": [(5, "S1", 25 * 3600 + 10 * 60, _STOP_LAT, _STOP_LON, 0.0)]}
    scheduled = _scheduled_utc(_SERVICE_DATE, 25 * 3600 + 10 * 60)
    actual = scheduled + timedelta(seconds=20)
    event = classify_arrival(_vp(), schedule, actual)
    assert event is not None
    assert event["service_date"] == _SERVICE_DATE  # not the calendar date of `actual`
    assert abs(event["delay_seconds"]) < 60


def test_geofence_hit_fires_regardless_of_in_transit_status():
    # RTD's feed commonly still reports IN_TRANSIT_TO a stop the vehicle is
    # already sitting at (current_stop_sequence only advances once it pulls
    # away), so current_status/current_stop_sequence must NOT suppress an
    # otherwise-valid geofence match — that was the bug that left stops with a
    # clear arrival in the position track unclassified on the trip page.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    early_obs = scheduled - timedelta(minutes=3)
    vp = {**_vp(), "current_status": 2, "current_stop_sequence": 5}
    event = classify_arrival(vp, _SCHEDULE, early_obs)
    assert event is not None
    assert event["stop_sequence"] == 5
    assert event["delay_seconds"] == -180


def test_in_transit_fires_for_past_timepoint():
    # Status=2 with current_stop_seq pointing to a LATER stop (6 > 5): stop 5
    # is in the past and a nearby match is legitimate either way.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled + timedelta(seconds=40)
    vp = {**_vp(), "current_status": 2, "current_stop_sequence": 6}
    event = classify_arrival(vp, _SCHEDULE, actual)
    assert event is not None
    assert event["stop_sequence"] == 5


def test_stopped_at_fires_normally():
    # Status=1 (STOPPED_AT): nearest timepoint within radius wins.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled + timedelta(seconds=60)
    vp = {**_vp(), "current_status": 1, "current_stop_sequence": 5}
    event = classify_arrival(vp, _SCHEDULE, actual)
    assert event is not None
    assert event["delay_seconds"] == 60


def test_unknown_status_falls_back_to_nearest():
    # No current_status/current_stop_sequence: nearest-timepoint behaviour.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled + timedelta(seconds=30)
    event = classify_arrival(_vp(), _SCHEDULE, actual)
    assert event is not None


def test_detect_arrivals_dedupes_loitering():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    rows = [
        {**_vp(), "timestamp": scheduled},
        {**_vp(), "timestamp": scheduled + timedelta(seconds=30)},  # still at stop
    ]
    reset_detection_state()
    with patch("app.services.ontime.load_trip_shape_dist_schedule", return_value=_SCHEDULE), \
         patch("app.services.ontime.load_trip_origin_timepoints", return_value={}), \
         patch("app.services.ontime.load_stop_arrivals_index", return_value={}):
        events = detect_arrivals(rows, scheduled)
    assert len(events) == 1  # one event despite two snapshots near the stop


def test_detect_arrivals_times_origin_by_departure():
    # End-to-end through the ingest entry point: the trip's origin is stop 5, so
    # the loitering snapshots must NOT be recorded as an arrival — only the
    # departure, once the bus is seen away from the stop.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    rows = [
        {**_vp(), "timestamp": scheduled - timedelta(minutes=3)},  # laying over
        {**_vp(), "timestamp": scheduled},                          # still there
        {**_vp(lat=_STOP_LAT + 0.0045), "timestamp": scheduled + timedelta(seconds=30)},
    ]
    reset_detection_state()
    with patch("app.services.ontime.load_trip_shape_dist_schedule", return_value=_SCHEDULE), \
         patch("app.services.ontime.load_trip_origin_timepoints", return_value=_ORIGINS), \
         patch("app.services.ontime.load_stop_arrivals_index", return_value={}):
        events = detect_arrivals(rows, scheduled + timedelta(seconds=30))
    assert len(events) == 1
    # Departure, not the 3-min-early first sighting.
    assert events[0]["delay_seconds"] > 0
    reset_detection_state()


# ── Origin departures ─────────────────────────────────────────────────────────

# {trip_id: (seq, stop_id, arrival_secs, lat, lon)} — T1's origin is its stop 5.
_ORIGINS: dict = {"T1": (5, "S1", _ARR_SECS, _STOP_LAT, _STOP_LON)}
# ~500 m north of the stop: comfortably outside the 100 m departure circle.
_AWAY_LAT = _STOP_LAT + 0.0045


def _tracker(**kwargs) -> OriginDepartureTracker:
    return OriginDepartureTracker(_ORIGINS, stop_arrivals={}, **kwargs)


def test_origin_dwell_emits_nothing_until_the_vehicle_leaves():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker()
    # Three snapshots parked at the gate, starting 3 min before the departure.
    for offset in (-180, -150, -120):
        assert t.feed(_vp(), scheduled + timedelta(seconds=offset)) is None


def test_origin_departure_is_interpolated_not_the_first_sighting():
    # The reported bug: bus sits at the gate from 3 min before its scheduled
    # departure, then pulls out on time.  The old logic recorded the layover.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker()
    for offset in (-180, -150, -120, -90, -60, -30, 0):
        assert t.feed(_vp(), scheduled + timedelta(seconds=offset)) is None
    # Next poll it is 500 m up the road; it crossed 100 m a fifth of the way in.
    event = t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(seconds=30))
    assert event is not None
    assert event["stop_sequence"] == 5
    # ~6 s after schedule (100/500 of a 30 s gap), nowhere near 3 min early.
    assert 0 <= event["delay_seconds"] <= 15
    assert event["actual_time"] >= scheduled


def test_origin_departure_reports_lateness():
    # Same shape, but the bus does not pull out until 4 min after schedule.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker()
    for offset in (-120, 0, 120, 240):
        assert t.feed(_vp(), scheduled + timedelta(seconds=offset)) is None
    event = t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(seconds=270))
    assert event is not None
    assert 240 <= event["delay_seconds"] <= 255  # ~4 min late


def test_origin_departure_emitted_once():
    # A loop route passing its own origin later must not re-record it.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker()
    assert t.feed(_vp(), scheduled) is None
    assert t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(seconds=30)) is not None
    assert t.feed(_vp(), scheduled + timedelta(minutes=40)) is None
    assert t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(minutes=41)) is None


def test_origin_never_seen_at_stop_yields_nothing():
    # trip_id attached only after the bus was already away — we cannot know when
    # it left, so record nothing rather than guessing.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker()
    assert t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(seconds=30)) is None
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_stale_origin_falls_back_to_last_sighting():
    # The trip vanishes from the feed while parked: keep the last moment it was
    # seen at the stop rather than losing the event entirely.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker(stale_after=timedelta(minutes=15))
    last_seen = scheduled + timedelta(seconds=60)
    assert t.feed(_vp(), scheduled) is None
    assert t.feed(_vp(), last_seen) is None
    assert t.flush(last_seen + timedelta(minutes=5)) == []  # not stale yet
    events = t.flush(last_seen + timedelta(minutes=20))
    assert len(events) == 1
    assert events[0]["actual_time"] == last_seen
    assert events[0]["delay_seconds"] == 60


def test_gps_jump_cannot_push_departure_past_the_next_fix():
    # A single wild fix 20 km away must not project the crossing outside the
    # interval we actually observed — clamped to [last inside, this fix].
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker()
    assert t.feed(_vp(), scheduled) is None
    later = scheduled + timedelta(seconds=30)
    event = t.feed(_vp(lat=_STOP_LAT + 0.18), later)
    assert event is not None
    assert scheduled <= event["actual_time"] <= later


def test_repositioning_within_the_station_is_not_a_departure():
    # Clear of the 100 m circle, but the feed still has the vehicle at stop 1 —
    # a shuffle between gates, not the start of the trip.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _tracker()
    assert t.feed(_vp(), scheduled - timedelta(minutes=5)) is None
    staged = {**_vp(lat=_AWAY_LAT), "current_status": 1, "current_stop_sequence": 5}
    assert t.feed(staged, scheduled - timedelta(minutes=4)) is None
    # Back at the gate, then away with the feed advanced to stop 6: departed.
    assert t.feed(_vp(), scheduled) is None
    moving = {**_vp(lat=_AWAY_LAT), "current_status": 2, "current_stop_sequence": 6}
    event = t.feed(moving, scheduled + timedelta(seconds=30))
    assert event is not None
    assert 0 <= event["delay_seconds"] <= 15


def test_origin_skipped_by_arrival_classifier():
    # classify_arrival must leave the origin alone so the layover isn't recorded
    # as an arrival alongside the departure.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    assert classify_arrival(_vp(), _SCHEDULE, scheduled, skip_sequence=5) is None
    assert classify_arrival(_vp(), _SCHEDULE, scheduled, skip_sequence=99) is not None


# ── Cross-trip misassignment detection ────────────────────────────────────────

# Build a two-trip schedule: T1 at 08:00, T2 at 08:15 — same route R1, same stop S1.
_ARR_SECS_T2 = _ARR_SECS + 15 * 60  # 08:15:00
_TWO_TRIP_INDEX: dict = {("R1", "S1"): sorted([(_ARR_SECS, "T1"), (_ARR_SECS_T2, "T2")])}


def _nobody_running() -> ActiveTrips:
    """An empty feed: no competing trip is out on the road."""
    return ActiveTrips()


def _also_running(trip_id: str, when) -> ActiveTrips:
    """A feed in which ``trip_id`` is being reported by some other vehicle."""
    registry = ActiveTrips()
    registry.observe(trip_id, when)
    return registry


def test_misassigned_trip_suppressed():
    # Bus on T1 (08:00) but arrives at exactly T2's scheduled time (08:15).
    # The GTFS-RT feed still reports trip_id=T1, making it look 15 min late.
    # A better match (T2) exists in the index and nothing else is reporting as
    # T2 — so this vehicle is the one running T2 → suppress.
    scheduled_t1 = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled_t1 + timedelta(seconds=15 * 60)
    event = classify_arrival(
        _vp(), _SCHEDULE, actual,
        stop_arrivals=_TWO_TRIP_INDEX, active_trips=_nobody_running(),
    )
    assert event is None


def test_late_bus_kept_when_the_competing_trip_is_also_running():
    # The bug: same geometry as above — T1 arriving one whole headway late lands
    # exactly on T2's slot — but here another vehicle *is* out reporting as T2.
    # Our vehicle therefore cannot be T2; it is T1, 15 minutes down.  The bare
    # schedule test cannot tell these two apart, and was deleting the late bus.
    scheduled_t1 = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled_t1 + timedelta(seconds=15 * 60)
    event = classify_arrival(
        _vp(), _SCHEDULE, actual,
        stop_arrivals=_TWO_TRIP_INDEX, active_trips=_also_running("T2", actual),
    )
    assert event is not None
    assert event["trip_id"] == "T1"
    assert event["delay_seconds"] == 15 * 60


def test_stale_sighting_of_the_competing_trip_does_not_vouch_for_it():
    # Yesterday's run of T2 is not evidence that T2 is on the road now: outside
    # the active window the guard falls back to suppressing.
    scheduled_t1 = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled_t1 + timedelta(seconds=15 * 60)
    event = classify_arrival(
        _vp(), _SCHEDULE, actual,
        stop_arrivals=_TWO_TRIP_INDEX,
        active_trips=_also_running("T2", actual - timedelta(days=1)),
    )
    assert event is None


def test_our_own_trip_running_does_not_vouch_for_itself():
    # The index carries our own scheduled slot too.  Being in the feed ourselves
    # must not count as "the competitor is out there" — only a *different* trip
    # can be the competitor.
    scheduled_t1 = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled_t1 + timedelta(seconds=15 * 60)
    event = classify_arrival(
        _vp(), _SCHEDULE, actual,
        stop_arrivals=_TWO_TRIP_INDEX, active_trips=_also_running("T1", actual),
    )
    assert event is None


def test_ordinary_late_bus_at_shared_stop_kept():
    # Regression: a bus 8 min late where the next trip is scheduled 15 min
    # after ours.  The competing slot is 7 min from the sighting -- "closer"
    # than our 8 min delay, which is what the old rule keyed on, so it deleted
    # the arrival.  Termini are the worst case (every trip on the route shares
    # the stop, so headways there are tightest), which is why whole runs of
    # stops came back uncoloured and trips got flagged "Incomplete".
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled + timedelta(seconds=8 * 60)
    event = classify_arrival(_vp(), _SCHEDULE, actual, stop_arrivals=_TWO_TRIP_INDEX)
    assert event is not None
    assert event["delay_seconds"] == 8 * 60


def test_no_better_match_keeps_arrival():
    # Bus is genuinely late (12 min) but there's no competing trip scheduled
    # closer to the actual time — keep the arrival.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled + timedelta(seconds=12 * 60)
    # Only T1 in the index for this stop — no better match.
    arrivals_index: dict = {("R1", "S1"): [(_ARR_SECS, "T1")]}
    event = classify_arrival(_vp(), _SCHEDULE, actual, stop_arrivals=arrivals_index)
    assert event is not None
    assert event["delay_seconds"] == 12 * 60


def test_within_ontime_threshold_skips_check():
    # Delay is within the 5-min on-time window — skip the cross-trip check
    # entirely even if a closer trip exists.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    actual = scheduled + timedelta(seconds=200)  # ~3.3 min late
    event = classify_arrival(_vp(), _SCHEDULE, actual, stop_arrivals=_TWO_TRIP_INDEX)
    assert event is not None
    assert event["delay_seconds"] == 200


# ── Pass-through arrivals interpolated between two fixes ──────────────────────

# Three timepoints on a straight north-south line ~1.1 km apart, so along-route
# distance is trivial to reason about.  B (08:00) is the one under test.
_LINE_LON = -104.9903
_A_LAT, _B_LAT, _C_LAT = 39.730, 39.740, 39.750
_SEG_M = _haversine_m(_A_LAT, _LINE_LON, _B_LAT, _LINE_LON)
_M_PER_DEG_LAT = 111_320.0
_LINE_SCHEDULE: dict = {
    "T1": [
        (1, "SA", _ARR_SECS - 600, _A_LAT, _LINE_LON, 0.0),
        (2, "SB", _ARR_SECS, _B_LAT, _LINE_LON, _SEG_M),
        (3, "SC", _ARR_SECS + 600, _C_LAT, _LINE_LON, 2 * _SEG_M),
    ]
}
_FT = 0.3048


def _north_of_b(metres):
    """A fix `metres` north (+) or south (-) of timepoint B, on the line."""
    return _vp(lat=_B_LAT + metres / _M_PER_DEG_LAT, lon=_LINE_LON)


def test_stop_passed_between_fixes_is_interpolated():
    # The reported case: 125 ft short of the stop, then 125 ft past it 50 s
    # later.  Neither fix is at the stop, but the bus plainly reached it, and
    # equal distances either side put the arrival at the midpoint of the gap.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t1 = scheduled - timedelta(seconds=25)
    t2 = scheduled + timedelta(seconds=25)
    events = classify_segment_arrivals(
        _north_of_b(-125 * _FT), t1,
        _north_of_b(125 * _FT), t2,
        _LINE_SCHEDULE, stop_arrivals={},
    )
    assert [e["stop_id"] for e in events] == ["SB"]
    assert abs((events[0]["actual_time"] - (t1 + (t2 - t1) / 2)).total_seconds()) < 1
    assert abs(events[0]["delay_seconds"]) < 1  # crossed exactly on schedule


def test_crossing_time_is_proportional_not_just_the_midpoint():
    # Three times closer to the stop at the first fix than the second, so the
    # crossing lands a quarter of the way through the gap, not halfway.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t1 = scheduled
    t2 = scheduled + timedelta(seconds=60)
    events = classify_segment_arrivals(
        _north_of_b(-50), t1,
        _north_of_b(150), t2,
        _LINE_SCHEDULE, stop_arrivals={},
    )
    assert len(events) == 1
    assert abs(events[0]["delay_seconds"] - 15) <= 1  # 50/200 of 60 s


def test_segment_catches_what_the_point_match_misses():
    # Regression for the reported symptom: both fixes are outside the geofence
    # (300 m either side of the 152 m bus radius), so per-fix matching sees
    # nothing -- while the replay, which interpolates, plainly shows the bus
    # arriving.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t1, t2 = scheduled - timedelta(seconds=20), scheduled + timedelta(seconds=20)
    before, after = _north_of_b(-300), _north_of_b(300)

    assert classify_arrival(before, _LINE_SCHEDULE, t1, stop_arrivals={}) is None
    assert classify_arrival(after, _LINE_SCHEDULE, t2, stop_arrivals={}) is None

    events = classify_segment_arrivals(before, t1, after, t2, _LINE_SCHEDULE, stop_arrivals={})
    assert [e["stop_id"] for e in events] == ["SB"]


def test_interpolated_position_lands_between_the_fixes():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    events = classify_segment_arrivals(
        _north_of_b(-100), scheduled - timedelta(seconds=20),
        _north_of_b(100), scheduled + timedelta(seconds=20),
        _LINE_SCHEDULE, stop_arrivals={},
    )
    assert len(events) == 1
    # Interpolated to the stop itself, so the map marker sits where the replay
    # draws the bus rather than at whichever raw fix happened to match.
    assert abs(events[0]["actual_lat"] - _B_LAT) < 1e-4
    assert abs(events[0]["actual_lon"] - _LINE_LON) < 1e-6


def test_multiple_stops_passed_in_one_gap():
    # One gap can step over more than one timepoint (express run, or a short
    # feed dropout); every one of them should be recovered, in order.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    events = classify_segment_arrivals(
        _vp(lat=_A_LAT + 0.001, lon=_LINE_LON), scheduled - timedelta(seconds=120),
        _vp(lat=_C_LAT + 0.001, lon=_LINE_LON), scheduled + timedelta(seconds=120),
        _LINE_SCHEDULE, stop_arrivals={},
    )
    assert [e["stop_id"] for e in events] == ["SB", "SC"]


def test_long_gap_is_not_interpolated():
    # Beyond arrival_segment_max_gap_seconds the two fixes are not one
    # continuous movement, so a crossing time would be invented, not measured.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    events = classify_segment_arrivals(
        _north_of_b(-150), scheduled - timedelta(minutes=30),
        _north_of_b(150), scheduled + timedelta(minutes=30),
        _LINE_SCHEDULE, stop_arrivals={},
    )
    assert events == []


def test_backwards_travel_is_not_an_arrival():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    events = classify_segment_arrivals(
        _north_of_b(150), scheduled - timedelta(seconds=20),
        _north_of_b(-150), scheduled + timedelta(seconds=20),
        _LINE_SCHEDULE, stop_arrivals={},
    )
    assert events == []


def test_off_corridor_segment_ignored():
    # Same along-route progress, but ~1 km off the line: a deadhead or a bus on
    # a parallel street must not be credited with passing the stop.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    off = 0.012  # ~1 km of longitude
    events = classify_segment_arrivals(
        _vp(lat=_B_LAT - 0.0015, lon=_LINE_LON + off), scheduled - timedelta(seconds=20),
        _vp(lat=_B_LAT + 0.0015, lon=_LINE_LON + off), scheduled + timedelta(seconds=20),
        _LINE_SCHEDULE, stop_arrivals={},
    )
    assert events == []


def test_skip_sequence_excluded_from_segment_matching():
    # Callers pass the trip's origin here: it is timed by departure
    # (OriginDepartureTracker), so matching it as a pass-through arrival too
    # would record the layover instead. SB stands in for it.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    events = classify_segment_arrivals(
        _vp(lat=_A_LAT + 0.001, lon=_LINE_LON), scheduled - timedelta(seconds=120),
        _vp(lat=_C_LAT + 0.001, lon=_LINE_LON), scheduled + timedelta(seconds=120),
        _LINE_SCHEDULE, stop_arrivals={}, skip_sequence=2,
    )
    assert [e["stop_id"] for e in events] == ["SC"]


# ── Origin departures only count when the vehicle leaves down the route ───────

# The origin (#1) plus two stops north of it, so "forward" has a direction.
_ORIGIN_ROUTE: dict = {
    "T1": [
        (5, "S1", _ARR_SECS, _STOP_LAT, _STOP_LON, 0.0),
        (6, "S2", _ARR_SECS + 300, _B_LAT, _LINE_LON, _haversine_m(_STOP_LAT, _LINE_LON, _B_LAT, _LINE_LON)),
        (7, "S3", _ARR_SECS + 600, _C_LAT, _LINE_LON, _haversine_m(_STOP_LAT, _LINE_LON, _C_LAT, _LINE_LON)),
    ]
}


def _route_tracker() -> OriginDepartureTracker:
    return OriginDepartureTracker(_ORIGINS, schedule=_ORIGIN_ROUTE, stop_arrivals={})


def test_departure_fires_when_leaving_along_the_route():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    assert t.feed(_vp(), scheduled - timedelta(seconds=60)) is None
    # 500 m north — onward toward S2/S3.
    event = t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(seconds=30))
    assert event is not None
    assert event["stop_sequence"] == 5


def test_reversing_to_the_yard_is_not_a_departure():
    # The reported bug: a rail car pulling back to the yard clears the circle
    # too, but backwards down the route -- that is not a departure.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    assert t.feed(_vp(), scheduled - timedelta(seconds=60)) is None
    south = _vp(lat=_STOP_LAT - 0.0045)  # 500 m the wrong way
    assert t.feed(south, scheduled + timedelta(seconds=30)) is None


def test_yard_move_does_not_resurface_via_flush():
    # And it must not come back as a departure once the trip goes quiet: the
    # pending entry is dropped, not parked.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    t.feed(_vp(), scheduled - timedelta(seconds=60))
    t.feed(_vp(lat=_STOP_LAT - 0.0045), scheduled + timedelta(seconds=30))
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_vehicle_that_returns_after_a_yard_move_still_departs():
    # Dropping the pending entry must not lock the trip out: a car that backs
    # up, comes back and then genuinely pulls out is still recorded.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    t.feed(_vp(), scheduled - timedelta(seconds=120))
    assert t.feed(_vp(lat=_STOP_LAT - 0.0045), scheduled - timedelta(seconds=90)) is None
    assert t.feed(_vp(), scheduled - timedelta(seconds=30)) is None      # back at the gate
    event = t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(seconds=30))
    assert event is not None


def test_sideways_first_step_out_of_the_bay_still_departs():
    # The reported bug (Route 52 at Alameda Station, trip 115888171).  The route
    # runs due north to S2/S3, but the bus pulls *east* out of the station bay
    # before turning up it.  Its first fix outside the circle therefore projects
    # to zero along-route progress — indistinguishable from a yard move at that
    # instant — and used to delete the pending entry outright, so the genuine
    # departure on the very next fix had nothing left to resolve against and the
    # origin got no row at all.  The answer is to wait one more fix, not to
    # decide here.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    assert t.feed(_vp(), scheduled - timedelta(seconds=30)) is None
    # ~180 m due east: clear of the 100 m circle, zero progress up the route.
    east = _vp(lat=_STOP_LAT, lon=_STOP_LON + 0.0021)
    assert t.feed(east, scheduled) is None
    # Next fix is unambiguously up the route — the departure resolves.
    event = t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(seconds=30))
    assert event is not None
    assert event["stop_sequence"] == 5


def test_departure_is_timed_from_the_first_fix_outside_the_circle():
    # Having waited for a later fix to confirm direction, the crossing must still
    # be interpolated against the *first* fix outside the circle — the crossing
    # happened in that gap.  Timing it against whichever fix confirmed the
    # direction would drag the departure minutes late.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    t.feed(_vp(), scheduled - timedelta(seconds=30))
    east = _vp(lat=_STOP_LAT, lon=_STOP_LON + 0.0021)
    t.feed(east, scheduled)
    event = t.feed(_vp(lat=_AWAY_LAT), scheduled + timedelta(minutes=5))
    assert event is not None
    # Crossing lands inside the 30 s gap it actually happened in, not out at the
    # five-minute fix that merely confirmed the direction.
    assert scheduled - timedelta(seconds=30) <= event["actual_time"] <= scheduled


def test_leaving_sideways_and_going_quiet_is_still_not_a_departure():
    # The other half: holding the pending entry must not let a real yard move
    # resurface through flush.  A vehicle that left the circle, never made
    # progress and then went silent went to the yard.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    t.feed(_vp(), scheduled - timedelta(seconds=30))
    t.feed(_vp(lat=_STOP_LAT, lon=_STOP_LON + 0.0021), scheduled)
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_never_leaving_the_gate_still_resolves_by_flush():
    # And the flush path it protects must still work: a trip that goes quiet
    # while parked at its origin is recorded at its last sighting there.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = _route_tracker()
    t.feed(_vp(), scheduled)
    events = t.flush(scheduled + timedelta(hours=1), force=True)
    assert [e["stop_sequence"] for e in events] == [5]
    assert events[0]["actual_time"] == scheduled


def test_single_timepoint_trip_keeps_old_behaviour():
    # No second timepoint means no direction to test, so leaving the circle in
    # any direction still counts (what _ORIGINS-only trackers have always done).
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    t = OriginDepartureTracker(_ORIGINS, schedule={"T1": _ORIGIN_ROUTE["T1"][:1]}, stop_arrivals={})
    t.feed(_vp(), scheduled - timedelta(seconds=60))
    assert t.feed(_vp(lat=_STOP_LAT - 0.0045), scheduled + timedelta(seconds=30)) is not None


# ── A run is closed by its terminus ───────────────────────────────────────────

def test_tracking_stops_once_the_terminus_is_reached():
    # A vehicle keeps its trip_id for a while after finishing, and a train that
    # turns around retraces stops it already served. Once the terminus arrival
    # is in, nothing later may add rows to that run.
    reset_detection_state()
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    schedule = {
        "T1": [
            (1, "SA", _ARR_SECS - 600, _A_LAT, _LINE_LON, 0.0),
            (2, "SB", _ARR_SECS, _B_LAT, _LINE_LON, _SEG_M),
        ]
    }
    with patch("app.services.ontime.load_trip_shape_dist_schedule", return_value=schedule), \
         patch("app.services.ontime.load_trip_origin_timepoints", return_value={}), \
         patch("app.services.ontime.load_stop_arrivals_index", return_value={}):
        # Arrives at the terminus (SB, the last timepoint).
        at_terminus = _vp(lat=_B_LAT, lon=_LINE_LON)
        first = detect_arrivals([{**at_terminus, "timestamp": scheduled}], scheduled)
        assert [e["stop_sequence"] for e in first] == [2]

        # Then it reverses back past SA, still carrying T1.
        back = _vp(lat=_A_LAT, lon=_LINE_LON)
        later = detect_arrivals(
            [{**back, "timestamp": scheduled + timedelta(seconds=120)}],
            scheduled + timedelta(seconds=120),
        )
        assert later == []
    reset_detection_state()


def test_stops_before_the_terminus_still_record():
    # The closing rule must not fire early: reaching a mid-route stop leaves the
    # run open.
    reset_detection_state()
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    schedule = {
        "T1": [
            (1, "SA", _ARR_SECS - 600, _A_LAT, _LINE_LON, 0.0),
            (2, "SB", _ARR_SECS, _B_LAT, _LINE_LON, _SEG_M),
            (3, "SC", _ARR_SECS + 600, _C_LAT, _LINE_LON, 2 * _SEG_M),
        ]
    }
    with patch("app.services.ontime.load_trip_shape_dist_schedule", return_value=schedule), \
         patch("app.services.ontime.load_trip_origin_timepoints", return_value={}), \
         patch("app.services.ontime.load_stop_arrivals_index", return_value={}):
        mid = detect_arrivals(
            [{**_vp(lat=_B_LAT, lon=_LINE_LON), "timestamp": scheduled}], scheduled)
        assert [e["stop_sequence"] for e in mid] == [2]
        end = detect_arrivals(
            [{**_vp(lat=_C_LAT, lon=_LINE_LON), "timestamp": scheduled + timedelta(seconds=600)}],
            scheduled + timedelta(seconds=600))
        assert [e["stop_sequence"] for e in end] == [3]
    reset_detection_state()


# ── Terminus fallback ─────────────────────────────────────────────────────────
#
# The reported case: a vehicle whose feed cuts out a few hundred metres short of
# the end of the line.  Nothing later can record its terminus arrival, so the
# trip is filed "Incomplete" even though the replay shows it almost there.  A
# second, wider circle at the terminus only — consulted after the trip goes
# quiet, and only when the ordinary geofence never fired there.

# Last leg SB→SC is _SEG_M (~1113 m), so the bus fallback circle is capped at
# half of it (~556 m) rather than at the configured 750 m.
_TERM_STALE = timedelta(minutes=15)


def _terminus_tracker(schedule=None, **kwargs) -> TerminusFallbackTracker:
    return TerminusFallbackTracker(
        schedule or _LINE_SCHEDULE, stop_arrivals={}, **kwargs
    )


def _north_of_c(metres):
    """A fix `metres` north (+) or south (-) of the terminus SC, on the line."""
    return _vp(lat=_C_LAT + metres / _M_PER_DEG_LAT, lon=_LINE_LON)


def test_terminus_fallback_rescues_a_trip_that_went_quiet_short():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker()
    # 400 m short of the terminus — well outside the 152 m geofence — then
    # nothing more from the feed.
    t.feed(_north_of_c(-400), scheduled)
    events = t.flush(scheduled + _TERM_STALE + timedelta(minutes=1))
    assert len(events) == 1
    assert events[0]["stop_sequence"] == 3
    assert events[0]["stop_id"] == "SC"
    assert events[0]["detection_method"] == "terminus_fallback"
    # Timed at the sighting, so the recorded arrival is a lower bound.
    assert events[0]["actual_time"] == scheduled


def test_terminus_fallback_waits_for_the_trip_to_go_quiet():
    # A vehicle still reporting on approach has not arrived yet.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker()
    t.feed(_north_of_c(-400), scheduled)
    assert t.flush(scheduled + timedelta(minutes=2)) == []


def test_terminus_fallback_keeps_the_closest_approach():
    # Three fixes closing on the terminus: the nearest one is the arrival, not
    # the first or the last sighting.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker()
    t.feed(_north_of_c(-700), scheduled)
    t.feed(_north_of_c(-250), scheduled + timedelta(seconds=30))
    t.feed(_north_of_c(-600), scheduled + timedelta(seconds=60))
    events = t.flush(scheduled + timedelta(hours=1))
    assert len(events) == 1
    assert events[0]["actual_time"] == scheduled + timedelta(seconds=30)


def test_terminus_fallback_skipped_when_the_ordinary_geofence_fired():
    # The whole point: the wide circle is a fallback, never an addition.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker()
    t.feed(_north_of_c(-400), scheduled)
    t.feed(_north_of_c(-50), scheduled + timedelta(seconds=30))  # inside 152 m
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_terminus_fallback_ignores_a_vehicle_that_never_got_close():
    # Died halfway down the last leg — too far to claim it finished.
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker()
    t.feed(_north_of_c(-900), scheduled)
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_terminus_fallback_never_reaches_back_to_the_previous_stop():
    # A short last leg caps the circle at that leg, so a vehicle that actually
    # died at the second-to-last stop is not credited with the terminus.
    short_leg = 300.0
    c_lat = _B_LAT + short_leg / _M_PER_DEG_LAT
    schedule = {
        "T1": [
            (1, "SA", _ARR_SECS - 600, _A_LAT, _LINE_LON, 0.0),
            (2, "SB", _ARR_SECS, _B_LAT, _LINE_LON, _SEG_M),
            (3, "SC", _ARR_SECS + 600, c_lat, _LINE_LON, _SEG_M + short_leg),
        ]
    }
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker(schedule)
    t.feed(_vp(lat=_B_LAT, lon=_LINE_LON), scheduled)  # sitting at SB
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_terminus_fallback_ignores_a_loop_passing_its_own_terminus():
    # A route that doubles back can be within the wide circle at the *start* of
    # the trip. Requiring the fix to project past the second-to-last timepoint
    # is what tells "on the way out" from "on the way in".
    b_lat = 39.745
    c_lat = _A_LAT + 400 / _M_PER_DEG_LAT  # back south, 400 m from SA
    ab = _haversine_m(_A_LAT, _LINE_LON, b_lat, _LINE_LON)
    bc = _haversine_m(b_lat, _LINE_LON, c_lat, _LINE_LON)
    schedule = {
        "T1": [
            (1, "SA", _ARR_SECS - 600, _A_LAT, _LINE_LON, 0.0),
            (2, "SB", _ARR_SECS, b_lat, _LINE_LON, ab),
            (3, "SC", _ARR_SECS + 600, c_lat, _LINE_LON, ab + bc),
        ]
    }
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS - 600)
    t = _terminus_tracker(schedule)
    t.feed(_vp(lat=_A_LAT, lon=_LINE_LON), scheduled)  # at the origin
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_terminus_fallback_emitted_once():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker()
    t.feed(_north_of_c(-400), scheduled)
    assert len(t.flush(scheduled + timedelta(hours=1), force=True)) == 1
    t.feed(_north_of_c(-400), scheduled + timedelta(hours=1))
    assert t.flush(scheduled + timedelta(hours=2), force=True) == []


def test_forget_drops_a_run_the_geofence_already_closed():
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    t = _terminus_tracker()
    t.feed(_north_of_c(-400), scheduled)
    t.forget("T1", _SERVICE_DATE)
    assert t.flush(scheduled + timedelta(hours=1), force=True) == []


def test_detect_arrivals_records_a_fallback_terminus():
    # End to end: a trip stops reporting 400 m short, and the terminus arrival
    # shows up on a later poll once the trip has been quiet long enough.
    reset_detection_state()
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    with patch("app.services.ontime.load_trip_shape_dist_schedule", return_value=_LINE_SCHEDULE), \
         patch("app.services.ontime.load_trip_origin_timepoints", return_value={}), \
         patch("app.services.ontime.load_stop_arrivals_index", return_value={}):
        at_b = detect_arrivals(
            [{**_vp(lat=_B_LAT, lon=_LINE_LON), "timestamp": scheduled}], scheduled)
        assert [e["stop_sequence"] for e in at_b] == [2]

        short = scheduled + timedelta(seconds=540)
        assert detect_arrivals(
            [{**_north_of_c(-400), "timestamp": short}], short) == []

        # …then silence.
        later = short + _TERM_STALE + timedelta(minutes=1)
        rescued = detect_arrivals([], later)
        assert [e["stop_sequence"] for e in rescued] == [3]
        assert rescued[0]["detection_method"] == "terminus_fallback"
    reset_detection_state()


def test_detect_arrivals_stamps_the_ordinary_rules():
    reset_detection_state()
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS)
    with patch("app.services.ontime.load_trip_shape_dist_schedule", return_value=_LINE_SCHEDULE), \
         patch("app.services.ontime.load_trip_origin_timepoints", return_value={}), \
         patch("app.services.ontime.load_stop_arrivals_index", return_value={}):
        events = detect_arrivals(
            [{**_vp(lat=_B_LAT, lon=_LINE_LON), "timestamp": scheduled}], scheduled)
        assert events[0]["detection_method"] == "geofence"
    reset_detection_state()


def test_a_real_terminus_arrival_beats_the_fallback_in_the_same_poll():
    # Both rules can fire on the same run in one cycle. The measurement must
    # win, and the fallback must not add a second row for the same stop.
    reset_detection_state()
    scheduled = _scheduled_utc(_SERVICE_DATE, _ARR_SECS + 600)
    with patch("app.services.ontime.load_trip_shape_dist_schedule", return_value=_LINE_SCHEDULE), \
         patch("app.services.ontime.load_trip_origin_timepoints", return_value={}), \
         patch("app.services.ontime.load_stop_arrivals_index", return_value={}):
        far = scheduled - timedelta(minutes=30)
        detect_arrivals([{**_north_of_c(-400), "timestamp": far}], far)
        # Long after the stale window, but this poll also has the vehicle
        # squarely on the terminus.
        events = detect_arrivals(
            [{**_vp(lat=_C_LAT, lon=_LINE_LON), "timestamp": scheduled}], scheduled)
        assert [e["stop_sequence"] for e in events] == [3]
        assert events[0]["detection_method"] == "geofence"
    reset_detection_state()
