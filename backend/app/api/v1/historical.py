"""Historical vehicle position query endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.vehicle_position import VehiclePosition
from app.schemas.vehicle_position import VehiclePositionHistoryOut
from app.services.gtfs_decoder import load_gtfs_static_data
from app.api.v1.vehicles import _MAX_TRIP_DURATION, _aware, _delay_map

router = APIRouter(prefix="/historical", tags=["historical"])

_settings = get_settings()

# Pagination total is capped to keep wide-range queries cheap. If the true count
# exceeds this the UI simply shows "many pages" rather than an exact figure.
_COUNT_CAP = 50_000


@router.get("/vehicles", response_model=dict)
async def get_historical_vehicles(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query(description="Filter by route ID")] = None,
    start: Annotated[
        datetime | None,
        Query(description="Start timestamp (ISO 8601, default: 24 h ago)"),
    ] = None,
    end: Annotated[
        datetime | None,
        Query(description="End timestamp (ISO 8601, default: now)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 200,
    page: Annotated[int, Query(ge=1)] = 1,
) -> dict:
    now = datetime.now(tz=timezone.utc)
    start = start or (now - timedelta(hours=24))
    end = end or now
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    if end - start > timedelta(days=_settings.historical_max_span_days):
        raise HTTPException(
            status_code=422,
            detail=f"time range too large: max {_settings.historical_max_span_days} days",
        )
    offset = (page - 1) * limit

    stmt = (
        select(VehiclePosition)
        .where(
            VehiclePosition.timestamp >= start,
            VehiclePosition.timestamp <= end,
        )
        .order_by(VehiclePosition.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    if route_id:
        stmt = stmt.where(VehiclePosition.route_id == route_id)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    # Enrich with static route names and most-recent delays. Only look up delays
    # for the trip_ids actually on this page — not the whole (up to 1-year) range.
    routes_static, _ = load_gtfs_static_data()
    page_trip_routes = {r.trip_id: r.route_id for r in rows if r.trip_id}
    delay_start, delay_end = start, end
    if rows:
        stamps = [_aware(r.timestamp) for r in rows]
        delay_start = max(min(stamps) - _MAX_TRIP_DURATION, start)
        delay_end = min(max(stamps) + _MAX_TRIP_DURATION, end)
    delay_map = await _delay_map(db, delay_start, delay_end, page_trip_routes)

    vehicles = [
        VehiclePositionHistoryOut(
            vehicle_id=r.vehicle_id,
            vehicle_label=r.vehicle_label,
            trip_id=r.trip_id,
            route_id=r.route_id,
            route_short_name=routes_static.get(r.route_id, {}).get("route_short_name"),
            latitude=r.latitude,
            longitude=r.longitude,
            bearing=r.bearing,
            current_status=r.current_status,
            stop_id=r.stop_id,
            occupancy_status=r.occupancy_status,
            timestamp=r.timestamp,
            delay_seconds=delay_map.get(r.trip_id or ""),
        )
        for r in rows
    ]

    # Bounded count: stop scanning past _COUNT_CAP rows so a wide date range
    # (the table can hold a year of data) can't turn pagination into a full scan.
    inner = (
        select(VehiclePosition.id)
        .where(
            VehiclePosition.timestamp >= start,
            VehiclePosition.timestamp <= end,
        )
        .limit(_COUNT_CAP)
    )
    if route_id:
        inner = inner.where(VehiclePosition.route_id == route_id)
    total = int((await db.execute(select(func.count()).select_from(inner.subquery()))).scalar_one())
    total_pages = (total + limit - 1) // limit if total else 0

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "page": page,
        "limit": limit,
        "returned": len(vehicles),
        "total": total,
        "total_pages": total_pages,
        "vehicles": [v.model_dump() for v in vehicles],
    }

