"""On-time performance and frequency endpoints."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ARRAY, String, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1._date_range import resolve_range
from app.api.v1._route_filter import resolve_route_ids
from app.database import get_db
from app.schemas.stats import (
    FrequencyResponse,
    FrequencyRouteStats,
    OnTimeResponse,
    OnTimeRouteStats,
    OverallOnTime,
)
from app.services.gtfs_decoder import load_gtfs_static_data
from app.config import get_settings

router = APIRouter(prefix="/stats", tags=["stats"])

# On-time classification thresholds (on-time = within ±5 min of schedule) live in
# the trip_ontime_hourly continuous aggregate (latest definition: migration 006);
# changing them there requires re-refreshing the aggregate.


# ── On-time performance ────────────────────────────────────────────────────

# Aggregate the pre-computed hourly buckets (trip_ontime_hourly, migration 002)
# down to per-route totals over the requested window. This reads a few hundred
# rollup rows instead of millions of raw trip_updates rows.
_ONTIME_SQL = """
    SELECT
        route_id,
        sum(on_time)::bigint                                  AS on_time,
        sum(slightly_late + late + very_late)::bigint        AS late,
        sum(very_early + early)::bigint                       AS early,
        sum(observations)::bigint                             AS observations,
        sum(delay_sum)::bigint                               AS delay_sum
    FROM trip_ontime_hourly
    WHERE bucket >= :start AND bucket < :end
      AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
    GROUP BY route_id
"""


@router.get("/ontime", response_model=OnTimeResponse)
async def ontime_performance(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> OnTimeResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=7)

    result = await db.execute(
        text(_ONTIME_SQL).bindparams(
            bindparam("start"), bindparam("end"),
            bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": rng.start_at, "end": rng.end_at, "route_ids": rids},
    )
    rows = result.all()

    routes_static, _ = load_gtfs_static_data()

    route_stats: list[OnTimeRouteStats] = []
    total_on_time = 0
    total_obs = 0
    total_delay_sum = 0
    for rid, on_time, late, early, observations, delay_sum in sorted(rows):
        total = (on_time or 0) + (late or 0) + (early or 0)
        pct = round(100 * (on_time or 0) / total, 1) if total else 0.0
        avg_delay = round((delay_sum or 0) / observations, 1) if observations else 0.0
        total_on_time += on_time or 0
        total_obs += observations or 0
        total_delay_sum += delay_sum or 0
        route_stats.append(
            OnTimeRouteStats(
                route_id=rid,
                route_short_name=routes_static.get(rid, {}).get("route_short_name", rid),
                total_observations=total,
                on_time=on_time or 0,
                late=late or 0,
                early=early or 0,
                on_time_pct=pct,
                avg_delay_seconds=avg_delay,
            )
        )

    overall_pct = round(100 * total_on_time / total_obs, 1) if total_obs else 0.0
    overall_avg = round(total_delay_sum / total_obs, 1) if total_obs else 0.0

    return OnTimeResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(),
        range_end=rng.end.isoformat(),
        routes=route_stats,
        overall=OverallOnTime(on_time_pct=overall_pct, avg_delay_seconds=overall_avg),
    )


# ── Frequency ──────────────────────────────────────────────────────────────

# Estimated round-trip cycle time per GTFS route_type (minutes).
# headway ≈ cycle_time / active_vehicle_count  (e.g. 10 buses on 90-min loop → 9 min)
_ROUTE_CYCLE_MINUTES: dict[str, float] = {
    "0": 45.0,   # light rail
    "1": 30.0,   # heavy rail / subway
    "2": 120.0,  # commuter rail
    "3": 90.0,   # bus (default)
}


@router.get("/frequency", response_model=FrequencyResponse)
async def frequency_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
) -> FrequencyResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    """Estimate current headway per route from live vehicle position counts.

    Groups the last 30 minutes of positions into 5-minute buckets and counts
    distinct vehicles per bucket.  Headway = cycle_time / vehicle_count using
    route-type-aware cycle times.  Min / max reflect real variation across
    buckets (peak vs. off-peak within the window).
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=30)

    # Count distinct active vehicles per route per 5-minute bucket.
    # Integer division bucketing avoids modulo-operator escaping issues.
    _BUCKET_SQL = """
        SELECT
            route_id,
            COUNT(DISTINCT COALESCE(vehicle_id, trip_id)) AS cnt
        FROM vehicle_positions
        WHERE timestamp >= :cutoff
          AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
        GROUP BY
            route_id,
            date_trunc('hour', timestamp)
                + (EXTRACT(minute FROM timestamp)::int / 5) * 5 * INTERVAL '1 minute'
    """
    result = await db.execute(
        text(_BUCKET_SQL).bindparams(
            bindparam("cutoff"),
            bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"cutoff": cutoff, "route_ids": rids},
    )
    bucket_rows = result.all()

    # Aggregate bucket counts per route
    route_buckets: dict[str, list[int]] = defaultdict(list)
    for rid, cnt in bucket_rows:
        route_buckets[rid].append(int(cnt))

    routes_static, _ = load_gtfs_static_data()

    freq_stats: list[FrequencyRouteStats] = []
    for rid, counts in sorted(route_buckets.items()):
        route_info = routes_static.get(rid, {})
        cycle = _ROUTE_CYCLE_MINUTES.get(route_info.get("route_type", "3"), 90.0)

        max_count = max(counts)
        if max_count >= 2:
            avg_count = sum(counts) / len(counts)
            avg_hw = round(cycle / avg_count, 1)
            # More vehicles active = shorter (better) headway; fewer = longer
            min_hw = round(cycle / max_count, 1)
            max_hw = round(cycle / max(1, min(counts)), 1)
        else:
            avg_hw = min_hw = max_hw = 0.0

        freq_stats.append(
            FrequencyRouteStats(
                route_id=rid,
                route_short_name=route_info.get("route_short_name", rid),
                avg_headway_minutes=avg_hw,
                min_headway_minutes=min_hw,
                max_headway_minutes=max_hw,
                vehicle_count=max_count,
            )
        )

    return FrequencyResponse(
        computed_at=datetime.now(tz=timezone.utc),
        routes=freq_stats,
    )
