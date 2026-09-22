"""Pure unit tests for business logic functions.

No database, no network, no mocking required. Tests the calculations that live
inside the endpoint modules.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.api.v1.realtime import _compute_headways, _enrich
from app.models.vehicle_position import VehiclePosition


# ── _compute_headways ─────────────────────────────────────────────────────────

def _make_vp(route_id: str) -> VehiclePosition:
    return VehiclePosition(
        id=1,
        route_id=route_id,
        vehicle_id="V1",
        latitude=39.7,
        longitude=-104.9,
        timestamp=datetime.now(tz=timezone.utc),
    )


def test_compute_headways_two_vehicles_same_route():
    vps = [_make_vp("R1"), _make_vp("R1")]
    hw = _compute_headways(vps)
    assert hw["R1"] == 60.0  # 120 / 2


def test_compute_headways_single_vehicle_returns_zero():
    hw = _compute_headways([_make_vp("R1")])
    assert hw["R1"] == 0.0


def test_compute_headways_empty_input():
    assert _compute_headways([]) == {}


def test_compute_headways_multiple_routes():
    vps = [_make_vp("R1"), _make_vp("R1"), _make_vp("R2")]
    hw = _compute_headways(vps)
    assert hw["R1"] == 60.0   # 120 / 2
    assert hw["R2"] == 0.0    # only 1 vehicle


def test_compute_headways_ten_vehicles():
    vps = [_make_vp("R1") for _ in range(10)]
    hw = _compute_headways(vps)
    assert hw["R1"] == 12.0  # 120 / 10


# ── _enrich ───────────────────────────────────────────────────────────────────

def _base_vp() -> VehiclePosition:
    return VehiclePosition(
        id=1,
        route_id="R1",
        vehicle_id="V1",
        vehicle_label="101",
        trip_id="T1",
        latitude=39.7,
        longitude=-104.9,
        bearing=90.0,
        current_stop_sequence=5,
        current_status=2,
        stop_id="S1",
        occupancy_status=None,
        timestamp=datetime.now(tz=timezone.utc),
    )


def test_enrich_delay_over_threshold_marks_late():
    vp = _base_vp()
    routes: dict = {}
    stops: dict = {}
    delays = {"T1": 400}  # > 300s threshold
    headways: dict = {}
    out = _enrich(vp, routes, stops, delays, headways)
    assert out.delay_seconds == 400
    assert out.is_late is True


def test_enrich_delay_under_threshold_not_late():
    vp = _base_vp()
    out = _enrich(vp, {}, {}, {"T1": 100}, {})
    assert out.delay_seconds == 100
    assert out.is_late is False


def test_enrich_no_trip_id_no_delay():
    vp = _base_vp()
    vp.trip_id = None
    out = _enrich(vp, {}, {}, {"T1": 400}, {})
    assert out.delay_seconds is None
    assert out.is_late is None


def test_enrich_trip_not_in_delays():
    vp = _base_vp()
    out = _enrich(vp, {}, {}, {}, {})  # empty delays dict
    assert out.delay_seconds is None
    assert out.is_late is None


def test_enrich_uses_route_short_name_from_static():
    vp = _base_vp()
    routes = {"R1": {"route_short_name": "15X", "route_long_name": "Colfax Express",
                     "route_color": "FF0000", "route_type": "3"}}
    out = _enrich(vp, routes, {}, {}, {})
    assert out.route_short_name == "15X"
    assert out.route_long_name == "Colfax Express"


def test_enrich_falls_back_to_route_id_when_no_static():
    vp = _base_vp()
    out = _enrich(vp, {}, {}, {}, {})
    assert out.route_short_name == "R1"


def test_enrich_headway_from_headways_dict():
    vp = _base_vp()
    out = _enrich(vp, {}, {}, {}, {"R1": 12.0})
    assert out.headway_minutes == 12.0


def test_enrich_headway_none_when_missing():
    vp = _base_vp()
    out = _enrich(vp, {}, {}, {}, {})
    assert out.headway_minutes is None
