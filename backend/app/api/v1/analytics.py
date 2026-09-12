"""Deep analytics endpoints powering the rebuilt Dashboard & Historical pages.

All time-series/aggregate endpoints read the continuous aggregates created in
migration 003 (trip_ontime_hourly, stop_delay_daily, trip_activity_daily) and
migration 008 (occupancy_status_hourly) so they stay fast over long windows —
up to dashboard_max_span_days (~a year), resolved via app.api.v1._date_range.
Ridership reads the plain ridership_monthly table.  Route names are enriched
from GTFS static via load_gtfs_static_data(), mirroring stats.py.

Hour-of-day / day-of-week are reported in America/Denver local time so the
heatmap and occupancy-by-hour charts read naturally to a Denver audience.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ARRAY, String, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.ridership import RidershipMonthly
from app.schemas.analytics import (
    DirectionInfo,
    DistributionBin,
    DistributionResponse,
    HeatmapCell,
    HeatmapResponse,
    HourHeadway,
    MetricWithDelta,
    OccupancyHourPoint,
    OccupancyResponse,
    OverviewResponse,
    RidershipPoint,
    RidershipResponse,
    RidershipRoute,
    ScheduleFrequencyResponse,
    ScheduleFrequencyRoute,
    ServiceDeliveryResponse,
    ServiceDeliveryRoute,
    TrendPoint,
    TrendResponse,
    WorstStop,
    WorstStopsResponse,
)
from app.api.v1._date_range import ResolvedRange, resolve_range
from app.api.v1._route_filter import resolve_route_ids
from app.services.gtfs_decoder import load_gtfs_static_data
from app.services.gtfs_schedule import load_route_direction_info, load_schedule_summary

router = APIRouter(prefix="/stats", tags=["analytics"])

_TZ = "America/Denver"

# Fine delay bins exposed by the distribution endpoint (matches migration 006:
# observed delay vs. schedule, on-time = ±5 min).
_DELAY_BINS = [
    ("very_early", "Very early (>10m)"),
    ("early", "Early (5–10m)"),
    ("on_time", "On time (±5m)"),
    ("slightly_late", "Slightly late (5–10m)"),
    ("late", "Late (10–15m)"),
    ("very_late", "Very late (>15m)"),
]


def _route_name(routes_static: dict[str, dict[str, Any]], rid: str) -> str:
    return routes_static.get(rid, {}).get("route_short_name", rid) or rid


_DOW_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _count_daytypes(start: datetime, end: datetime) -> dict[str, int]:
    """Count occurrences of each day-of-week in [start, end] (inclusive of dates)."""
    counts: dict[str, int] = {d: 0 for d in _DOW_NAMES}
    d = start.date()
    end_d = end.date()
    while d <= end_d:
        counts[_DOW_NAMES[d.weekday()]] += 1
        d += timedelta(days=1)
    return counts


def _scheduled_trips(route_id: str, day_counts: dict[str, int]) -> int:
    s = load_schedule_summary().get(route_id)
    if not s:
        return 0
    tdow = s.get("trips_per_dow")
    if tdow:
        return sum(tdow.get(dow, 0) * cnt for dow, cnt in day_counts.items())
    # Fallback for a cached schedule built before trips_per_dow was added.
    wd = sum(day_counts.get(d, 0) for d in ("monday", "tuesday", "wednesday", "thursday", "friday"))
    return s["weekday_trips"] * wd + s["saturday_trips"] * day_counts.get("saturday", 0) + s["sunday_trips"] * day_counts.get("sunday", 0)


# ── Overview (hero KPIs) ────────────────────────────────────────────────────

_OVERVIEW_ONTIME_SQL = """
    SELECT
        sum(on_time)::bigint                            AS on_time,
        sum(slightly_late + late + very_late)::bigint  AS late,
        sum(very_early + early)::bigint                 AS early,
        sum(observations)::bigint                       AS observations,
        sum(delay_sum)::bigint                          AS delay_sum,
        sum(delay_sumsq)::numeric                       AS delay_sumsq,
        count(DISTINCT route_id)                        AS routes
    FROM trip_ontime_hourly
    WHERE bucket >= :start AND bucket < :end
      AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
"""

_OBSERVED_TRIPS_SQL = """
    SELECT count(*)::bigint AS trips
    FROM trip_activity_daily
    WHERE bucket >= :start AND bucket < :end
      AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
"""


async def _ontime_totals(db: AsyncSession, start: datetime, end: datetime,
                         route_ids: list[str] | None) -> dict[str, float]:
    row = (await db.execute(
        text(_OVERVIEW_ONTIME_SQL).bindparams(
            bindparam("start"), bindparam("end"),
            bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": start, "end": end, "route_ids": route_ids},
    )).one()
    on_time, late, early, obs, dsum, dsumsq, routes = row
    return {
        "on_time": on_time or 0, "late": late or 0, "early": early or 0,
        "observations": obs or 0, "delay_sum": dsum or 0,
        "delay_sumsq": float(dsumsq or 0), "routes": routes or 0,
    }


def _pct_on_time(t: dict[str, float]) -> float:
    total = t["on_time"] + t["late"] + t["early"]
    return round(100 * t["on_time"] / total, 1) if total else 0.0


def _avg_delay(t: dict[str, float]) -> float:
    return round(t["delay_sum"] / t["observations"], 1) if t["observations"] else 0.0


def _stddev(t: dict[str, float]) -> float:
    n = t["observations"]
    if not n:
        return 0.0
    mean = t["delay_sum"] / n
    var = t["delay_sumsq"] / n - mean * mean
    return round(math.sqrt(var), 1) if var > 0 else 0.0


@router.get("/overview", response_model=OverviewResponse)
async def overview(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> OverviewResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=7)
    span = rng.end_at - rng.start_at
    prev_start_at = rng.start_at - span

    cur = await _ontime_totals(db, rng.start_at, rng.end_at, rids)
    prev = await _ontime_totals(db, prev_start_at, rng.start_at, rids)

    observed = (await db.execute(
        text(_OBSERVED_TRIPS_SQL).bindparams(
            bindparam("start"), bindparam("end"),
            bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": rng.start_at, "end": rng.end_at, "route_ids": rids},
    )).scalar() or 0
    observed_prev = (await db.execute(
        text(_OBSERVED_TRIPS_SQL).bindparams(
            bindparam("start"), bindparam("end"),
            bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": prev_start_at, "end": rng.start_at, "route_ids": rids},
    )).scalar() or 0

    routes_static, _ = load_gtfs_static_data()
    sched_route_ids = rids if rids else list(routes_static.keys())

    def _sched(start_d: date, end_d: date) -> int:
        day_counts = _count_daytypes(
            datetime.combine(start_d, datetime.min.time()),
            datetime.combine(end_d, datetime.min.time()),
        )
        return sum(_scheduled_trips(rid, day_counts) for rid in sched_route_ids)

    prev_end_date = rng.start - timedelta(days=1)
    prev_start_date = prev_end_date - timedelta(days=rng.span_days - 1)

    sched = _sched(rng.start, rng.end)
    sched_prev = _sched(prev_start_date, prev_end_date)
    delivered = round(min(100.0, 100 * observed / sched), 1) if sched else 0.0
    delivered_prev = round(min(100.0, 100 * observed_prev / sched_prev), 1) if sched_prev else 0.0

    # Latest ridership (system or the selected routes).
    rship = await _latest_ridership(db, rids)

    return OverviewResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(),
        range_end=rng.end.isoformat(),
        on_time_pct=MetricWithDelta(value=_pct_on_time(cur), previous=_pct_on_time(prev)),
        avg_delay_seconds=MetricWithDelta(value=_avg_delay(cur), previous=_avg_delay(prev)),
        delay_stddev_seconds=_stddev(cur),
        service_delivered_pct=MetricWithDelta(value=delivered, previous=delivered_prev),
        observed_trips=int(observed),
        scheduled_trips=int(sched),
        routes_tracked=int(cur["routes"]),
        total_observations=int(cur["observations"]),
        latest_ridership_month=rship[0],
        latest_ridership_total=rship[1],
        prev_ridership_total=rship[2],
    )


# ── On-time trend ───────────────────────────────────────────────────────────

@router.get("/ontime/trend", response_model=TrendResponse)
async def ontime_trend(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    granularity: Annotated[str, Query(pattern="^(hour|day)$")] = "day",
) -> TrendResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=14)
    if granularity == "hour":
        t_expr = "bucket"
    else:
        t_expr = f"date_trunc('day', bucket AT TIME ZONE '{_TZ}')"

    sql = f"""
        SELECT
            {t_expr}                                       AS t,
            sum(on_time)::bigint                           AS on_time,
            sum(slightly_late + late + very_late)::bigint AS late,
            sum(very_early + early)::bigint                AS early,
            sum(observations)::bigint                      AS observations,
            sum(delay_sum)::bigint                         AS delay_sum
        FROM trip_ontime_hourly
        WHERE bucket >= :start AND bucket < :end
          AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
        GROUP BY t
        ORDER BY t
    """
    rows = (await db.execute(
        text(sql).bindparams(
            bindparam("start"), bindparam("end"), bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": rng.start_at, "end": rng.end_at, "route_ids": rids},
    )).all()

    points: list[TrendPoint] = []
    for t, on_time, late, early, obs, dsum in rows:
        total = (on_time or 0) + (late or 0) + (early or 0)
        points.append(TrendPoint(
            t=t.isoformat() if hasattr(t, "isoformat") else str(t),
            on_time_pct=round(100 * (on_time or 0) / total, 1) if total else 0.0,
            avg_delay_seconds=round((dsum or 0) / obs, 1) if obs else 0.0,
            observations=int(obs or 0),
        ))
    return TrendResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(),
        range_end=rng.end.isoformat(),
        granularity=granularity, route_id=route_id, points=points,
    )


# ── Heatmap (hour × day-of-week, local time) ────────────────────────────────

@router.get("/ontime/heatmap", response_model=HeatmapResponse)
async def ontime_heatmap(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> HeatmapResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=30)
    sql = f"""
        SELECT
            EXTRACT(dow  FROM bucket AT TIME ZONE '{_TZ}')::int AS dow,
            EXTRACT(hour FROM bucket AT TIME ZONE '{_TZ}')::int AS hour,
            sum(on_time)::bigint                           AS on_time,
            sum(slightly_late + late + very_late)::bigint AS late,
            sum(very_early + early)::bigint                AS early,
            sum(observations)::bigint                      AS observations,
            sum(delay_sum)::bigint                         AS delay_sum
        FROM trip_ontime_hourly
        WHERE bucket >= :start AND bucket < :end
          AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
        GROUP BY dow, hour
    """
    rows = (await db.execute(
        text(sql).bindparams(
            bindparam("start"), bindparam("end"), bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": rng.start_at, "end": rng.end_at, "route_ids": rids},
    )).all()

    cells = []
    for dow, hour, on_time, late, early, obs, dsum in rows:
        total = (on_time or 0) + (late or 0) + (early or 0)
        cells.append(HeatmapCell(
            dow=int(dow), hour=int(hour),
            on_time_pct=round(100 * (on_time or 0) / total, 1) if total else 0.0,
            avg_delay_seconds=round((dsum or 0) / obs, 1) if obs else 0.0,
            observations=int(obs or 0),
        ))
    return HeatmapResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(),
        range_end=rng.end.isoformat(),
        route_id=route_id, cells=cells,
    )


# ── Delay distribution ──────────────────────────────────────────────────────

@router.get("/delay/distribution", response_model=DistributionResponse)
async def delay_distribution(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> DistributionResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=7)
    sql = """
        SELECT
            sum(very_early)::bigint    AS very_early,
            sum(early)::bigint         AS early,
            sum(on_time)::bigint       AS on_time,
            sum(slightly_late)::bigint AS slightly_late,
            sum(late)::bigint          AS late,
            sum(very_late)::bigint     AS very_late,
            sum(observations)::bigint  AS observations,
            sum(delay_sum)::bigint     AS delay_sum,
            sum(delay_sumsq)::numeric  AS delay_sumsq
        FROM trip_ontime_hourly
        WHERE bucket >= :start AND bucket < :end
          AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
    """
    row = (await db.execute(
        text(sql).bindparams(
            bindparam("start"), bindparam("end"), bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": rng.start_at, "end": rng.end_at, "route_ids": rids},
    )).one()
    counts = {k: (row[i] or 0) for i, (k, _) in enumerate(_DELAY_BINS)}
    obs = row[6] or 0
    dsum = row[7] or 0
    dsumsq = float(row[8] or 0)
    total = sum(counts.values())
    mean = dsum / obs if obs else 0.0
    var = (dsumsq / obs - mean * mean) if obs else 0.0
    stddev = round(math.sqrt(var), 1) if var > 0 else 0.0

    bins = [
        DistributionBin(
            key=key, label=label, count=int(counts[key]),
            pct=round(100 * counts[key] / total, 1) if total else 0.0,
        )
        for key, label in _DELAY_BINS
    ]
    return DistributionResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(), range_end=rng.end.isoformat(),
        route_id=route_id, total=int(total),
        avg_delay_seconds=round(mean, 1), stddev_seconds=stddev, bins=bins,
    )


# ── Worst stops ─────────────────────────────────────────────────────────────

@router.get("/stops/worst", response_model=WorstStopsResponse)
async def worst_stops(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 15,
    min_observations: Annotated[int, Query(ge=1)] = 20,
) -> WorstStopsResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=14)
    sql = """
        SELECT
            stop_id,
            sum(on_time)::bigint      AS on_time,
            sum(late)::bigint         AS late,
            sum(observations)::bigint AS observations,
            sum(delay_sum)::bigint    AS delay_sum
        FROM stop_delay_daily
        WHERE bucket >= :start AND bucket < :end
          AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
        GROUP BY stop_id
        HAVING sum(observations) >= :min_obs
        ORDER BY (sum(delay_sum)::float / NULLIF(sum(observations), 0)) DESC
        LIMIT :limit
    """
    rows = (await db.execute(
        text(sql).bindparams(
            bindparam("start"), bindparam("end"), bindparam("route_ids", type_=ARRAY(String)),
            bindparam("min_obs"), bindparam("limit"),
        ),
        {"start": rng.start_at, "end": rng.end_at, "route_ids": rids,
         "min_obs": min_observations, "limit": limit},
    )).all()

    _, stops_static = load_gtfs_static_data()
    stops = []
    for stop_id, on_time, late, obs, dsum in rows:
        total = obs or 0
        stops.append(WorstStop(
            stop_id=stop_id,
            stop_name=stops_static.get(stop_id, {}).get("stop_name"),
            route_id=route_id,
            observations=int(total),
            on_time_pct=round(100 * (on_time or 0) / total, 1) if total else 0.0,
            avg_delay_seconds=round((dsum or 0) / total, 1) if total else 0.0,
        ))
    return WorstStopsResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(), range_end=rng.end.isoformat(),
        route_id=route_id, stops=stops,
    )


# ── Service delivery (operated vs scheduled) ────────────────────────────────

@router.get("/service-delivery", response_model=ServiceDeliveryResponse)
async def service_delivery(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    route_ids: Annotated[str | None, Query()] = None,
    modes: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> ServiceDeliveryResponse:
    rids = resolve_route_ids(route_id, route_ids, modes)
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=7)

    sql = """
        SELECT route_id, count(*)::bigint AS trips
        FROM trip_activity_daily
        WHERE bucket >= :start AND bucket < :end
          AND (:route_ids IS NULL OR route_id = ANY(:route_ids))
        GROUP BY route_id
    """
    rows = (await db.execute(
        text(sql).bindparams(
            bindparam("start"), bindparam("end"), bindparam("route_ids", type_=ARRAY(String)),
        ),
        {"start": rng.start_at, "end": rng.end_at, "route_ids": rids},
    )).all()

    routes_static, _ = load_gtfs_static_data()
    day_counts = _count_daytypes(
        datetime.combine(rng.start, datetime.min.time()),
        datetime.combine(rng.end, datetime.min.time()),
    )

    results: list[ServiceDeliveryRoute] = []
    total_observed = 0
    total_scheduled = 0
    for rid, observed in rows:
        scheduled = _scheduled_trips(rid, day_counts)
        total_observed += int(observed)
        total_scheduled += scheduled
        results.append(ServiceDeliveryRoute(
            route_id=rid,
            route_short_name=_route_name(routes_static, rid),
            observed_trips=int(observed),
            scheduled_trips=scheduled,
            delivered_pct=round(min(100.0, 100 * observed / scheduled), 1) if scheduled else 0.0,
        ))
    results.sort(key=lambda r: r.delivered_pct)
    return ServiceDeliveryResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(), range_end=rng.end.isoformat(),
        observed_trips=total_observed,
        scheduled_trips=total_scheduled,
        delivered_pct=round(min(100.0, 100 * total_observed / total_scheduled), 1) if total_scheduled else 0.0,
        routes=results,
    )


# ── Scheduled frequency (static) ────────────────────────────────────────────

@router.get("/frequency/schedule", response_model=ScheduleFrequencyResponse)
async def schedule_frequency(
    route_id: Annotated[str | None, Query()] = None,
) -> ScheduleFrequencyResponse:
    summary = load_schedule_summary()
    routes_static, _ = load_gtfs_static_data()

    def _to_schema(rid: str, s: dict[str, Any], with_hours: bool) -> ScheduleFrequencyRoute:
        return ScheduleFrequencyRoute(
            route_id=rid,
            route_short_name=_route_name(routes_static, rid),
            weekday_trips=s["weekday_trips"],
            saturday_trips=s["saturday_trips"],
            sunday_trips=s["sunday_trips"],
            span_start=s["service_span"]["start"],
            span_end=s["service_span"]["end"],
            headways_by_hour=(
                [HourHeadway(hour=h, headway_minutes=s["headways_by_hour"].get(h))
                 for h in range(24)] if with_hours else []
            ),
        )

    if route_id:
        s = summary.get(route_id)
        routes = [_to_schema(route_id, s, True)] if s else []
        return ScheduleFrequencyResponse(route_id=route_id, routes=routes)

    routes = [_to_schema(rid, s, False) for rid, s in summary.items() if s["weekday_trips"]]
    routes.sort(key=lambda r: r.route_short_name)
    return ScheduleFrequencyResponse(route_id=None, routes=routes)


# ── Occupancy / crowding ────────────────────────────────────────────────────
#
# Reads occupancy_status_hourly (migration 008), a continuous aggregate keyed
# by route_id/trip_id/hour with a count per raw GTFS-RT occupancy_status value.
# Keeping trip_id in the cagg (mirroring trip_activity_daily's own precedent)
# is what lets direction filtering keep working post-aggregation: direction
# has no column of its own on vehicle_positions, it's only ever resolved from
# GTFS-static trip_id lists (see _occ_trip_ids), so the cagg needs trip_id to
# stay filterable by the same lists.

_OCC_TOTALS_SQL = """
    SELECT
        sum(empty)::bigint         AS empty,
        sum(many_seats)::bigint    AS many_seats,
        sum(few_seats)::bigint     AS few_seats,
        sum(standing)::bigint      AS standing,
        sum(crushed)::bigint       AS crushed,
        sum("full")::bigint        AS full,
        sum(not_accepting)::bigint AS not_accepting,
        sum(unknown)::bigint       AS unknown,
        sum(samples)::bigint       AS samples
    FROM occupancy_status_hourly
    WHERE bucket >= :start AND bucket < :end
      AND (:route_id IS NULL OR route_id = :route_id)
"""

_OCC_HOUR_SQL = f"""
    SELECT
        EXTRACT(hour FROM bucket AT TIME ZONE '{_TZ}')::int AS hour,
        sum(empty)::bigint         AS empty,
        sum(many_seats)::bigint    AS many_seats,
        sum(few_seats)::bigint     AS few_seats,
        sum(standing)::bigint      AS standing,
        sum(crushed)::bigint       AS crushed,
        sum("full")::bigint        AS full,
        sum(not_accepting)::bigint AS not_accepting,
        sum(unknown)::bigint       AS unknown,
        sum(samples)::bigint       AS total
    FROM occupancy_status_hourly
    WHERE bucket >= :start AND bucket < :end
      AND (:route_id IS NULL OR route_id = :route_id)
"""

_OCC_HOUR_SQL_TAIL = "    GROUP BY 1\n    ORDER BY 1\n"


def _occ_trip_ids(route_id: str | None, direction: int | None) -> list[str] | None:
    """Return trip_id list for a direction filter, or None when no filter needed."""
    if route_id is None or direction is None:
        return None
    dir_info = load_route_direction_info().get(route_id, {})
    entry = dir_info.get(str(direction))
    return entry["trip_ids"] if entry else []


# occupancy_status_hourly is a continuous aggregate, so a wide window is cheap
# — the cache below is just burst protection for concurrent dashboard tabs,
# same idea as the alerts cache, not a workaround for a slow raw-table scan.
# TTL matches the frontend's 5-min analytics polling; key space is capped since
# route_id/start/end are client-controlled.
_OCC_CACHE_TTL_SECONDS = 300.0
_OCC_CACHE_MAX_KEYS = 512
_OccCacheKey = tuple[str | None, date, date, int | None]
_occ_cache: dict[_OccCacheKey, tuple[float, OccupancyResponse]] = {}
_occ_locks: dict[_OccCacheKey, asyncio.Lock] = {}


@router.get("/occupancy", response_model=OccupancyResponse)
async def occupancy(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    direction: Annotated[int | None, Query(ge=0, le=1)] = None,
) -> OccupancyResponse:
    settings = get_settings()
    rng = resolve_range(start, end, max_span_days=settings.dashboard_max_span_days, default_days=7)
    key: _OccCacheKey = (route_id, rng.start, rng.end, direction)
    hit = _occ_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _OCC_CACHE_TTL_SECONDS:
        return hit[1]

    lock = _occ_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _occ_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _OCC_CACHE_TTL_SECONDS:
            return hit[1]
        resp = await _build_occupancy(db, route_id, rng, direction)
        if len(_occ_cache) >= _OCC_CACHE_MAX_KEYS:
            now = time.monotonic()
            for k in [k for k, (t, _) in _occ_cache.items() if now - t >= _OCC_CACHE_TTL_SECONDS]:
                _occ_cache.pop(k, None)
                stale_lock = _occ_locks.get(k)
                if stale_lock is not None and not stale_lock.locked():
                    _occ_locks.pop(k, None)
        _occ_cache[key] = (time.monotonic(), resp)
        return resp


async def _build_occupancy(
    db: AsyncSession,
    route_id: str | None,
    rng: ResolvedRange,
    direction: int | None,
) -> OccupancyResponse:
    trip_ids = _occ_trip_ids(route_id, direction)

    # Build direction info for the route regardless of direction filter
    directions: list[DirectionInfo] = []
    if route_id:
        dir_info = load_route_direction_info().get(route_id, {})
        for did_str, info in sorted(dir_info.items()):
            try:
                directions.append(DirectionInfo(
                    direction_id=int(did_str), headsign=info["headsign"]
                ))
            except (ValueError, KeyError):
                pass

    # If direction was specified but no trips found, return empty response
    if trip_ids is not None and len(trip_ids) == 0:
        return OccupancyResponse(
            period_days=rng.span_days,
            range_start=rng.start.isoformat(), range_end=rng.end.isoformat(),
            route_id=route_id, direction=direction,
            reported=False, directions=directions,
        )

    use_trip_filter = trip_ids is not None

    def _add_trip_filter(sql: str) -> str:
        if not use_trip_filter:
            return sql
        return sql + "\n      AND trip_id = ANY(:trip_ids)"

    params_base: dict[str, Any] = {
        "start": rng.start_at, "end": rng.end_at, "route_id": route_id,
    }
    trip_bp = [bindparam("trip_ids", type_=ARRAY(String))] if use_trip_filter else []
    if use_trip_filter:
        params_base["trip_ids"] = trip_ids

    totals_sql = _add_trip_filter(_OCC_TOTALS_SQL)
    row = (await db.execute(
        text(totals_sql).bindparams(
            bindparam("start"), bindparam("end"), bindparam("route_id", type_=String), *trip_bp,
        ),
        params_base,
    )).one()

    (empty, many_seats, few_seats, standing, crushed, full_cnt,
     not_accepting, unknown, samples) = (int(v or 0) for v in row)

    low = empty + many_seats
    medium = few_seats
    high = standing + crushed + full_cnt + not_accepting
    total_known = low + medium + high

    hour_sql = _add_trip_filter(_OCC_HOUR_SQL) + _OCC_HOUR_SQL_TAIL
    hour_rows = (await db.execute(
        text(hour_sql).bindparams(
            bindparam("start"), bindparam("end"), bindparam("route_id", type_=String), *trip_bp,
        ),
        params_base,
    )).all()

    by_hour = []
    for hr_row in hour_rows:
        (h, e, ms, fs, st, cr, fl, na, unk, tot) = (
            int(hr_row[i] or 0) for i in range(10)
        )
        hr_total = e + ms + fs + st + cr + fl + na
        if hr_total == 0:
            continue
        by_hour.append(OccupancyHourPoint(
            hour=h,
            empty=e, many_seats=ms, few_seats=fs,
            standing=st, crushed=cr, full=fl, not_accepting=na,
            unknown=unk, total=tot,
        ))

    standing_pct = round(100 * high / total_known, 1) if total_known else None

    return OccupancyResponse(
        period_days=rng.span_days,
        range_start=rng.start.isoformat(), range_end=rng.end.isoformat(),
        route_id=route_id,
        direction=direction,
        reported=total_known > 0,
        empty=empty, many_seats=many_seats, few_seats=few_seats,
        standing=standing, crushed=crushed, full=full_cnt,
        not_accepting=not_accepting,
        low=low, medium=medium, high=high,
        unknown=unknown, samples=samples,
        standing_pct=standing_pct,
        by_hour=by_hour,
        directions=directions,
    )


# ── Ridership ───────────────────────────────────────────────────────────────

async def _latest_ridership(db: AsyncSession, route_ids: list[str] | None) -> tuple[str | None, int | None, int | None]:
    """Return (latest_month_iso, latest_total, prev_total) for the system or the
    given routes (summed)."""
    stmt = select(RidershipMonthly.month, RidershipMonthly.boardings)
    if route_ids:
        stmt = stmt.where(RidershipMonthly.route_id.in_(route_ids))
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None, None, None
    by_month: dict[Any, int] = {}
    for month, boardings in rows:
        by_month[month] = by_month.get(month, 0) + (boardings or 0)
    months = sorted(by_month.keys())
    latest = months[-1]
    prev = months[-2] if len(months) > 1 else None
    return latest.isoformat(), by_month[latest], (by_month[prev] if prev else None)


@router.get("/ridership", response_model=RidershipResponse)
async def ridership(
    db: Annotated[AsyncSession, Depends(get_db)],
    route_id: Annotated[str | None, Query()] = None,
    months: Annotated[int, Query(ge=1, le=60)] = 24,
) -> RidershipResponse:
    routes_static, _ = load_gtfs_static_data()

    # Series (system total or single route) by month.
    stmt = select(RidershipMonthly.month, RidershipMonthly.boardings)
    if route_id:
        stmt = stmt.where(RidershipMonthly.route_id == route_id)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return RidershipResponse(route_id=route_id, available=False, series=[], by_route_latest=[])

    by_month: dict[Any, int] = {}
    for month, boardings in rows:
        by_month[month] = by_month.get(month, 0) + (boardings or 0)
    ordered = sorted(by_month.keys())[-months:]
    series = [RidershipPoint(month=m.isoformat(), boardings=by_month[m]) for m in ordered]

    # Latest-month boardings per route (system view only).
    by_route_latest: list[RidershipRoute] = []
    latest_month = sorted(by_month.keys())[-1]
    latest_rows = (await db.execute(
        select(RidershipMonthly.route_id, RidershipMonthly.boardings)
        .where(RidershipMonthly.month == latest_month)
    )).all()
    for rid, boardings in latest_rows:
        by_route_latest.append(RidershipRoute(
            route_id=rid, route_short_name=_route_name(routes_static, rid),
            boardings=boardings or 0,
        ))
    by_route_latest.sort(key=lambda r: r.boardings, reverse=True)

    latest, latest_total, prev_total = await _latest_ridership(db, [route_id] if route_id else None)
    return RidershipResponse(
        route_id=route_id, available=True,
        latest_month=latest, latest_total=latest_total, prev_total=prev_total,
        series=series, by_route_latest=by_route_latest,
    )
