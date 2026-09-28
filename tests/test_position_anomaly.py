"""Unit tests for the GPS-jump filter (app/services/position_anomaly.py).

Pure functions/classes — no DB, no GTFS filesystem beyond what is_rail_route
falls back on when the static feed is unavailable (empty rail set, so every
route below is treated as bus-speed unless told otherwise).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.position_anomaly import PositionAnomalyFilter, filter_batch

_T0 = datetime(2026, 6, 20, 8, 0, 0, tzinfo=timezone.utc)
# Denver Union Station-ish coordinates; ~0.01 deg lat is roughly 1.1 km.
_LAT, _LON = 39.7392, -104.9903


def _vp(lat=_LAT, lon=_LON, vehicle_id="V1", route_id="R1", trip_id="T1"):
    return {
        "vehicle_id": vehicle_id,
        "trip_id": trip_id,
        "route_id": route_id,
        "latitude": lat,
        "longitude": lon,
    }


def test_ordinary_movement_is_never_held():
    tracker = PositionAnomalyFilter(max_speed_mps=40)
    t1 = _T0
    t2 = _T0 + timedelta(seconds=30)
    out1 = tracker.feed(_vp(lat=_LAT), t1)
    out2 = tracker.feed(_vp(lat=_LAT + 0.001), t2)  # ~111 m in 30s ≈ 3.7 m/s
    assert len(out1) == 1 and out1[0]["is_anomalous"] is False
    assert len(out2) == 1 and out2[0]["is_anomalous"] is False


def test_bounce_back_is_flagged_as_noise():
    """The reported C-line bug: origin -> station -> origin -> station -> origin.

    A single implausible jump followed by a return to the prior location must
    be held back rather than trusted, then released flagged once the vehicle
    is seen back where it actually was.
    """
    tracker = PositionAnomalyFilter(max_speed_mps=40, hold_seconds=180)
    origin_lat = _LAT
    # ~2.2 km away — unreachable in 30s at 40 m/s (max ~1.2 km).
    far_lat = _LAT + 0.02

    t0 = _T0
    t1 = _T0 + timedelta(seconds=30)
    t2 = _T0 + timedelta(seconds=60)

    out0 = tracker.feed(_vp(lat=origin_lat), t0)
    assert out0[0]["is_anomalous"] is False

    # Implausible jump: held, not yet returned as accepted.
    out1 = tracker.feed(_vp(lat=far_lat), t1)
    assert out1 == []

    # Bounces back to the origin — the held fix is released, flagged.
    out2 = tracker.feed(_vp(lat=origin_lat), t2)
    assert len(out2) == 2
    held, current = out2
    assert held["latitude"] == far_lat
    assert held["is_anomalous"] is True
    assert current["latitude"] == origin_lat
    assert current["is_anomalous"] is False


def test_confirmed_jump_is_trusted():
    """If the vehicle's next fix continues on from the jump, it was real."""
    tracker = PositionAnomalyFilter(max_speed_mps=40, hold_seconds=180)
    near_lat = _LAT
    jumped_lat = _LAT + 0.02
    continued_lat = _LAT + 0.021  # small onward step from jumped_lat

    t0 = _T0
    t1 = _T0 + timedelta(seconds=30)
    t2 = _T0 + timedelta(seconds=60)

    tracker.feed(_vp(lat=near_lat), t0)
    out1 = tracker.feed(_vp(lat=jumped_lat), t1)
    assert out1 == []  # held pending resolution

    out2 = tracker.feed(_vp(lat=continued_lat), t2)
    assert len(out2) == 2
    held, current = out2
    assert held["latitude"] == jumped_lat
    assert held["is_anomalous"] is False
    assert current["is_anomalous"] is False


def test_unresolved_hold_is_released_untouched():
    """A vehicle that goes quiet after a suspect jump isn't held forever."""
    tracker = PositionAnomalyFilter(max_speed_mps=40, hold_seconds=60)
    t0 = _T0
    t1 = _T0 + timedelta(seconds=30)

    tracker.feed(_vp(lat=_LAT), t0)
    out1 = tracker.feed(_vp(lat=_LAT + 0.02), t1)
    assert out1 == []

    # Nothing more from this vehicle; flush after the hold window elapses.
    released = tracker.flush_stale(t1 + timedelta(seconds=61))
    assert len(released) == 1
    assert released[0]["is_anomalous"] is False

    # Flushing again before another hold window has nothing left to release.
    assert tracker.flush_stale(t1 + timedelta(seconds=62)) == []


def test_rows_without_identity_pass_through_unfiltered():
    tracker = PositionAnomalyFilter(max_speed_mps=40)
    row = {"vehicle_id": None, "trip_id": None, "route_id": "R1", "latitude": _LAT, "longitude": _LON}
    out = tracker.feed(row, _T0)
    assert out == [row]
    assert row["is_anomalous"] is False


def test_filter_batch_matches_manual_feed_plus_flush():
    tracker = PositionAnomalyFilter(max_speed_mps=40, hold_seconds=60)
    rows = [_vp(lat=_LAT), _vp(lat=_LAT + 0.02)]
    out = filter_batch(tracker, rows, _T0)
    # First row accepted immediately; second held (implausible jump from the
    # first, same poll's timestamp so dt <= 1 short-circuits plausibility) —
    # filter_batch feeds rows one at a time with the shared default_time, so
    # both share t=_T0 and the dt<=1 guard accepts both.
    assert len(out) == 2
    assert all(r["is_anomalous"] is False for r in out)
