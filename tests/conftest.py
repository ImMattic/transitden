"""Shared fixtures for all backend tests.

Uses an in-memory SQLite database so tests run without a real Postgres/TimescaleDB
instance. Postgres-specific SQL (DISTINCT ON, date_trunc, trip_ontime_hourly) is
handled by patching the relevant helpers in individual test modules.
"""
from __future__ import annotations

import os

from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)
from httpx import AsyncClient, ASGITransport

# Must be set before app.main is imported: the rate-limit middleware is wired
# at import time, and its per-IP budgets would trip across a full test run.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from app.database import Base, get_db
from app.main import app
from app.models.vehicle_position import VehiclePosition

_SQLITE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    """Per-test async SQLite session with fresh schema."""
    engine = create_async_engine(_SQLITE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession):
    """AsyncClient wired to the FastAPI app with SQLite DB override.

    Scheduler and shape-cache warmup are suppressed so tests don't need
    a real Postgres or GTFS static filesystem.
    """
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    with (
        patch("app.main.start_scheduler"),
        patch("app.main.stop_scheduler"),
        patch("app.main.warm_shape_cache"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_module_caches():
    """Clear module-level response caches before each test."""
    import app.api.v1.analytics as analytics_mod
    import app.api.v1.realtime as realtime_mod
    import app.api.v1.stats as stats_mod
    import app.services.ingestion as ingestion_mod
    import app.services.gtfs_schedule as schedule_mod
    import app.services.ontime as ontime_mod
    import app.services.sports as sports_mod
    import app.services.sports_sim as sports_sim_mod

    realtime_mod._vehicles_cache.clear()
    realtime_mod._vehicles_locks.clear()
    analytics_mod._occ_cache.clear()
    analytics_mod._occ_locks.clear()
    stats_mod._alerts_cache = None
    stats_mod._trip_endpoints = None
    ingestion_mod._routes_cache = None
    ingestion_mod._stops_cache = None
    schedule_mod._schedule_cache = None
    schedule_mod._trip_stop_schedule_cache = None
    ontime_mod._recorded.clear()
    sports_mod.reset_caches()
    sports_sim_mod.reset()
    yield


@pytest.fixture(autouse=True)
def patch_gtfs_static():
    """Return empty dicts for GTFS static data in every test.

    Avoids reading ~35 MB of CSV files from the filesystem (which may not
    exist in CI) while still letting endpoints that enrich via static data run.
    """
    with (
        patch("app.api.v1.realtime.load_gtfs_static_data", return_value=({}, {})),
        patch("app.api.v1.historical.load_gtfs_static_data", return_value=({}, {})),
        patch("app.api.v1.stats.load_gtfs_static_data", return_value=({}, {})),
        patch("app.api.v1.analytics.load_gtfs_static_data", return_value=({}, {})),
        patch("app.api.v1.analytics.load_schedule_summary", return_value={}),
    ):
        yield


def make_vehicle(
    id: int = 1,
    route_id: str = "R1",
    vehicle_id: str = "V1",
    trip_id: str | None = None,
    lat: float = 39.7392,
    lon: float = -104.9903,
    current_status: int = 2,
    current_stop_sequence: int = 1,
    timestamp: datetime | None = None,
    **kwargs,
) -> VehiclePosition:
    """Factory for VehiclePosition ORM instances used in test fixtures."""
    return VehiclePosition(
        id=id,
        route_id=route_id,
        vehicle_id=vehicle_id,
        vehicle_label=f"Label{id}",
        trip_id=trip_id if trip_id is not None else f"T{id}",
        latitude=lat,
        longitude=lon,
        bearing=0.0,
        current_stop_sequence=current_stop_sequence,
        current_status=current_status,
        stop_id="S1",
        occupancy_status=None,
        timestamp=timestamp or datetime.now(tz=timezone.utc),
        **kwargs,
    )
