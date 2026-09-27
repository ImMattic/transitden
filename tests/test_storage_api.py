"""Integration tests for GET /api/v1/meta/storage.

Reads timescaledb_information.chunks (Timescale-only catalog), so db.execute
is mocked to return synthetic rows for the "it works" cases — same approach as
test_stats_api.py. One test deliberately leaves it unmocked to prove the
SQLite-backed test DB (no such catalog) degrades to `available: false`
instead of a 500.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def _mock_execute(rows: list):
    result = MagicMock()
    result.all.return_value = rows
    return AsyncMock(return_value=result)


async def test_storage_reports_loose_since_per_table(client, db_session):
    loose_since = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)
    rows = [
        ("vehicle_positions", loose_since),
        ("trip_updates", loose_since),
    ]
    with patch.object(db_session, "execute", _mock_execute(rows)):
        resp = await client.get("/api/v1/meta/storage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["vehicle_positions"]["loose_since"] == loose_since.isoformat()
    assert data["trip_updates"]["loose_since"] == loose_since.isoformat()


async def test_storage_handles_a_table_with_nothing_yet_uncompressed(client, db_session):
    """A NULL loose_since (the FILTER matched no rows) is a legitimate value,
    not a missing one — the endpoint must not treat it as failure."""
    rows = [("vehicle_positions", None)]
    with patch.object(db_session, "execute", _mock_execute(rows)):
        resp = await client.get("/api/v1/meta/storage")
    data = resp.json()
    assert data["available"] is True
    assert data["vehicle_positions"]["loose_since"] is None
    # A table the query didn't return a row for is unknown, same shape.
    assert data["trip_updates"]["loose_since"] is None


async def test_storage_degrades_gracefully_without_a_timescale_catalog(client):
    """Runs for real against the SQLite test DB, which has no
    timescaledb_information schema — must 200 with available: false, never 500."""
    resp = await client.get("/api/v1/meta/storage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert data["vehicle_positions"]["loose_since"] is None
    assert data["trip_updates"]["loose_since"] is None
