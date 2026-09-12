"""Occupancy: replace occupancy_hourly with a per-status, per-trip cagg.

Revision ID: 008
Revises: 007
Create Date: 2026-09-12

occupancy_hourly (migration 003) collapsed the 7 raw GTFS-RT occupancy_status
values into 3 coarse bands and grouped only by route_id/hour — too little
detail for the /stats/occupancy endpoint, which has always queried raw
vehicle_positions directly instead (see analytics.py). Nothing else reads
occupancy_hourly, so it's dropped here rather than left as dead weight.

occupancy_status_hourly replaces it: one row per route_id/trip_id/hour with a
count for each of the 7 raw occupancy_status values. Keeping trip_id in the
GROUP BY (the same COUNT(DISTINCT)-workaround trip_activity_daily already
uses) is what lets /stats/occupancy keep filtering by direction after the
move to a cagg — direction has no column of its own on vehicle_positions, it's
only ever resolved from GTFS-static trip_id lists, so the cagg needs trip_id
to stay filterable by those same lists.

Like every cagg in this project, DDL runs inside autocommit_block() because it
can't execute inside a transaction (see migration 002/003).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OCC_STATUS_HOURLY_SELECT = """
    SELECT
        route_id,
        trip_id,
        time_bucket(INTERVAL '1 hour', timestamp) AS bucket,
        count(*) FILTER (WHERE occupancy_status = 'EMPTY')                        AS empty,
        count(*) FILTER (WHERE occupancy_status = 'MANY_SEATS_AVAILABLE')         AS many_seats,
        count(*) FILTER (WHERE occupancy_status = 'FEW_SEATS_AVAILABLE')          AS few_seats,
        count(*) FILTER (WHERE occupancy_status = 'STANDING_ROOM_ONLY')           AS standing,
        count(*) FILTER (WHERE occupancy_status = 'CRUSHED_STANDING_ROOM_ONLY')   AS crushed,
        count(*) FILTER (WHERE occupancy_status = 'FULL')                         AS full,
        count(*) FILTER (WHERE occupancy_status = 'NOT_ACCEPTING_PASSENGERS')     AS not_accepting,
        count(*) FILTER (
            WHERE occupancy_status IS NULL OR occupancy_status = 'UNKNOWN'
        )                                                                          AS unknown,
        count(*)                                                                   AS samples
    FROM vehicle_positions
    WHERE trip_id IS NOT NULL
    GROUP BY route_id, trip_id, bucket
"""

# occupancy_hourly's migration-003 definition, recreated verbatim by downgrade().
_OCCUPANCY_HOURLY_SELECT = """
    SELECT
        route_id,
        time_bucket(INTERVAL '1 hour', timestamp) AS bucket,
        count(*) FILTER (
            WHERE occupancy_status IN ('EMPTY', 'MANY_SEATS_AVAILABLE')
        )                                                  AS low,
        count(*) FILTER (
            WHERE occupancy_status = 'FEW_SEATS_AVAILABLE'
        )                                                  AS medium,
        count(*) FILTER (
            WHERE occupancy_status IN (
                'STANDING_ROOM_ONLY', 'CRUSHED_STANDING_ROOM_ONLY',
                'FULL', 'NOT_ACCEPTING_PASSENGERS'
            )
        )                                                  AS high,
        count(*) FILTER (
            WHERE occupancy_status IS NULL
               OR occupancy_status = 'UNKNOWN'
        )                                                  AS unknown,
        count(*)                                           AS samples
    FROM vehicle_positions
    GROUP BY route_id, bucket
"""


def _create_cagg(name: str, select_sql: str, *, backfill_days: int = 90,
                 start_offset: str, end_offset: str, schedule: str) -> None:
    """Create a continuous aggregate with real-time aggregation, backfill, and
    a refresh policy. Must be called inside an autocommit_block().

    backfill_days is intentionally modest (default 90): a full-history backfill
    over a year of raw vehicle_positions can block migration startup for many
    minutes (see migration 002). The refresh policy then keeps it current
    going forward — trip_ontime_hourly/stop_delay_daily only look like they
    hold a full year today because months have passed since migration 003
    created them with this same 90-day default.
    """
    op.execute(
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {name} "
        f"WITH (timescaledb.continuous) AS {select_sql} WITH NO DATA;"
    )
    op.execute(
        f"ALTER MATERIALIZED VIEW {name} "
        f"SET (timescaledb.materialized_only = false);"
    )
    op.execute(
        f"CALL refresh_continuous_aggregate('{name}',"
        f" NOW() - INTERVAL '{backfill_days} days', NOW());"
    )
    op.execute(
        f"SELECT add_continuous_aggregate_policy('{name}',"
        f" start_offset => INTERVAL '{start_offset}',"
        f" end_offset   => INTERVAL '{end_offset}',"
        f" schedule_interval => INTERVAL '{schedule}',"
        f" if_not_exists => TRUE);"
    )


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS occupancy_hourly;")
        _create_cagg(
            "occupancy_status_hourly", _OCC_STATUS_HOURLY_SELECT,
            start_offset="3 days", end_offset="1 hour", schedule="1 hour",
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS occupancy_status_hourly;")
        _create_cagg(
            "occupancy_hourly", _OCCUPANCY_HOURLY_SELECT,
            start_offset="3 days", end_offset="1 hour", schedule="1 hour",
        )
