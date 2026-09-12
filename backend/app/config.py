from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Enables dev-only surfaces (the legacy /realtime/decode file endpoint).
    debug: bool = False

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://transitden:transitden@localhost:5432/transitden"
    # Server-side cap on how long any single query may run. Protects the small
    # connection pool from being pinned by runaway scans. 0 disables (useful for
    # manual backfill scripts).
    statement_timeout_ms: int = 10_000

    # ── Abuse / resource limits ───────────────────────────────────────────────
    rate_limit_enabled: bool = True
    # Per-client-IP fixed-window (60 s) request budgets, bucketed by path.
    rate_limit_default_per_minute: int = 120
    rate_limit_expensive_per_minute: int = 20
    rate_limit_export_per_minute: int = 5
    # Simulator login is the only guessable endpoint on the server, so it gets
    # its own small budget rather than sharing the general one.
    rate_limit_sim_auth_per_minute: int = 5
    # Trust forwarding headers for client identity (true when behind Caddy/Next).
    trust_proxy_headers: bool = True
    # Header carrying the real client IP, checked before X-Forwarded-For.
    # Cloudflare sets CF-Connecting-IP itself, overwriting anything the client
    # sent, and Caddy and Next pass it through — so it survives the proxy chain
    # where X-Forwarded-For does not.  Without it every visitor rate-limits
    # against the same key (see middleware/rate_limit.py).  Set empty to
    # disable when no such edge is in front.
    client_ip_header: str = "cf-connecting-ip"
    # Widest time span a single request may ask a raw-hypertable scan to cover.
    export_max_span_days: int = 31
    historical_max_span_days: int = 7
    vehicles_max_span_hours: int = 72
    # Dashboard analytics endpoints read continuous aggregates (pre-rolled
    # rollups, not raw rows), so a wide window is cheap — this is a generous
    # ceiling rather than a performance guard, sized to "about a year" so the
    # calendar picker's "Last year" preset always fits.
    dashboard_max_span_days: int = 366
    # How far back raw rows still exist.  Mirrors the retention policy created in
    # migration 002 (add_retention_policy, INTERVAL '365 days'); it is published
    # via /api/v1/meta/limits purely so date pickers can grey out days we know
    # hold no data.  Change the migration if you want the real retention moved.
    data_retention_days: int = 365

    # ── RTD GTFS-RT feed URLs ─────────────────────────────────────────────────
    gtfs_rt_vehicle_url: str = (
        "https://www.rtd-denver.com/files/gtfs-rt/VehiclePosition.pb"
    )
    gtfs_rt_trip_url: str = (
        "https://www.rtd-denver.com/files/gtfs-rt/TripUpdate.pb"
    )

    # ── Ingestion scheduler ───────────────────────────────────────────────────
    polling_interval_seconds: int = 30

    # ── Alert thresholds ─────────────────────────────────────────────────────
    stuck_vehicle_minutes: int = 12

    # ── On-time performance (observed position vs. static schedule) ──────────
    # A vehicle counts as "arrived" at a timepoint when within this many metres
    # of it; the observed arrival time is then compared to the scheduled time.
    # 152m ≈ 500ft.
    arrival_radius_m: int = 152
    # Rail gets a wider geofence (402m = 0.25mi).  Rail positions are reported
    # further from the platform than a bus's are from the kerb, and stations
    # sit kilometres apart rather than blocks (median 1775m), so a radius
    # sized for bus stops misses arrivals a train plainly made.
    #
    # 3.3% of adjacent rail timepoint pairs are closer together than this, so
    # a train can be inside two stations' circles at once.  That is fine and
    # not the same as ambiguity: the search takes the *nearest* timepoint by
    # along-route distance, so overlapping circles resolve to whichever
    # station the train is actually closer to.
    arrival_radius_rail_m: int = 402
    # An arrival within ±this many seconds of schedule is "on time".
    # RTD defines on-time as within 5 minutes.
    ontime_threshold_seconds: int = 300
    # Sanity guard: if the best schedule match is off by more than this, the
    # live trip_id probably doesn't match the static schedule for that day —
    # drop the event rather than record a bogus delay.
    arrival_max_delay_seconds: int = 10800
    # Trip_id-misassignment guard (services/ontime.py).  An arrival is only
    # discarded as a probable misassignment when BOTH hold: our own delay is at
    # least ..._min_delay_seconds, and another trip on the route is scheduled
    # within ..._max_gap_seconds of the observation — i.e. the sighting lands
    # almost exactly on a competing trip's slot, the headway-aliasing
    # signature.  Loosening either (especially raising max_gap toward the
    # headway) starts deleting ordinary late buses instead of recording them,
    # which blanks stops on the trip page and biases on-time stats optimistic.
    arrival_misassignment_min_delay_seconds: int = 600
    arrival_misassignment_max_gap_seconds: int = 60
    # Two consecutive fixes further apart than this are not read as one
    # continuous movement, so no arrival is interpolated between them —
    # inventing a crossing time across a long feed dropout would be a guess,
    # not a measurement. See classify_segment_arrivals in services/ontime.py.
    arrival_segment_max_gap_seconds: int = 300
    # The origin terminal is timed by *departure*, not arrival: a vehicle lays
    # over at the gate (already carrying its next trip_id) long before it pulls
    # out, so its first geofenced snapshot there is minutes too early.  It
    # counts as departed once it is this far from the origin stop; the crossing
    # time is interpolated between the last snapshot inside and the first
    # outside, so a 30 s poll interval doesn't cost a 30 s error.
    origin_departure_radius_m: int = 100
    # If a trip stops appearing in the feed while still parked at its origin, we
    # never see it leave.  After this much silence, fall back to recording the
    # last moment it was seen at the stop rather than losing the event.
    origin_departure_stale_minutes: int = 15
    # ── Terminus fallback ────────────────────────────────────────────────────
    # A trip whose feed cuts out short of its last stop never gets a terminus
    # arrival, and a missing terminus arrival is exactly what marks a finished
    # trip "Incomplete".  So when — and only when — the ordinary geofence above
    # never fired at the terminus, a second, wider circle is consulted once the
    # trip has gone quiet: if the vehicle's closest approach to the last stop
    # was inside it, that approach is recorded as the arrival.
    #
    # The fallback is always additionally capped at half the distance from the
    # second-to-last timepoint to the terminus (see TerminusFallbackTracker), so
    # however wide these are set the circle can never reach back far enough to
    # confuse the previous stop with the last one.
    arrival_terminus_fallback_radius_m: int = 750
    arrival_terminus_fallback_radius_rail_m: int = 1500
    # How long a trip must be absent from the feed before the fallback is
    # allowed to resolve it.  Too short and a vehicle merely idling on approach
    # gets an arrival it hasn't made yet; this matches the origin's own window.
    arrival_terminus_fallback_stale_minutes: int = 15

    # ── Denver home games (ESPN scoreboard) ──────────────────────────────────
    # Powers the map status carousel's game slides.  Home games only: an away
    # game doesn't move Denver ridership, so it never earns a turn.
    sports_enabled: bool = True
    # Cache TTL for the ESPN scoreboard, by how much is going on.  Idle is the
    # common case — most days have no home game at all, and re-asking every 30 s
    # about a day with nothing on is pure waste.
    sports_poll_seconds_live: int = 30
    sports_poll_seconds_soon: int = 60
    sports_poll_seconds_idle: int = 600
    # A finished game keeps its slide this long, then clears.
    sports_postgame_window_minutes: int = 120
    # Inside this many minutes of first pitch, the slide switches from a start
    # time ("7:00 PM") to a countdown ("in 45 min").
    sports_countdown_minutes: int = 60

    # ── Game simulator (unlisted /sim page) ──────────────────────────────────
    # Shared password for the simulator page.  Empty disables the simulator
    # outright: its routes 404 and no fake game can ever reach the map.  Set it
    # only on staging.
    sports_sim_password: str = ""
    # Hard ceiling on how long one simulation may run before it expires and
    # live ESPN data takes back over.
    sports_sim_max_minutes: int = 240

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:3000"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors(cls, v: object) -> list[str]:
        if isinstance(v, str):
            import json
            return json.loads(v)
        return v  # type: ignore[return-value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
