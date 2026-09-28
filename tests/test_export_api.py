"""Integration tests for GET /api/v1/export/vehicles."""
from __future__ import annotations

import csv
import io
import json

import pytest

from tests.conftest import make_vehicle


async def test_export_json_empty_returns_200(client):
    resp = await client.get("/api/v1/export/vehicles")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"


async def test_export_json_empty_body_is_array(client):
    resp = await client.get("/api/v1/export/vehicles")
    assert resp.json() == []


async def test_export_json_content_disposition(client):
    resp = await client.get("/api/v1/export/vehicles")
    cd = resp.headers.get("content-disposition", "")
    assert "vehicle_positions.json" in cd


async def test_export_csv_empty_returns_200(client):
    resp = await client.get("/api/v1/export/vehicles?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]


async def test_export_json_has_expected_fields(client, db_session):
    db_session.add(make_vehicle())
    await db_session.flush()

    resp = await client.get("/api/v1/export/vehicles")
    rows = resp.json()
    assert len(rows) == 1
    for field in ("vehicle_id", "route_id", "latitude", "longitude", "timestamp"):
        assert field in rows[0]


async def test_export_excludes_anomalous_rows(client, db_session):
    db_session.add(make_vehicle(id=1, is_anomalous=False))
    db_session.add(make_vehicle(id=2, vehicle_id="V2", is_anomalous=True))
    await db_session.flush()

    resp = await client.get("/api/v1/export/vehicles")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["vehicle_id"] == "V1"


async def test_export_csv_has_header_row(client, db_session):
    db_session.add(make_vehicle())
    await db_session.flush()

    resp = await client.get("/api/v1/export/vehicles?format=csv")
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader)
    assert "vehicle_id" in header
    assert "route_id" in header
    assert "timestamp" in header


async def test_export_route_filter(client, db_session):
    db_session.add(make_vehicle(id=1, route_id="R1"))
    db_session.add(make_vehicle(id=2, vehicle_id="V2", route_id="R2"))
    await db_session.flush()

    resp = await client.get("/api/v1/export/vehicles?route_id=R1")
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["route_id"] == "R1"


async def test_export_invalid_format_rejected(client):
    resp = await client.get("/api/v1/export/vehicles?format=xml")
    assert resp.status_code == 422


async def test_export_limit_ceiling_rejected(client):
    resp = await client.get("/api/v1/export/vehicles?limit=99999")
    assert resp.status_code == 422


async def test_export_limit_at_ceiling_accepted(client):
    resp = await client.get("/api/v1/export/vehicles?limit=10000")
    assert resp.status_code == 200


async def test_export_old_ceiling_now_rejected(client):
    resp = await client.get("/api/v1/export/vehicles?limit=50000")
    assert resp.status_code == 422


async def test_export_span_too_wide_rejected(client):
    resp = await client.get(
        "/api/v1/export/vehicles",
        params={"start": "2024-01-01T00:00:00Z", "end": "2024-06-01T00:00:00Z"},
    )
    assert resp.status_code == 422
    assert "time range too large" in resp.json()["detail"]


async def test_export_start_after_end_rejected(client):
    resp = await client.get(
        "/api/v1/export/vehicles",
        params={"start": "2024-01-02T00:00:00Z", "end": "2024-01-01T00:00:00Z"},
    )
    assert resp.status_code == 422


async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
