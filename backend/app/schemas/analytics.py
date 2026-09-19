"""Response schemas for the deep-analytics endpoints (api/v1/analytics.py)."""
from __future__ import annotations

from pydantic import BaseModel


# ── Overview (hero KPIs) ────────────────────────────────────────────────────

class MetricWithDelta(BaseModel):
    value: float
    previous: float | None = None


class OverviewResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    on_time_pct: MetricWithDelta
    avg_delay_seconds: MetricWithDelta
    delay_stddev_seconds: float
    service_delivered_pct: MetricWithDelta
    observed_trips: int
    scheduled_trips: int
    routes_tracked: int
    total_observations: int
    latest_ridership_month: str | None = None
    latest_ridership_total: int | None = None
    prev_ridership_total: int | None = None


# ── On-time trend ───────────────────────────────────────────────────────────

class TrendPoint(BaseModel):
    t: str
    on_time_pct: float
    avg_delay_seconds: float
    observations: int


class TrendResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    granularity: str
    route_id: str | None = None
    points: list[TrendPoint]


# ── Heatmap (hour-of-day × day-of-week) ─────────────────────────────────────

class HeatmapCell(BaseModel):
    dow: int          # 0=Sunday … 6=Saturday (local time)
    hour: int         # 0–23 (local time)
    on_time_pct: float
    avg_delay_seconds: float
    observations: int


class HeatmapResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    route_id: str | None = None
    cells: list[HeatmapCell]


# ── Delay distribution ──────────────────────────────────────────────────────

class DistributionBin(BaseModel):
    key: str
    label: str
    count: int
    pct: float


class DistributionResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    route_id: str | None = None
    total: int
    avg_delay_seconds: float
    stddev_seconds: float
    bins: list[DistributionBin]


# ── Worst stops ─────────────────────────────────────────────────────────────

class WorstStop(BaseModel):
    stop_id: str
    stop_name: str | None
    route_id: str | None = None
    observations: int
    on_time_pct: float
    avg_delay_seconds: float


class WorstStopsResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    route_id: str | None = None
    stops: list[WorstStop]


# ── Service delivery ────────────────────────────────────────────────────────

class ServiceDeliveryRoute(BaseModel):
    route_id: str
    route_short_name: str
    observed_trips: int
    scheduled_trips: int
    delivered_pct: float


class ServiceDeliveryResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    observed_trips: int
    scheduled_trips: int
    delivered_pct: float
    routes: list[ServiceDeliveryRoute]


# ── Scheduled frequency ─────────────────────────────────────────────────────

class HourHeadway(BaseModel):
    hour: int
    headway_minutes: float | None


class ScheduleFrequencyRoute(BaseModel):
    route_id: str
    route_short_name: str
    weekday_trips: int
    saturday_trips: int
    sunday_trips: int
    span_start: str | None
    span_end: str | None
    headways_by_hour: list[HourHeadway] = []


class ScheduleFrequencyResponse(BaseModel):
    route_id: str | None = None
    routes: list[ScheduleFrequencyRoute]


# ── Occupancy / crowding ────────────────────────────────────────────────────

class OccupancyHourPoint(BaseModel):
    hour: int
    # individual GTFS-RT occupancy codes
    empty: int = 0
    many_seats: int = 0
    few_seats: int = 0
    standing: int = 0
    crushed: int = 0
    full: int = 0
    not_accepting: int = 0
    unknown: int = 0
    total: int = 0


class DirectionInfo(BaseModel):
    direction_id: int
    headsign: str


class OccupancyResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    route_id: str | None = None
    direction: int | None = None
    reported: bool          # False when RTD populates no occupancy data
    # individual code totals
    empty: int = 0
    many_seats: int = 0
    few_seats: int = 0
    standing: int = 0
    crushed: int = 0
    full: int = 0
    not_accepting: int = 0
    # legacy band totals (kept for KPI card: standing_pct)
    low: int = 0
    medium: int = 0
    high: int = 0
    unknown: int = 0
    samples: int = 0
    standing_pct: float | None = None
    by_hour: list[OccupancyHourPoint] = []
    directions: list[DirectionInfo] = []


# ── Ridership ───────────────────────────────────────────────────────────────

class RidershipPoint(BaseModel):
    month: str
    boardings: int


class RidershipRoute(BaseModel):
    route_id: str
    route_short_name: str
    boardings: int


class RidershipResponse(BaseModel):
    route_id: str | None = None
    available: bool         # False when no ridership has been imported
    latest_month: str | None = None
    latest_total: int | None = None
    prev_total: int | None = None
    series: list[RidershipPoint]
    by_route_latest: list[RidershipRoute]
