from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OnTimeRouteStats(BaseModel):
    route_id: str
    route_short_name: str
    total_observations: int
    on_time: int
    late: int
    early: int
    on_time_pct: float
    avg_delay_seconds: float


class OverallOnTime(BaseModel):
    on_time_pct: float
    avg_delay_seconds: float


class OnTimeResponse(BaseModel):
    period_days: int
    range_start: str | None = None
    range_end: str | None = None
    routes: list[OnTimeRouteStats]
    overall: OverallOnTime


class FrequencyRouteStats(BaseModel):
    route_id: str
    route_short_name: str
    avg_headway_minutes: float
    min_headway_minutes: float
    max_headway_minutes: float
    vehicle_count: int


class FrequencyResponse(BaseModel):
    computed_at: datetime
    routes: list[FrequencyRouteStats]


class StuckAlert(BaseModel):
    vehicle_id: str | None
    vehicle_label: str | None
    route_id: str
    route_short_name: str
    latitude: float | None
    longitude: float | None
    stop_id: str | None
    stop_name: str | None
    stuck_since: datetime
    minutes_stuck: float


class AlertsResponse(BaseModel):
    computed_at: datetime
    alerts: list[StuckAlert]
