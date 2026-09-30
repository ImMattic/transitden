"""Integration tests for GET /api/v1/export/trip — the single-trip diagnostic bundle."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.stop_arrival import StopArrivalEvent
from app.models.trip_update import TripUpdate
from tests.conftest import make_vehicle

T0 = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
WINDOW = {"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat()}

# (stop_sequence, stop_id, arrival_secs, lat, lon) — two timepoints ~1.1 km apart.
TIMEPOINTS = [(1, "S1", 28800, 39.7392, -104.9903), (5, "S5", 29400, 39.7492, -104.9903)]


@pytest.fixture(autouse=True)
def static_schedule():
    """Small, deterministic static schedule instead of the 35 MB bundled feed."""
    with ExitStack() as stack:
        for target, value in [
            ("app.api.v1.vehicles.load_gtfs_static_data", ({}, {})),
            ("app.api.v1.vehicles.load_trip_stop_sequence", ()),
            ("app.api.v1.vehicles.load_trip_origin_timepoints", {}),
            ("app.api.v1.trip_export.load_trip_stop_sequence", ()),
            ("app.api.v1.trip_export.load_trip_stop_schedule", {"T1": TIMEPOINTS}),
            # The detector's schedule has dropped S5, as the shape-dist filter does
            # for timepoints that sit too close together.
            ("app.api.v1.trip_export.load_trip_shape_dist_schedule", {"T1": [(*TIMEPOINTS[0], 0.0)]}),
            ("app.api.v1.trip_export.load_stop_arrivals_index", {("R1", "S5"): [(29400, "T1"), (29700, "T9")]}),
            ("app.api.v1.trip_export._trip_and_block", (None, [])),
            ("app.api.v1.trip_export.is_rail_route", False),
        ]:
            stack.enter_context(patch(target, return_value=value))
        yield


async def _export(client, **params):
    resp = await client.get("/api/v1/export/trip", params={"vehicle_label": "Label1", **WINDOW, **params})
    assert resp.status_code == 200, resp.text
    return resp


async def test_empty_trip_still_exports(client):
    body = (await _export(client)).json()
    assert body["export_version"] == 1
    assert body["resolved"]["trip_id"] is None
    assert body["raw"]["vehicle_positions"] == []
    assert body["timeline"]["positions"] == []


async def test_is_an_attachment_with_a_safe_filename(client):
    resp = await _export(client, trip_id="T1/../x")
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment;")
    assert 'filename="trip_Label1_T1-x_20260928T1400Z.json"' in cd


async def test_raw_keeps_anomalous_fixes_that_the_timeline_drops(client, db_session):
    db_session.add(make_vehicle(id=1, trip_id="T1", timestamp=T0 + timedelta(minutes=1)))
    db_session.add(
        make_vehicle(id=2, trip_id="T1", timestamp=T0 + timedelta(minutes=2), is_anomalous=True)
    )
    # make_vehicle names the label after the id; pin it back to this vehicle.
    await db_session.flush()
    second = await db_session.get(type(make_vehicle()), 2)
    second.vehicle_label = "Label1"
    await db_session.flush()

    body = (await _export(client, trip_id="T1")).json()
    assert [p["is_anomalous"] for p in body["raw"]["vehicle_positions"]] == [False, True]
    assert body["timeline"]["observation_count"] == 1


async def test_includes_other_vehicles_reporting_the_same_trip(client, db_session):
    db_session.add(make_vehicle(id=1, trip_id="T1", timestamp=T0 + timedelta(minutes=1)))
    db_session.add(make_vehicle(id=7, trip_id="T1", timestamp=T0 + timedelta(minutes=1)))
    db_session.add(make_vehicle(id=8, trip_id="T2", timestamp=T0 + timedelta(minutes=1)))
    await db_session.flush()

    body = (await _export(client, trip_id="T1")).json()
    labels = sorted(p["vehicle_label"] for p in body["raw"]["vehicle_positions"])
    assert labels == ["Label1", "Label7"]


async def test_raw_window_is_padded_beyond_the_page_window(client, db_session):
    db_session.add(make_vehicle(id=1, trip_id="T1", timestamp=T0 + timedelta(minutes=5)))
    db_session.add(make_vehicle(id=2, trip_id="T1", timestamp=T0 - timedelta(minutes=10)))
    await db_session.flush()
    early = await db_session.get(type(make_vehicle()), 2)
    early.vehicle_label = "Label1"
    await db_session.flush()

    padded = (await _export(client, trip_id="T1")).json()
    assert len(padded["raw"]["vehicle_positions"]) == 2
    assert padded["timeline"]["observation_count"] == 1

    tight = (await _export(client, trip_id="T1", pad_minutes=0)).json()
    assert len(tight["raw"]["vehicle_positions"]) == 1


async def test_fix_diagnostics_measure_against_timepoints(client, db_session):
    db_session.add(make_vehicle(id=1, trip_id="T1", timestamp=T0 + timedelta(minutes=1)))
    await db_session.flush()

    body = (await _export(client, trip_id="T1")).json()
    fix = body["diagnostics"]["fixes"][0]
    assert fix["nearest_timepoint_stop_id"] == "S1"
    assert fix["nearest_timepoint_m"] == 0.0
    assert fix["inside_arrival_radius"] is True
    assert body["resolved"]["arrival_radius_m"] == body["config"]["arrival_radius_m"]


async def test_timepoints_say_which_the_detector_uses(client):
    body = (await _export(client, trip_id="T1")).json()
    used = {tp["stop_id"]: tp["used_by_detector"] for tp in body["schedule"]["timepoints"]}
    assert used == {"S1": True, "S5": False}
    assert body["schedule"]["timepoints"][0]["arrival_hms"] == "08:00:00"


async def test_competing_slots_flag_live_rivals(client, db_session):
    db_session.add(make_vehicle(id=1, trip_id="T1", timestamp=T0 + timedelta(minutes=1)))
    db_session.add(make_vehicle(id=9, trip_id="T9", timestamp=T0 + timedelta(minutes=1)))
    await db_session.flush()

    body = (await _export(client, trip_id="T1")).json()
    [slot] = body["diagnostics"]["competing_slots"]
    assert slot["stop_id"] == "S5"
    assert slot["rivals"] == [
        {"trip_id": "T9", "arrival_secs": 29700, "offset_seconds": 300, "seen_in_feed": True}
    ]


async def test_flags_duplicate_arrival_rows(client, db_session):
    db_session.add(make_vehicle(id=1, trip_id="T1", timestamp=T0 + timedelta(minutes=1)))
    for row_id in (1, 2):
        at = T0 + timedelta(minutes=row_id)
        db_session.add(
            StopArrivalEvent(
                id=row_id, trip_id="T1", route_id="R1", stop_id="S1", stop_sequence=1,
                scheduled_time=T0, actual_time=at, delay_seconds=60,
                service_date=date(2026, 9, 28), timestamp=at,
            )
        )
    await db_session.flush()

    body = (await _export(client, trip_id="T1")).json()
    assert len(body["raw"]["stop_arrival_events"]) == 2
    assert body["diagnostics"]["duplicate_arrival_rows"] == [
        {"stop_sequence": 1, "service_date": "2026-09-28", "row_ids": [1, 2]}
    ]


async def test_trip_updates_are_capped_and_say_so(client, db_session):
    db_session.add(make_vehicle(id=1, trip_id="T1", timestamp=T0 + timedelta(minutes=1)))
    for i in range(3):
        db_session.add(
            TripUpdate(id=i + 1, trip_id="T1", route_id="R1", stop_sequence=i,
                       arrival_delay=30, timestamp=T0 + timedelta(minutes=i))
        )
    await db_session.flush()

    with patch("app.api.v1.trip_export._TRIP_UPDATE_ROW_CAP", 2):
        body = (await _export(client, trip_id="T1")).json()
    assert len(body["raw"]["trip_updates"]) == 2
    assert body["raw"]["trip_updates_truncated"] is True


async def test_trip_id_flips_on_the_vehicle_are_listed(client, db_session):
    for i, trip in enumerate(["T1", "T1", "T2"], start=1):
        db_session.add(make_vehicle(id=i, trip_id=trip, timestamp=T0 + timedelta(minutes=i)))
    await db_session.flush()
    for i in (2, 3):
        (await db_session.get(type(make_vehicle()), i)).vehicle_label = "Label1"
    await db_session.flush()

    body = (await _export(client)).json()
    runs = body["diagnostics"]["trip_ids_seen_on_vehicle"]
    assert [(r["trip_id"], r["fixes"]) for r in runs] == [("T1", 2), ("T2", 1)]


async def test_config_snapshot_carries_no_secrets(client):
    config = (await _export(client)).json()["config"]
    assert "ontime_threshold_seconds" in config
    assert "arrival_segment_max_gap_seconds" in config
    assert not any("database" in k or "password" in k or "ntfy" in k for k in config)


async def test_a_failing_static_lookup_lands_in_errors_not_a_500(client):
    with patch("app.api.v1.trip_export.load_trip_stop_schedule", side_effect=OSError("gone")):
        body = (await _export(client, trip_id="T1")).json()
    assert any("timepoint schedule" in e and "gone" in e for e in body["errors"])
    assert body["schedule"]["timepoints"] == []


async def test_rejects_windows_wider_than_the_vehicles_cap(client):
    resp = await client.get(
        "/api/v1/export/trip",
        params={"vehicle_label": "Label1", "start": T0.isoformat(), "end": (T0 + timedelta(days=5)).isoformat()},
    )
    assert resp.status_code == 422
