"""Integration tests for GET /api/v1/stats/* deep-analytics endpoints.

Endpoints that read the Postgres/Timescale continuous aggregates have their
db.execute mocked (same approach as test_stats_api.py for /ontime).  Ridership
uses the plain ridership_monthly ORM table, so it runs for real on SQLite.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.ridership import RidershipMonthly


def _result(*, one=None, scalar=None, all_=None):
    """A MagicMock standing in for a SQLAlchemy Result."""
    m = MagicMock()
    m.one.return_value = one
    m.scalar.return_value = scalar
    m.all.return_value = all_ if all_ is not None else []
    return m


def _execute(*results):
    """AsyncMock for db.execute yielding the given results in call order."""
    return AsyncMock(side_effect=list(results))


# ── /stats/delay/distribution ───────────────────────────────────────────────

async def test_distribution_bins_and_total(client, db_session):
    # very_early, early, on_time, slightly_late, late, very_late, obs, dsum, dsumsq
    row = (10, 20, 100, 15, 10, 5, 160, 48000, 50_000_000)
    with patch.object(db_session, "execute", AsyncMock(return_value=_result(one=row))):
        resp = await client.get("/api/v1/stats/delay/distribution")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 160
    assert len(data["bins"]) == 6
    on_time_bin = next(b for b in data["bins"] if b["key"] == "on_time")
    assert on_time_bin["count"] == 100
    assert on_time_bin["pct"] == pytest.approx(62.5)


async def test_distribution_empty(client, db_session):
    row = (0, 0, 0, 0, 0, 0, 0, 0, 0)
    with patch.object(db_session, "execute", AsyncMock(return_value=_result(one=row))):
        resp = await client.get("/api/v1/stats/delay/distribution")
    data = resp.json()
    assert data["total"] == 0
    assert data["stddev_seconds"] == 0.0


# ── /stats/ontime/trend ─────────────────────────────────────────────────────

async def test_trend_points(client, db_session):
    t = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [(t, 80, 15, 5, 100, 4500)]
    with patch.object(db_session, "execute", AsyncMock(return_value=_result(all_=rows))):
        resp = await client.get("/api/v1/stats/ontime/trend")
    data = resp.json()
    assert data["granularity"] == "day"
    assert len(data["points"]) == 1
    assert data["points"][0]["on_time_pct"] == pytest.approx(80.0)
    assert data["points"][0]["avg_delay_seconds"] == pytest.approx(45.0)


# ── /stats/ontime/heatmap ───────────────────────────────────────────────────

async def test_heatmap_cells(client, db_session):
    rows = [(3, 8, 90, 8, 2, 100, 3000)]  # dow=Wed, hour=8
    with patch.object(db_session, "execute", AsyncMock(return_value=_result(all_=rows))):
        resp = await client.get("/api/v1/stats/ontime/heatmap")
    data = resp.json()
    assert len(data["cells"]) == 1
    cell = data["cells"][0]
    assert cell["dow"] == 3 and cell["hour"] == 8
    assert cell["on_time_pct"] == pytest.approx(90.0)


# ── /stats/stops/worst ──────────────────────────────────────────────────────

async def test_worst_stops(client, db_session):
    rows = [("S1", 50, 50, 100, 60000)]  # stop, on_time, late, obs, delay_sum
    with patch.object(db_session, "execute", AsyncMock(return_value=_result(all_=rows))):
        resp = await client.get("/api/v1/stats/stops/worst?limit=5")
    data = resp.json()
    assert len(data["stops"]) == 1
    s = data["stops"][0]
    assert s["stop_id"] == "S1"
    assert s["avg_delay_seconds"] == pytest.approx(600.0)
    assert s["on_time_pct"] == pytest.approx(50.0)


# ── /stats/service-delivery ─────────────────────────────────────────────────

async def test_service_delivery(client, db_session):
    rows = [("R1", 90)]  # observed trip-days
    sched = {"R1": {"weekday_trips": 10, "saturday_trips": 0, "sunday_trips": 0}}
    with patch.object(db_session, "execute", AsyncMock(return_value=_result(all_=rows))), \
         patch("app.api.v1.analytics.load_schedule_summary", return_value=sched):
        resp = await client.get("/api/v1/stats/service-delivery")
    data = resp.json()
    assert data["observed_trips"] == 90
    assert data["scheduled_trips"] > 0
    assert len(data["routes"]) == 1
    assert data["routes"][0]["route_id"] == "R1"


# ── /stats/frequency/schedule ───────────────────────────────────────────────

async def test_schedule_frequency_for_route(client):
    summary = {
        "R1": {
            "route_id": "R1",
            "weekday_trips": 120,
            "saturday_trips": 80,
            "sunday_trips": 60,
            "service_span": {"start": "05:00", "end": "23:30"},
            "headways_by_hour": {h: 15.0 for h in range(24)},
        }
    }
    with patch("app.api.v1.analytics.load_schedule_summary", return_value=summary):
        resp = await client.get("/api/v1/stats/frequency/schedule?route_id=R1")
    data = resp.json()
    assert len(data["routes"]) == 1
    r = data["routes"][0]
    assert r["weekday_trips"] == 120
    assert r["span_start"] == "05:00"
    assert len(r["headways_by_hour"]) == 24


# ── /stats/occupancy ────────────────────────────────────────────────────────

async def test_occupancy_reported(client, db_session):
    # empty, many_seats, few_seats, standing, crushed, full, not_accepting, unknown, samples
    totals = (200, 100, 100, 40, 5, 4, 1, 200, 650)
    # hour, empty, many_seats, few_seats, standing, crushed, full, not_accepting, unknown, total
    hourly = [(8, 20, 10, 10, 4, 0, 0, 0, 5, 49)]
    with patch.object(db_session, "execute", _execute(_result(one=totals), _result(all_=hourly))):
        resp = await client.get("/api/v1/stats/occupancy")
    data = resp.json()
    assert data["reported"] is True
    assert data["low"] == 300  # empty + many_seats
    assert data["few_seats"] == 100
    assert len(data["by_hour"]) == 1


async def test_occupancy_not_reported(client, db_session):
    totals = (0, 0, 0, 0, 0, 0, 0, 500, 500)
    with patch.object(db_session, "execute", _execute(_result(one=totals), _result(all_=[]))):
        resp = await client.get("/api/v1/stats/occupancy")
    assert resp.json()["reported"] is False


# ── /stats/overview ─────────────────────────────────────────────────────────

async def test_overview(client, db_session):
    # _ontime_totals(cur).one(), _ontime_totals(prev).one(),
    # observed.scalar(), observed_prev.scalar(), _latest_ridership.all()
    cur = (800, 150, 50, 1000, 60000, 9_000_000, 12)
    prev = (700, 250, 50, 1000, 90000, 12_000_000, 11)
    with patch.object(db_session, "execute", _execute(
        _result(one=cur),
        _result(one=prev),
        _result(scalar=500),
        _result(scalar=480),
        _result(all_=[]),  # no ridership imported
    )):
        resp = await client.get("/api/v1/stats/overview")
    data = resp.json()
    assert data["on_time_pct"]["value"] == pytest.approx(80.0)
    assert data["on_time_pct"]["previous"] == pytest.approx(70.0)
    assert data["routes_tracked"] == 12
    assert data["observed_trips"] == 500
    assert data["latest_ridership_total"] is None


async def test_overview_accepts_multi_route_and_mode_params(client, db_session):
    """The dashboard filter menu sends route_ids + modes; the endpoint must bind
    them without error (SQL is mocked, so this covers the param plumbing)."""
    cur = (800, 150, 50, 1000, 60000, 9_000_000, 3)
    prev = (700, 250, 50, 1000, 90000, 12_000_000, 3)
    with patch.object(db_session, "execute", _execute(
        _result(one=cur),
        _result(one=prev),
        _result(scalar=500),
        _result(scalar=480),
        _result(all_=[]),
    )):
        resp = await client.get(
            "/api/v1/stats/overview?route_ids=r15,rE&modes=commuter_rail"
        )
    assert resp.status_code == 200
    assert resp.json()["routes_tracked"] == 3


async def test_trend_accepts_route_ids(client, db_session):
    t = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with patch.object(db_session, "execute",
                      AsyncMock(return_value=_result(all_=[(t, 80, 15, 5, 100, 4500)]))):
        resp = await client.get("/api/v1/stats/ontime/trend?route_ids=A,B,C")
    assert resp.status_code == 200
    assert len(resp.json()["points"]) == 1


async def test_overview_accepts_explicit_start_end_up_to_a_year(client, db_session):
    """The dashboard's calendar picker can request up to ~a year; overview reads
    continuous aggregates, so this must not 422 the way it did at days=91."""
    cur = (800, 150, 50, 1000, 60000, 9_000_000, 12)
    prev = (700, 250, 50, 1000, 90000, 12_000_000, 11)
    with patch.object(db_session, "execute", _execute(
        _result(one=cur), _result(one=prev),
        _result(scalar=500), _result(scalar=480),
        _result(all_=[]),
    )):
        resp = await client.get(
            "/api/v1/stats/overview?start=2025-09-12&end=2026-09-11"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["period_days"] == 365
    assert data["range_start"] == "2025-09-12"
    assert data["range_end"] == "2026-09-11"


async def test_overview_start_after_end_rejected(client):
    resp = await client.get("/api/v1/stats/overview?start=2026-02-01&end=2026-01-01")
    assert resp.status_code == 422


async def test_overview_span_wider_than_dashboard_max_rejected(client):
    resp = await client.get("/api/v1/stats/overview?start=2020-01-01&end=2026-01-01")
    assert resp.status_code == 422


# ── /stats/ridership (real ORM on SQLite) ───────────────────────────────────

async def test_ridership_empty(client):
    resp = await client.get("/api/v1/stats/ridership")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert data["series"] == []


async def test_ridership_with_data(client, db_session):
    db_session.add_all([
        RidershipMonthly(route_id="R1", month=date(2026, 4, 1), boardings=1000),
        RidershipMonthly(route_id="R1", month=date(2026, 5, 1), boardings=1200),
        RidershipMonthly(route_id="R2", month=date(2026, 5, 1), boardings=800),
    ])
    await db_session.flush()

    resp = await client.get("/api/v1/stats/ridership")
    data = resp.json()
    assert data["available"] is True
    # System series sums both routes per month: Apr=1000, May=2000.
    assert data["series"][-1]["boardings"] == 2000
    assert data["latest_total"] == 2000
    assert data["prev_total"] == 1000
    assert data["by_route_latest"][0]["boardings"] == 1200  # R1 leads in May


async def test_ridership_route_filter(client, db_session):
    db_session.add_all([
        RidershipMonthly(route_id="R1", month=date(2026, 5, 1), boardings=1200),
        RidershipMonthly(route_id="R2", month=date(2026, 5, 1), boardings=800),
    ])
    await db_session.flush()

    resp = await client.get("/api/v1/stats/ridership?route_id=R1")
    data = resp.json()
    assert data["latest_total"] == 1200
    assert data["series"][-1]["boardings"] == 1200
