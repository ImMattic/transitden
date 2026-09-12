"""Integration tests for GET /api/v1/stats/* endpoints.

/alerts: uses standard SQLAlchemy ORM → runs fine on SQLite.
         SQLite strips timezone info from stored datetimes, so we patch
         datetime.now in the stats module to return naive UTC to avoid
         "can't subtract offset-naive and offset-aware datetimes" errors.
/ontime: queries trip_ontime_hourly (Timescale continuous aggregate) + Postgres
         SQL → db.execute is mocked to return synthetic rows.
/frequency: uses date_trunc() in SQL → db.execute is mocked similarly.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime as _dt_class, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_vehicle


class _NaiveDatetime:
    """Substitute for datetime.datetime that returns naive UTC from .now().

    SQLite strips timezone info when reading TIMESTAMP columns, so arithmetic
    between the endpoint's datetime.now(tz=utc) and DB-read timestamps fails.
    Using naive UTC on both sides avoids the mismatch without changing prod code.
    """

    @staticmethod
    def now(tz=None):
        return _dt_class.now(timezone.utc).replace(tzinfo=None)

    def __getattr__(self, name):
        return getattr(_dt_class, name)


@contextmanager
def _naive_now():
    """Patch app.api.v1.stats.datetime to return naive UTC from .now()."""
    with patch("app.api.v1.stats.datetime", _NaiveDatetime()):
        yield


# ── /stats/alerts ─────────────────────────────────────────────────────────────

async def test_alerts_empty_db(client):
    with _naive_now():
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["alerts"] == []
    assert "computed_at" in data


async def test_alerts_detects_stuck_vehicle(client, db_session):
    """Vehicle in-transit at same position for >12 min should appear as an alert."""
    now = _dt_class.now(timezone.utc).replace(tzinfo=None)  # naive UTC to match SQLite read-back
    for i in range(5):
        db_session.add(
            make_vehicle(
                id=i + 1,
                vehicle_id="V1",
                lat=39.7392,
                lon=-104.9903,
                current_status=2,  # IN_TRANSIT_TO
                timestamp=now - timedelta(minutes=20 - i * 2),
            )
        )
    await db_session.flush()

    with _naive_now():
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.status_code == 200
    alerts = resp.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["vehicle_id"] == "V1"
    assert alerts[0]["minutes_stuck"] >= 12


async def test_alerts_skips_stopped_at_status(client, db_session):
    """Vehicles with current_status=1 (STOPPED_AT) across their entire streak
    are intentional dwells and must NOT trigger an alert."""
    now = _dt_class.now(timezone.utc).replace(tzinfo=None)
    for i in range(5):
        db_session.add(
            make_vehicle(
                id=i + 1,
                vehicle_id="V1",
                lat=39.7392,
                lon=-104.9903,
                current_status=1,  # STOPPED_AT
                timestamp=now - timedelta(minutes=20 - i * 2),
            )
        )
    await db_session.flush()

    with _naive_now():
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.json()["alerts"] == []


async def test_alerts_skips_vehicle_that_moved(client, db_session):
    """If the vehicle's recent history shows a position change, no alert."""
    now = _dt_class.now(timezone.utc).replace(tzinfo=None)
    db_session.add(make_vehicle(id=1, vehicle_id="V1", lat=39.70, lon=-104.99,
                                current_status=2, timestamp=now - timedelta(minutes=3)))
    db_session.add(make_vehicle(id=2, vehicle_id="V1", lat=39.71, lon=-104.98,
                                current_status=2, timestamp=now - timedelta(minutes=6)))
    db_session.add(make_vehicle(id=3, vehicle_id="V1", lat=39.72, lon=-104.97,
                                current_status=2, timestamp=now - timedelta(minutes=9)))
    await db_session.flush()

    with _naive_now():
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.json()["alerts"] == []


async def test_alerts_single_row_ignored(client, db_session):
    """A vehicle with only one history row cannot be classified as stuck."""
    db_session.add(make_vehicle(current_status=2))
    await db_session.flush()

    with _naive_now():
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.json()["alerts"] == []


async def test_alerts_skips_zero_coordinates(client, db_session):
    """Vehicles reporting GPS position (0, 0) are invalid readings and must not alert."""
    now = _dt_class.now(timezone.utc).replace(tzinfo=None)
    for i in range(5):
        db_session.add(
            make_vehicle(
                id=i + 1,
                vehicle_id="V1",
                lat=0.0,
                lon=0.0,
                current_status=2,
                timestamp=now - timedelta(minutes=20 - i * 2),
            )
        )
    await db_session.flush()

    with _naive_now():
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.json()["alerts"] == []


async def test_alerts_skips_endpoint_stop(client, db_session):
    """Vehicles waiting at the first or last stop of their trip must not alert."""
    import app.api.v1.stats as stats_mod

    now = _dt_class.now(timezone.utc).replace(tzinfo=None)
    for i in range(5):
        db_session.add(
            make_vehicle(
                id=i + 1,
                vehicle_id="V1",
                trip_id="TRIP1",
                lat=39.7392,
                lon=-104.9903,
                current_status=2,
                timestamp=now - timedelta(minutes=20 - i * 2),
            )
        )
    await db_session.flush()

    # Inject endpoint maps: TRIP1's last stop is at the vehicle's exact position.
    stats_mod._trip_endpoint_stops = {"TRIP1": ("STOP_FIRST", "STOP_LAST")}
    mock_stops = {
        "STOP_FIRST": {"stop_lat": 39.0, "stop_lon": -105.0},  # far away
        "STOP_LAST": {"stop_lat": 39.7392, "stop_lon": -104.9903},  # at vehicle
    }
    with _naive_now(), patch(
        "app.api.v1.stats.load_gtfs_static_data", return_value=({}, mock_stops)
    ):
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.json()["alerts"] == []


@pytest.mark.asyncio
async def test_alerts_skips_endpoint_stop_route_fallback(client, db_session):
    """When trip_id is absent from static data, fall back to route-level terminals."""
    import app.api.v1.stats as stats_mod

    now = _dt_class.now(timezone.utc).replace(tzinfo=None)
    for i in range(5):
        db_session.add(
            make_vehicle(
                id=i + 10,
                vehicle_id="V2",
                trip_id="UNKNOWN_TRIP",  # not in static schedule
                route_id="19",
                lat=39.7392,
                lon=-104.9903,
                current_status=2,
                timestamp=now - timedelta(minutes=20 - i * 2),
            )
        )
    await db_session.flush()

    # Trip not in static data; route-level fallback has the terminal nearby.
    stats_mod._trip_endpoint_stops = {}
    stats_mod._route_terminal_stops = {"19": {"CIVIC_CENTER"}}
    mock_stops = {
        "CIVIC_CENTER": {"stop_lat": 39.7392, "stop_lon": -104.9903},
    }
    with _naive_now(), patch(
        "app.api.v1.stats.load_gtfs_static_data", return_value=({}, mock_stops)
    ):
        resp = await client.get("/api/v1/stats/alerts")
    assert resp.json()["alerts"] == []


# ── /stats/ontime ─────────────────────────────────────────────────────────────

def _mock_execute(rows: list):
    """Return an AsyncMock for db.execute that yields the given rows."""
    result = MagicMock()
    result.all.return_value = rows
    return AsyncMock(return_value=result)


async def test_ontime_empty_result(client, db_session):
    with patch.object(db_session, "execute", _mock_execute([])):
        resp = await client.get("/api/v1/stats/ontime")
    assert resp.status_code == 200
    data = resp.json()
    assert data["routes"] == []
    assert data["overall"]["on_time_pct"] == 0.0


async def test_ontime_calculates_pct_correctly(client, db_session):
    # Columns: route_id, on_time, late, early, observations, delay_sum
    rows = [("R1", 80, 15, 5, 100, 4500)]
    with patch.object(db_session, "execute", _mock_execute(rows)):
        resp = await client.get("/api/v1/stats/ontime")
    data = resp.json()
    assert data["period_days"] == 7
    assert len(data["routes"]) == 1
    r = data["routes"][0]
    assert r["route_id"] == "R1"
    assert r["on_time_pct"] == pytest.approx(80.0)
    assert r["avg_delay_seconds"] == pytest.approx(45.0)


async def test_ontime_overall_aggregates_all_routes(client, db_session):
    rows = [
        ("R1", 80, 10, 10, 100, 3000),
        ("R2", 40, 40, 20, 100, 6000),
    ]
    with patch.object(db_session, "execute", _mock_execute(rows)):
        resp = await client.get("/api/v1/stats/ontime")
    data = resp.json()
    overall = data["overall"]
    assert overall["on_time_pct"] == pytest.approx(60.0)   # 120 / 200
    assert overall["avg_delay_seconds"] == pytest.approx(45.0)  # 9000 / 200


async def test_ontime_default_range_is_7_days(client, db_session):
    with patch.object(db_session, "execute", _mock_execute([])):
        resp = await client.get("/api/v1/stats/ontime")
    assert resp.json()["period_days"] == 7


async def test_ontime_start_end_params_accepted(client, db_session):
    with patch.object(db_session, "execute", _mock_execute([])):
        resp = await client.get("/api/v1/stats/ontime?start=2026-01-01&end=2026-01-30")
    data = resp.json()
    assert data["period_days"] == 30
    assert data["range_start"] == "2026-01-01"
    assert data["range_end"] == "2026-01-30"


async def test_ontime_start_after_end_rejected(client):
    resp = await client.get("/api/v1/stats/ontime?start=2026-01-10&end=2026-01-01")
    assert resp.status_code == 422


async def test_ontime_span_too_wide_rejected(client):
    resp = await client.get("/api/v1/stats/ontime?start=2025-01-01&end=2026-01-05")
    assert resp.status_code == 422


# ── /stats/frequency ──────────────────────────────────────────────────────────

async def test_frequency_empty_result(client, db_session):
    with patch.object(db_session, "execute", _mock_execute([])):
        resp = await client.get("/api/v1/stats/frequency")
    assert resp.status_code == 200
    data = resp.json()
    assert data["routes"] == []
    assert "computed_at" in data


async def test_frequency_headway_calculation(client, db_session):
    # Columns: route_id, cnt (vehicles in that 5-min bucket)
    # Two buckets for R1 (bus, route_type="3", cycle=90 min): counts [10, 8]
    rows = [("R1", 10), ("R1", 8)]
    with patch.object(db_session, "execute", _mock_execute(rows)):
        resp = await client.get("/api/v1/stats/frequency")
    data = resp.json()
    assert len(data["routes"]) == 1
    r = data["routes"][0]
    assert r["route_id"] == "R1"
    # avg_count = (10+8)/2 = 9, cycle=90 → avg_headway = 90/9 = 10.0
    assert r["avg_headway_minutes"] == pytest.approx(10.0)
    assert r["vehicle_count"] == 10  # max(counts)


async def test_frequency_single_vehicle_headway_zero(client, db_session):
    rows = [("R1", 1)]
    with patch.object(db_session, "execute", _mock_execute(rows)):
        resp = await client.get("/api/v1/stats/frequency")
    r = resp.json()["routes"][0]
    assert r["avg_headway_minutes"] == 0.0
    assert r["min_headway_minutes"] == 0.0
