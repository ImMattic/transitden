from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.transit import gtfs_realtime_pb2


TRANSIT_FOLDERS = [
    "light_rail",
    "op_commuter_rail",
    "op_motorbus",
    "pur_commuter_rail",
    "pur_motorbus",
]

VEHICLE_TYPE_MAP = {
    "0": "light_rail",
    "1": "heavy_rail",
    "2": "commuter_rail",
    "3": "bus",
}

# Manual overrides for new RTD lines not yet present in the bundled GTFS static feeds.
# These supplement (or replace) anything loaded from the CSV files.
_ROUTE_OVERRIDES: dict[str, dict[str, str]] = {
    "101C": {
        "route_id": "101C",
        "route_short_name": "C",
        "route_long_name": "C Line",
        "route_type": "0",
        "route_color": "f79239",
        "agency_id": "RTD",
    },
    "101T": {
        "route_id": "101T",
        "route_short_name": "T",
        "route_long_name": "T Line",
        "route_type": "0",
        "route_color": "b71318",
        "agency_id": "RTD",
    },
}


def _safe_has_field(message: Any, field_name: str) -> bool:
    try:
        return message.HasField(field_name)
    except ValueError:
        # Some generated protobuf classes do not expose HasField on all scalars.
        return False


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_gtfs_static_root() -> Path:
    """Locate the gtfs-static directory across local and container layouts.

    In the Docker image the backend is copied to ``/app`` (so this file lives at
    ``/app/app/services/...`` and ``parents[3]`` is ``/``), while the static
    feeds are mounted at ``/app/gtfs-static``.  Locally ``parents[3]`` is the
    repo root.  Check the container mount first, then the local layout, then
    walk upward as a last resort.
    """
    current = Path(__file__).resolve()
    candidates = [Path("/app/gtfs-static"), current.parents[3] / "gtfs-static"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    for parent in current.parents:
        candidate = parent / "gtfs-static"
        if candidate.exists():
            return candidate
    return Path("/app/gtfs-static")


def load_gtfs_static_data(gtfs_static_root: Path | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    root = gtfs_static_root or resolve_gtfs_static_root()
    routes: dict[str, dict[str, Any]] = {}
    stops: dict[str, dict[str, Any]] = {}

    for folder in TRANSIT_FOLDERS:
        routes_file = root / folder / "routes.txt"
        if routes_file.exists():
            with routes_file.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    route_id = row.get("route_id")
                    if not route_id:
                        continue
                    routes[route_id] = {
                        "route_id": route_id,
                        "route_short_name": row.get("route_short_name", ""),
                        "route_long_name": row.get("route_long_name", ""),
                        "route_type": row.get("route_type", ""),
                        "route_color": row.get("route_color", ""),
                        "agency_id": row.get("agency_id", ""),
                    }

        stops_file = root / folder / "stops.txt"
        if stops_file.exists():
            with stops_file.open("r", encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    stop_id = row.get("stop_id")
                    if not stop_id:
                        continue
                    stops[stop_id] = {
                        "stop_id": stop_id,
                        "stop_name": row.get("stop_name", ""),
                        "stop_lat": float(row.get("stop_lat") or 0.0),
                        "stop_lon": float(row.get("stop_lon") or 0.0),
                    }

    routes.update(_ROUTE_OVERRIDES)
    return routes, stops


def load_trip_endpoint_sequences(gtfs_static_root: Path | None = None) -> tuple[
    dict[str, tuple[int, int]],
    dict[str, tuple[str | None, str | None]],
]:
    """Return endpoint data for all trips.

    Returns a pair:
      sequences — {trip_id: (min_stop_sequence, max_stop_sequence)}
      stop_ids  — {trip_id: (first_stop_id, last_stop_id)}

    Used to exclude vehicles that are legitimately waiting at route terminals.
    """
    root = gtfs_static_root or resolve_gtfs_static_root()
    sequences: dict[str, tuple[int, int]] = {}
    stop_ids: dict[str, tuple[str | None, str | None]] = {}

    for folder in TRANSIT_FOLDERS:
        f = root / folder / "stop_times.txt"
        if not f.exists():
            continue
        with f.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                trip_id = row.get("trip_id")
                seq_str = row.get("stop_sequence")
                stop_id = row.get("stop_id", "").strip() or None
                if not trip_id or not seq_str:
                    continue
                try:
                    seq = int(seq_str)
                except ValueError:
                    continue
                if trip_id in sequences:
                    lo, hi = sequences[trip_id]
                    first_sid, last_sid = stop_ids[trip_id]
                    if seq < lo:
                        sequences[trip_id] = (seq, hi)
                        stop_ids[trip_id] = (stop_id, last_sid)
                    elif seq > hi:
                        sequences[trip_id] = (lo, seq)
                        stop_ids[trip_id] = (first_sid, stop_id)
                else:
                    sequences[trip_id] = (seq, seq)
                    stop_ids[trip_id] = (stop_id, stop_id)
    return sequences, stop_ids


def _extract_occupancy(vehicle_pos: Any) -> str:
    if _safe_has_field(vehicle_pos, "occupancy_status"):
        enum_value = int(vehicle_pos.occupancy_status)
        return gtfs_realtime_pb2.VehiclePosition.OccupancyStatus.Name(enum_value)
    return "UNKNOWN"


def format_line_info(entities: Any, routes: dict[str, dict[str, Any]], stops: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lines_data: dict[str, dict[str, Any]] = defaultdict(lambda: {"vehicles": [], "route_info": None})

    for entity in entities:
        if not entity.HasField("vehicle"):
            continue

        vehicle_pos = entity.vehicle
        trip = vehicle_pos.trip if _safe_has_field(vehicle_pos, "trip") else None
        route_id = trip.route_id if trip else None

        if not route_id:
            continue

        has_position = _safe_has_field(vehicle_pos, "position")
        has_vehicle = _safe_has_field(vehicle_pos, "vehicle")
        stop_id = vehicle_pos.stop_id if _safe_has_field(vehicle_pos, "stop_id") else None
        timestamp = vehicle_pos.timestamp if _safe_has_field(vehicle_pos, "timestamp") else None

        vehicle_data: dict[str, Any] = {
            "trip_id": trip.trip_id if trip else None,
            "vehicle_label": vehicle_pos.vehicle.label if has_vehicle else None,
            "position": {
                "latitude": vehicle_pos.position.latitude if has_position else None,
                "longitude": vehicle_pos.position.longitude if has_position else None,
                "bearing": vehicle_pos.position.bearing if has_position and _safe_has_field(vehicle_pos.position, "bearing") else None,
            },
            "current_stop_sequence": vehicle_pos.current_stop_sequence if _safe_has_field(vehicle_pos, "current_stop_sequence") else None,
            "current_status": int(vehicle_pos.current_status) if _safe_has_field(vehicle_pos, "current_status") else None,
            "occupancy_status": _extract_occupancy(vehicle_pos),
            "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat() if timestamp else None,
        }

        if stop_id and stop_id in stops:
            vehicle_data["current_stop"] = stops[stop_id]

        lines_data[route_id]["vehicles"].append(vehicle_data)

    for route_id, line_data in lines_data.items():
        if route_id in routes:
            line_data["route_info"] = routes[route_id]

    return dict(lines_data)


def get_occupancy_summary(vehicles: list[dict[str, Any]]) -> dict[str, int]:
    occupancy_counts: dict[str, int] = defaultdict(int)
    for vehicle in vehicles:
        status = vehicle.get("occupancy_status") or "UNKNOWN"
        occupancy_counts[status] += 1
    return dict(occupancy_counts)


def format_output_by_type(lines_data: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output_by_type: dict[str, dict[str, Any]] = {
        "bus": {},
        "light_rail": {},
        "heavy_rail": {},
        "commuter_rail": {},
        "other": {},
    }

    for route_id, line_data in lines_data.items():
        route_info = line_data.get("route_info") or {}
        route_type = route_info.get("route_type", "")
        type_key = VEHICLE_TYPE_MAP.get(route_type, "other")
        output_by_type[type_key][route_id] = {
            "route": {
                "id": route_info.get("route_id", route_id),
                "short_name": route_info.get("route_short_name", ""),
                "long_name": route_info.get("route_long_name", ""),
                "type": route_type,
                "color": route_info.get("route_color", ""),
            },
            "vehicles": line_data.get("vehicles", []),
            "summary": {
                "total_vehicles": len(line_data.get("vehicles", [])),
                "occupancy_levels": get_occupancy_summary(line_data.get("vehicles", [])),
            },
        }

    return output_by_type


def decode_vehicle_positions(pb_file: Path, gtfs_static_root: Path | None = None) -> dict[str, dict[str, Any]]:
    feed = gtfs_realtime_pb2.FeedMessage()
    with pb_file.open("rb") as handle:
        feed.ParseFromString(handle.read())

    routes, stops = load_gtfs_static_data(gtfs_static_root=gtfs_static_root)
    lines_data = format_line_info(feed.entity, routes, stops)
    return format_output_by_type(lines_data)


# ---------------------------------------------------------------------------
# Bytes-based helpers used by the async ingestion pipeline
# ---------------------------------------------------------------------------

def extract_vehicle_positions_from_bytes(
    pb_bytes: bytes,
    routes: dict[str, dict[str, Any]],
    stops: dict[str, dict[str, Any]],
    ingest_time: datetime,
) -> list[dict[str, Any]]:
    """Parse raw VehiclePosition protobuf bytes into a flat list of dicts.

    Each dict maps directly to the columns of the *vehicle_positions* table.
    Records without a route_id are discarded.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(pb_bytes)

    positions: list[dict[str, Any]] = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue

        vp = entity.vehicle
        trip = vp.trip if _safe_has_field(vp, "trip") else None
        route_id = trip.route_id if trip else None
        if not route_id:
            continue

        has_pos = _safe_has_field(vp, "position")
        has_vehicle = _safe_has_field(vp, "vehicle")

        # Always use ingest_time so the DB timestamp reflects when we polled,
        # not the vehicle's GPS clock (which can lag minutes behind real time).
        ts = ingest_time

        positions.append(
            {
                "trip_id": trip.trip_id if trip else None,
                "route_id": route_id,
                "vehicle_id": vp.vehicle.id if has_vehicle else None,
                "vehicle_label": vp.vehicle.label if has_vehicle else None,
                "latitude": vp.position.latitude if has_pos else None,
                "longitude": vp.position.longitude if has_pos else None,
                "bearing": (
                    vp.position.bearing
                    if has_pos and _safe_has_field(vp.position, "bearing")
                    else None
                ),
                "current_stop_sequence": (
                    vp.current_stop_sequence
                    if _safe_has_field(vp, "current_stop_sequence")
                    else None
                ),
                "current_status": (
                    int(vp.current_status)
                    if _safe_has_field(vp, "current_status")
                    else None
                ),
                "stop_id": (
                    vp.stop_id if _safe_has_field(vp, "stop_id") else None
                ),
                "occupancy_status": _extract_occupancy(vp),
                "timestamp": ts,
            }
        )

    return positions


def extract_trip_updates_from_bytes(
    pb_bytes: bytes,
    ingest_time: datetime,
) -> list[dict[str, Any]]:
    """Parse raw TripUpdate protobuf bytes into a flat list of stop-time dicts."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(pb_bytes)

    updates: list[dict[str, Any]] = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        tu = entity.trip_update
        trip = tu.trip
        route_id = trip.route_id if trip else None
        if not route_id:
            continue

        raw_ts = tu.timestamp if tu.timestamp else None
        ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc) if raw_ts else ingest_time

        for stu in tu.stop_time_update:
            has_arr = stu.HasField("arrival")
            has_dep = stu.HasField("departure")
            updates.append(
                {
                    "trip_id": trip.trip_id if trip else None,
                    "route_id": route_id,
                    "stop_id": stu.stop_id or None,
                    "stop_sequence": stu.stop_sequence or None,
                    "arrival_delay": stu.arrival.delay if has_arr else None,
                    "departure_delay": stu.departure.delay if has_dep else None,
                    "arrival_time": (
                        stu.arrival.time if has_arr and stu.arrival.time else None
                    ),
                    "departure_time": (
                        stu.departure.time if has_dep and stu.departure.time else None
                    ),
                    "timestamp": ts,
                }
            )

    return updates


def write_grouped_outputs(output_by_type: dict[str, dict[str, Any]], output_root: Path) -> dict[str, int]:
    output_root.mkdir(parents=True, exist_ok=True)
    file_mappings = {
        "bus": "output_buses.json",
        "light_rail": "output_lr.json",
        "heavy_rail": "output_hr.json",
        "commuter_rail": "output_cr.json",
        "other": "output_other.json",
    }
    counts: dict[str, int] = {}

    for type_name, filename in file_mappings.items():
        payload = output_by_type.get(type_name, {})
        if not payload:
            continue
        with (output_root / filename).open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        counts[type_name] = len(payload)

    return counts
