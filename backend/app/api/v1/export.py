"""Data export endpoint – download vehicle history as CSV or JSON.

Responses are streamed in ~1000-row chunks from a server-side cursor so memory
stays flat regardless of the requested limit. The request-scoped session stays
open for the duration of the stream (FastAPI closes yield-dependencies after
the response is fully sent).
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession, AsyncResult

from app.config import get_settings
from app.database import get_db
from app.models.vehicle_position import VehiclePosition

router = APIRouter(prefix="/export", tags=["export"])

_settings = get_settings()

_CHUNK_ROWS = 1_000

_COLUMNS = (
    VehiclePosition.vehicle_id,
    VehiclePosition.vehicle_label,
    VehiclePosition.trip_id,
    VehiclePosition.route_id,
    VehiclePosition.latitude,
    VehiclePosition.longitude,
    VehiclePosition.bearing,
    VehiclePosition.current_stop_sequence,
    VehiclePosition.current_status,
    VehiclePosition.stop_id,
    VehiclePosition.occupancy_status,
    VehiclePosition.timestamp,
)
_FIELDNAMES = [c.key for c in _COLUMNS]


@router.get("/vehicles")
async def export_vehicles(
    db: Annotated[AsyncSession, Depends(get_db)],
    fmt: Annotated[str, Query(alias="format", pattern="^(csv|json)$")] = "json",
    route_id: Annotated[str | None, Query()] = None,
    start: Annotated[datetime | None, Query()] = None,
    end: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 5_000,
) -> StreamingResponse:
    start, end = _validated_range(start, end)

    stmt = (
        select(*_COLUMNS)
        .where(
            VehiclePosition.timestamp >= start,
            VehiclePosition.timestamp <= end,
            VehiclePosition.is_anomalous.is_(False),
        )
        .order_by(VehiclePosition.timestamp.asc())
        .limit(limit)
    )
    if route_id:
        stmt = stmt.where(VehiclePosition.route_id == route_id)

    result = await db.stream(stmt.execution_options(yield_per=_CHUNK_ROWS))

    if fmt == "csv":
        return StreamingResponse(
            _csv_chunks(result),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="vehicle_positions.csv"'},
        )
    return StreamingResponse(
        _json_chunks(result),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="vehicle_positions.json"'},
    )


def _validated_range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(tz=timezone.utc)
    start = start or (now - timedelta(days=7))
    end = end or now
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    max_span = timedelta(days=_settings.export_max_span_days)
    if end - start > max_span:
        raise HTTPException(
            status_code=422,
            detail=f"time range too large: max {_settings.export_max_span_days} days",
        )
    return start, end


def _row_to_dict(row: Row) -> dict:
    d = dict(row._mapping)
    d["timestamp"] = d["timestamp"].isoformat()
    return d


async def _csv_chunks(result: AsyncResult) -> AsyncIterator[str]:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_FIELDNAMES)
    writer.writeheader()
    async for partition in result.partitions(_CHUNK_ROWS):
        for row in partition:
            writer.writerow(_row_to_dict(row))
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
    remaining = buf.getvalue()
    if remaining:
        yield remaining


async def _json_chunks(result: AsyncResult) -> AsyncIterator[str]:
    yield "["
    first = True
    async for partition in result.partitions(_CHUNK_ROWS):
        chunk = ",".join(json.dumps(_row_to_dict(r), default=str) for r in partition)
        yield chunk if first else "," + chunk
        first = False
    yield "]"
