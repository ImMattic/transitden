"""Add is_anomalous to vehicle_positions; exclude it from occupancy_status_hourly.

Revision ID: 009
Revises: 008
Create Date: 2026-09-27

RTD's feed occasionally reports a vehicle somewhere it could not plausibly
have reached since its last fix, then a poll or two later reports it right
back — a GPS glitch, not a real move (see services/position_anomaly.py,
wired into ingestion.py from this point on). Rather than drop a suspect fix,
it is stored and flagged so the raw feed stays available for tuning, and
every reader excludes it: the live map, historical/export queries, trip
replay, on-time detection, and — recreated here — occupancy_status_hourly,
the one continuous aggregate that reads raw vehicle_positions directly for
a number the Dashboard shows.

No backfill: existing rows all get is_anomalous = false. This only affects
positions ingested after this migration runs; nothing reclassifies history.

Like every cagg in this project, DDL runs inside autocommit_block() because
it can't execute inside a transaction (see migration 002/003/008).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Same as migration 008's _OCC_STATUS_HOURLY_SELECT, with the anomaly filter added.
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
    WHERE trip_id IS NOT NULL AND NOT is_anomalous
    GROUP BY route_id, trip_id, bucket
"""

# migration 008's definition, recreated verbatim by downgrade().
_OCC_STATUS_HOURLY_SELECT_NO_FILTER = """
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


def _create_cagg(name: str, select_sql: str, *, backfill_days: int = 90,
                 start_offset: str, end_offset: str, schedule: str) -> None:
    """Mirrors migration 008's helper — must be called inside an autocommit_block()."""
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
    op.add_column(
        "vehicle_positions",
        sa.Column(
            "is_anomalous",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS occupancy_status_hourly;")
        _create_cagg(
            "occupancy_status_hourly", _OCC_STATUS_HOURLY_SELECT,
            start_offset="3 days", end_offset="1 hour", schedule="1 hour",
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS occupancy_status_hourly;")
        _create_cagg(
            "occupancy_status_hourly", _OCC_STATUS_HOURLY_SELECT_NO_FILTER,
            start_offset="3 days", end_offset="1 hour", schedule="1 hour",
        )

    op.drop_column("vehicle_positions", "is_anomalous")
