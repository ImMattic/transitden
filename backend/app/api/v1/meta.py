"""Client-facing description of the server's own request limits.

The time-range guards in ``vehicles.py`` / ``historical.py`` / ``export.py``
reject a window that is inverted or too wide, which is the right server
behaviour but a poor way to tell someone their date range is wrong.  Publishing
the same numbers here lets the UI disable the out-of-range dates before they are
picked, without hardcoding a second copy of the limits that can drift from
``Settings``.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db

router = APIRouter(prefix="/meta", tags=["meta"])

_settings = get_settings()


@router.get("/limits")
async def get_limits() -> dict:
    """Widest span each time-ranged endpoint accepts, plus how far back data goes."""
    return {
        "vehicles_max_span_hours": _settings.vehicles_max_span_hours,
        "historical_max_span_days": _settings.historical_max_span_days,
        "export_max_span_days": _settings.export_max_span_days,
        "dashboard_max_span_days": _settings.dashboard_max_span_days,
        "data_retention_days": _settings.data_retention_days,
    }


# ── Storage tier ──────────────────────────────────────────────────────────
# Backs the Trip Explorer's "why does this take longer" disclosure (see
# DateRangePicker.tsx / lib/storageTier.ts) — nothing else reads this today.
#
# Migration 002 compresses vehicle_positions/trip_updates chunks once they age
# out of the hot window, oldest first, so at any moment there is one instant
# such that every chunk starting before it is compressed ("packed") and every
# chunk at or after it is still row-store ("loose"). That instant is exactly
# `min(range_start) FILTER (WHERE NOT is_compressed)` — the earliest chunk the
# compression policy hasn't reached yet.
_STORAGE_TABLES = ("vehicle_positions", "trip_updates")

_STORAGE_SQL = text(
    """
    SELECT hypertable_name,
           min(range_start) FILTER (WHERE NOT is_compressed) AS loose_since
    FROM timescaledb_information.chunks
    WHERE hypertable_name IN :tables
    GROUP BY hypertable_name
    """
).bindparams(bindparam("tables", expanding=True))


@router.get("/storage")
async def get_storage_info(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:
    """Per-hypertable packed/loose boundary, for the Trip Explorer's picker.

    Reads TimescaleDB's own chunk catalog, so there is no state of ours to keep
    in step with the compression policy. Like services/sports.py, this is
    decorative context for a page that works fine without it: a database that
    isn't TimescaleDB (the pytest suite runs on SQLite) has no such catalog, so
    a failure here degrades to "unknown" — ``available: false`` — rather than
    ever 500ing the page that asked for it.
    """
    tables: dict[str, dict[str, str | None]] = {
        name: {"loose_since": None} for name in _STORAGE_TABLES
    }
    try:
        rows = (await db.execute(_STORAGE_SQL, {"tables": _STORAGE_TABLES})).all()
        available = True
        for hypertable_name, loose_since in rows:
            if hypertable_name in tables:
                tables[hypertable_name]["loose_since"] = (
                    loose_since.isoformat() if loose_since else None
                )
    except Exception:
        available = False
    return {"available": available, **tables}
