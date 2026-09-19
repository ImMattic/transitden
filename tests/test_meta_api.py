"""Integration tests for GET /api/v1/meta/limits.

The trips page's date picker greys out days using these numbers, so they have to
be the same ones the request validators enforce — a drift here shows up as a
calendar that offers a range the API then rejects.
"""
from __future__ import annotations

from app.config import get_settings


async def test_limits_reports_every_span_cap(client):
    resp = await client.get("/api/v1/meta/limits")
    assert resp.status_code == 200

    settings = get_settings()
    assert resp.json() == {
        "vehicles_max_span_hours": settings.vehicles_max_span_hours,
        "historical_max_span_days": settings.historical_max_span_days,
        "export_max_span_days": settings.export_max_span_days,
        "dashboard_max_span_days": settings.dashboard_max_span_days,
        "data_retention_days": settings.data_retention_days,
    }


async def test_limits_match_what_the_validators_reject(client):
    """The published vehicles cap is the one /vehicles/active actually enforces."""
    from datetime import datetime, timedelta, timezone

    max_hours = (await client.get("/api/v1/meta/limits")).json()["vehicles_max_span_hours"]
    end = datetime.now(tz=timezone.utc)
    over = end - timedelta(hours=max_hours, minutes=1)

    resp = await client.get(
        "/api/v1/vehicles/active",
        params={"start": over.isoformat(), "end": end.isoformat()},
    )
    assert resp.status_code == 422
    assert "too large" in resp.json()["detail"]


async def test_limits_is_cacheable(client):
    """Values only change with a deploy, so the response carries a long max-age."""
    resp = await client.get("/api/v1/meta/limits")
    assert "max-age=3600" in resp.headers.get("cache-control", "")
