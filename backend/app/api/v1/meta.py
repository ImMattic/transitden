"""Client-facing description of the server's own request limits.

The time-range guards in ``vehicles.py`` / ``historical.py`` / ``export.py``
reject a window that is inverted or too wide, which is the right server
behaviour but a poor way to tell someone their date range is wrong.  Publishing
the same numbers here lets the UI disable the out-of-range dates before they are
picked, without hardcoding a second copy of the limits that can drift from
``Settings``.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings

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
