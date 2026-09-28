"""Integration tests for GET /api/v1/historical/vehicles.

Uses real SQLite via the db_session fixture. The _delay_map helper uses
DISTINCT ON but only fires when page_trip_ids is non-empty; inserting
VehiclePositions without matching TripUpdates keeps trip_ids empty and
avoids the Postgres-specific query.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.conftest import make_vehicle


async def test_empty_db_returns_zero(client):
    resp = await client.get("/api/v1/historical/vehicles")
    assert resp.status_code == 200
    data = resp.json()
    assert data["returned"] == 0
    assert data["total"] == 0
    assert data["vehicles"] == []


async def test_response_has_required_shape(client, db_session):
    vp = make_vehicle()
    db_session.add(vp)
    await db_session.flush()

    resp = await client.get("/api/v1/historical/vehicles")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("start", "end", "page", "limit", "returned", "total", "total_pages", "vehicles"):
        assert key in data


async def test_returns_inserted_row(client, db_session):
    vp = make_vehicle()
    db_session.add(vp)
    await db_session.flush()

    resp = await client.get("/api/v1/historical/vehicles")
    data = resp.json()
    assert data["returned"] == 1
    assert data["total"] == 1
    assert data["vehicles"][0]["route_id"] == "R1"


async def test_anomalous_rows_excluded(client, db_session):
    db_session.add(make_vehicle(id=1, is_anomalous=False))
    db_session.add(make_vehicle(id=2, vehicle_id="V2", is_anomalous=True))
    await db_session.flush()

    resp = await client.get("/api/v1/historical/vehicles")
    data = resp.json()
    assert data["returned"] == 1
    assert data["total"] == 1
    assert data["vehicles"][0]["vehicle_id"] == "V1"


async def test_pagination_limit(client, db_session):
    for i in range(10):
        db_session.add(make_vehicle(id=i + 1, vehicle_id=f"V{i}"))
    await db_session.flush()

    resp = await client.get("/api/v1/historical/vehicles?limit=3")
    data = resp.json()
    assert data["returned"] == 3
    assert data["limit"] == 3
    assert data["total"] == 10
    assert data["total_pages"] == 4


async def test_pagination_page_2(client, db_session):
    for i in range(10):
        db_session.add(make_vehicle(id=i + 1, vehicle_id=f"V{i}"))
    await db_session.flush()

    resp = await client.get("/api/v1/historical/vehicles?limit=3&page=2")
    data = resp.json()
    assert data["returned"] == 3
    assert data["page"] == 2


async def test_route_filter(client, db_session):
    db_session.add(make_vehicle(id=1, route_id="R1"))
    db_session.add(make_vehicle(id=2, vehicle_id="V2", route_id="R2"))
    db_session.add(make_vehicle(id=3, vehicle_id="V3", route_id="R1"))
    await db_session.flush()

    resp = await client.get("/api/v1/historical/vehicles?route_id=R1")
    data = resp.json()
    assert data["returned"] == 2
    assert all(v["route_id"] == "R1" for v in data["vehicles"])


async def test_date_range_filter(client, db_session):
    now = datetime.now(tz=timezone.utc)
    old = make_vehicle(id=1, timestamp=now - timedelta(minutes=30))
    recent = make_vehicle(id=2, vehicle_id="V2", timestamp=now - timedelta(minutes=5))
    db_session.add(old)
    db_session.add(recent)
    await db_session.flush()

    cutoff = (now - timedelta(minutes=10)).isoformat()
    # Use params= so httpx URL-encodes the + sign in "+00:00" timezone offsets.
    resp = await client.get("/api/v1/historical/vehicles", params={"start": cutoff})
    data = resp.json()
    assert data["returned"] == 1
    assert data["vehicles"][0]["vehicle_id"] == "V2"


async def test_limit_too_large_rejected(client):
    resp = await client.get("/api/v1/historical/vehicles?limit=99999")
    assert resp.status_code == 422


async def test_limit_above_new_ceiling_rejected(client):
    resp = await client.get("/api/v1/historical/vehicles?limit=5000")
    assert resp.status_code == 422


async def test_span_too_wide_rejected(client):
    resp = await client.get(
        "/api/v1/historical/vehicles",
        params={"start": "2024-01-01T00:00:00Z", "end": "2024-02-01T00:00:00Z"},
    )
    assert resp.status_code == 422
    assert "time range too large" in resp.json()["detail"]


async def test_start_after_end_rejected(client):
    resp = await client.get(
        "/api/v1/historical/vehicles",
        params={"start": "2024-01-02T00:00:00Z", "end": "2024-01-01T00:00:00Z"},
    )
    assert resp.status_code == 422


async def test_page_zero_rejected(client):
    resp = await client.get("/api/v1/historical/vehicles?page=0")
    assert resp.status_code == 422


async def test_page_one_is_default(client):
    resp = await client.get("/api/v1/historical/vehicles")
    assert resp.json()["page"] == 1


async def test_vehicle_fields_present(client, db_session):
    db_session.add(make_vehicle())
    await db_session.flush()

    resp = await client.get("/api/v1/historical/vehicles")
    v = resp.json()["vehicles"][0]
    for field in ("vehicle_id", "route_id", "latitude", "longitude", "timestamp"):
        assert field in v
