"""Integration tests for GET /api/v1/stats/* endpoints.

/ontime: queries trip_ontime_hourly (Timescale continuous aggregate) + Postgres
         SQL → db.execute is mocked to return synthetic rows.
/frequency: uses date_trunc() in SQL → db.execute is mocked similarly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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
