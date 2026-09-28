from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class VehiclePosition(Base):
    """One row = one GTFS-RT vehicle snapshot.

    TimescaleDB converts this into a hypertable partitioned on *timestamp*
    (see alembic migration 001).
    """

    __tablename__ = "vehicle_positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Trip / route identification
    trip_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Vehicle identification
    vehicle_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vehicle_label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Position
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    bearing: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Stop info
    current_stop_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 0=INCOMING_AT  1=STOPPED_AT  2=IN_TRANSIT_TO
    current_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Occupancy
    occupancy_status: Mapped[str | None] = mapped_column(String(48), nullable=True)

    # Time (used as TimescaleDB partition key)
    timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    # Set by services/position_anomaly.py when this fix implied a speed no
    # real vehicle could have covered since its last trusted one — a GPS
    # glitch, not a genuine position. Never deleted, only flagged, so the raw
    # feed stays available for tuning; every reader (map, trip replay, on-time
    # detection, the occupancy cagg) filters these out.
    is_anomalous: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_vp_route_ts", "route_id", "timestamp"),
        Index("ix_vp_vehicle_ts", "vehicle_id", "timestamp"),
    )
