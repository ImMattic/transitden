"""Shared absolute date-range resolution for the Dashboard's analytics endpoints.

The Dashboard's calendar picker sends plain calendar dates (``start``/``end``,
no time-of-day) rather than a relative "last N days" window. Every endpoint in
``stats.py``/``analytics.py`` that reads a continuous aggregate funnels its
``start``/``end`` query params through :func:`resolve_range`, which turns them
into one UTC ``[start_at, end_at)`` instant pair for a ``bucket >= :start AND
bucket < :end`` clause — mirroring how :mod:`_route_filter` is the one place
route/mode filtering gets resolved.

Dates are calendar days in America/Denver, matching the local-time convention
already used for the hour×day-of-week heatmap and occupancy-by-hour (see
``analytics.py``'s ``_TZ``) — a "day" on the Dashboard means a Denver day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

_TZ = ZoneInfo("America/Denver")


@dataclass(frozen=True)
class ResolvedRange:
    start_at: datetime   # UTC instant, inclusive
    end_at: datetime      # UTC instant, exclusive
    start: date           # the resolved local calendar start date
    end: date             # the resolved local calendar end date (inclusive)
    span_days: int        # number of calendar days covered, inclusive


def resolve_range(
    start: date | None,
    end: date | None,
    *,
    max_span_days: int,
    default_days: int = 7,
) -> ResolvedRange:
    """Resolve optional ``start``/``end`` calendar dates into a UTC window.

    Missing ``end`` defaults to today (America/Denver); missing ``start``
    defaults to ``default_days`` days before the resolved ``end``. A future
    ``end`` is clamped to today. Raises 422 if the range is inverted or wider
    than ``max_span_days``.
    """
    today = datetime.now(_TZ).date()
    if end is None:
        end = today
    elif end > today:
        end = today
    if start is None:
        start = end - timedelta(days=default_days - 1)

    if start > end:
        raise HTTPException(422, "start must be on or before end")

    span_days = (end - start).days + 1
    if span_days > max_span_days:
        raise HTTPException(
            422, f"Range too wide: {span_days} days (max {max_span_days})"
        )

    start_at = datetime.combine(start, time.min, tzinfo=_TZ).astimezone(timezone.utc)
    end_at = (
        datetime.combine(end, time.min, tzinfo=_TZ) + timedelta(days=1)
    ).astimezone(timezone.utc)
    return ResolvedRange(start_at=start_at, end_at=end_at, start=start, end=end, span_days=span_days)
