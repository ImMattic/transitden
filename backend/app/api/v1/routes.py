"""GTFS static route / stop info endpoints."""
from __future__ import annotations

import csv
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.schemas.vehicle_position import RouteInfo
from app.services.gtfs_decoder import load_gtfs_static_data, TRANSIT_FOLDERS

router = APIRouter(prefix="/routes", tags=["routes"])

_VEHICLE_TYPE_NAMES = {
    "0": "light_rail",
    "1": "heavy_rail",
    "2": "commuter_rail",
    "3": "bus",
}


def _all_routes() -> list[RouteInfo]:
    routes, _ = load_gtfs_static_data(gtfs_static_root=_gtfs_root())
    return [
        RouteInfo(
            route_id=r["route_id"],
            short_name=r.get("route_short_name", ""),
            long_name=r.get("route_long_name", ""),
            route_type=r.get("route_type", ""),
            type_name=_VEHICLE_TYPE_NAMES.get(r.get("route_type", ""), "other"),
            color=r.get("route_color", "888888"),
            agency_id=r.get("agency_id", ""),
        )
        for r in routes.values()
    ]


@router.get("", response_model=dict)
async def list_routes() -> dict:
    return {"routes": [r.model_dump() for r in _all_routes()]}


# ── Rail shapes — must be declared before /{route_id} so FastAPI doesn't
#    interpret the literal string "shapes" as a route_id path parameter. ──────

_RAIL_TYPES = {"0", "1", "2"}


def _gtfs_root() -> Path:
    """Resolve gtfs-static directory for both local dev and Docker container."""
    current = Path(__file__).resolve()
    # Docker mounts it at /app/gtfs-static; locally it sits 4 levels up
    candidates = [Path("/app/gtfs-static"), current.parents[4] / "gtfs-static"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Walk upward as a last resort
    for parent in current.parents:
        candidate = parent / "gtfs-static"
        if candidate.exists():
            return candidate
    return Path("/app/gtfs-static")


# Polyline simplification tolerance in degrees (~5–6 m of latitude). Transit
# shapes carry thousands of densely-spaced points; decimating them shrinks the
# payload and the frontend's nearest-point scan with no visible change.
_SIMPLIFY_EPSILON = 0.00005


def _simplify(points: list[list[float]], epsilon: float = _SIMPLIFY_EPSILON) -> list[list[float]]:
    """Ramer–Douglas–Peucker line simplification (iterative, stack-based).

    Treats lat/lon as planar — accurate enough at this tolerance for local
    transit geometry, and avoids recursion limits on multi-thousand-point shapes.
    """
    n = len(points)
    if n < 3:
        return points

    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack: list[tuple[int, int]] = [(0, n - 1)]
    eps_sq = epsilon * epsilon

    while stack:
        start, end = stack.pop()
        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy

        max_dist_sq = -1.0
        index = -1
        for i in range(start + 1, end):
            px, py = points[i]
            if seg_sq == 0:
                ddx, ddy = px - ax, py - ay
                dist_sq = ddx * ddx + ddy * ddy
            else:
                # squared perpendicular distance of point i to segment a–b
                cross = (px - ax) * dy - (py - ay) * dx
                dist_sq = (cross * cross) / seg_sq
            if dist_sq > max_dist_sq:
                max_dist_sq = dist_sq
                index = i

        if index != -1 and max_dist_sq > eps_sq:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))

    return [points[i] for i in range(n) if keep[i]]


@lru_cache(maxsize=1)
def _load_route_shapes_index() -> dict[str, dict[str, Any]]:
    """Read GTFS static files once and return all route shapes keyed by route_id.

    Each entry::

        {
            "route_id": str,
            "short_name": str,
            "route_type": str,     # GTFS route_type ("0"/"1"/"2"/"3"/…)
            "color": str,          # e.g. "#008348"
            "shapes": [[[lat, lon], ...], ...],  # one (simplified) entry per shape_id
        }

    Parsing ~35 MB of CSV is expensive; the result is cached for the process
    lifetime. Call :func:`warm_shape_cache` at startup (off the event loop) so no
    request ever pays the cold parse.
    """
    root = _gtfs_root()

    # 1. Collect route metadata
    routes_meta: dict[str, dict[str, Any]] = {}
    for folder in TRANSIT_FOLDERS:
        path = root / folder / "routes.txt"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                rid = row.get("route_id", "").strip()
                if not rid:
                    continue
                color = row.get("route_color", "888888").strip() or "888888"
                routes_meta[rid] = {
                    "route_id": rid,
                    "short_name": row.get("route_short_name", "").strip(),
                    "route_type": row.get("route_type", "").strip(),
                    "color": f"#{color}",
                }

    # 2. Map route_id -> shape_ids and shape_id -> folder
    route_shape_ids: dict[str, set[str]] = defaultdict(set)
    shape_id_folder: dict[str, str] = {}
    for folder in TRANSIT_FOLDERS:
        path = root / folder / "trips.txt"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                rid = row.get("route_id", "").strip()
                sid = row.get("shape_id", "").strip()
                if rid in routes_meta and sid:
                    route_shape_ids[rid].add(sid)
                    shape_id_folder[sid] = folder

    # 3. Load coordinates for each required shape_id, simplified
    shape_coords: dict[str, list[list[float]]] = {}
    for folder in TRANSIT_FOLDERS:
        path = root / folder / "shapes.txt"
        if not path.exists():
            continue
        raw: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                sid = row.get("shape_id", "").strip()
                if sid not in shape_id_folder or shape_id_folder[sid] != folder:
                    continue
                try:
                    seq = int(row.get("shape_pt_sequence", 0))
                    lat = float(row.get("shape_pt_lat", 0))
                    lon = float(row.get("shape_pt_lon", 0))
                    raw[sid].append((seq, lat, lon))
                except (ValueError, TypeError):
                    pass
        for sid, pts in raw.items():
            pts.sort(key=lambda p: p[0])
            shape_coords[sid] = _simplify([[p[1], p[2]] for p in pts])

    # 4. Assemble result
    result: dict[str, dict[str, Any]] = {}
    for rid, meta in routes_meta.items():
        shapes = [
            shape_coords[sid]
            for sid in route_shape_ids.get(rid, set())
            if sid in shape_coords
        ]
        if shapes:
            result[rid] = {**meta, "shapes": shapes}

    return result


def _load_rail_shapes() -> list[dict[str, Any]]:
    """Rail-only route shapes (route_type 0/1/2), derived from the shared index."""
    return [
        {
            "route_id": meta["route_id"],
            "short_name": meta["short_name"],
            "color": meta["color"],
            "shapes": meta["shapes"],
        }
        for meta in _load_route_shapes_index().values()
        if meta["route_type"] in _RAIL_TYPES
    ]


def warm_shape_cache() -> None:
    """Populate the shape index cache (call at startup, off the event loop)."""
    _load_route_shapes_index()


@router.get("/shapes", response_model=dict)
async def get_rail_shapes() -> dict:
    """Return GeoJSON-like shapes for all RTD rail routes (route_type 0/1/2)."""
    return {"shapes": _load_rail_shapes()}


@router.get("/shape/{route_id}", response_model=dict)
async def get_route_shape(route_id: str) -> dict:
    """Return shape geometry for one route (used by selected vehicle overlay)."""
    shape = _load_route_shapes_index().get(route_id)
    if not shape:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Route shape {route_id!r} not found")
    return shape


@lru_cache(maxsize=1)
def _load_route_stops_index() -> dict[str, list[dict[str, Any]]]:
    """Return {route_id: [{stop_id, stop_name, stop_lat, stop_lon}, ...]} ordered by stop_sequence.

    Unique stops per route, deduped across all trips. Cached for process lifetime.
    """
    root = _gtfs_root()

    # All stops: stop_id → {stop_id, stop_name, stop_lat, stop_lon}
    all_stops: dict[str, dict[str, Any]] = {}
    for folder in TRANSIT_FOLDERS:
        path = root / folder / "stops.txt"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                sid = row.get("stop_id", "").strip()
                if not sid:
                    continue
                try:
                    lat = float(row.get("stop_lat") or 0.0)
                    lon = float(row.get("stop_lon") or 0.0)
                except (ValueError, TypeError):
                    lat, lon = 0.0, 0.0
                all_stops[sid] = {
                    "stop_id": sid,
                    "stop_name": row.get("stop_name", "").strip(),
                    "stop_lat": lat,
                    "stop_lon": lon,
                }

    # trip_id → route_id
    trip_route: dict[str, str] = {}
    for folder in TRANSIT_FOLDERS:
        path = root / folder / "trips.txt"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                tid = row.get("trip_id", "").strip()
                rid = row.get("route_id", "").strip()
                if tid and rid:
                    trip_route[tid] = rid

    # route_id → {stop_id: min_sequence} — keeps only the lowest sequence
    # number seen for each unique stop across all trips on that route.
    route_stop_min_seq: dict[str, dict[str, int]] = defaultdict(dict)
    for folder in TRANSIT_FOLDERS:
        path = root / folder / "stop_times.txt"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                tid = row.get("trip_id", "").strip()
                sid = row.get("stop_id", "").strip()
                seq_str = row.get("stop_sequence", "").strip()
                if not tid or not sid or not seq_str:
                    continue
                rid = trip_route.get(tid)
                if not rid:
                    continue
                try:
                    seq = int(seq_str)
                except ValueError:
                    continue
                stop_seqs = route_stop_min_seq[rid]
                if sid not in stop_seqs or seq < stop_seqs[sid]:
                    stop_seqs[sid] = seq

    result: dict[str, list[dict[str, Any]]] = {}
    for rid, stop_seqs in route_stop_min_seq.items():
        ordered = sorted(stop_seqs.items(), key=lambda x: x[1])
        stops = [
            all_stops[sid]
            for sid, _ in ordered
            if sid in all_stops and (all_stops[sid]["stop_lat"] != 0.0 or all_stops[sid]["stop_lon"] != 0.0)
        ]
        if stops:
            result[rid] = stops
    return result


@router.get("/stops/{route_id}", response_model=dict)
async def get_route_stops(route_id: str) -> dict:
    """Return ordered stops for one route (used for station markers on the map)."""
    index = _load_route_stops_index()
    stops = index.get(route_id)
    if stops is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Route {route_id!r} not found")
    return {"route_id": route_id, "stops": stops}


@router.get("/{route_id}", response_model=dict)
async def get_route(route_id: str) -> dict:
    routes, _ = load_gtfs_static_data()
    r = routes.get(route_id)
    if not r:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Route {route_id!r} not found")
    return RouteInfo(
        route_id=r["route_id"],
        short_name=r.get("route_short_name", ""),
        long_name=r.get("route_long_name", ""),
        route_type=r.get("route_type", ""),
        type_name=_VEHICLE_TYPE_NAMES.get(r.get("route_type", ""), "other"),
        color=r.get("route_color", "888888"),
        agency_id=r.get("agency_id", ""),
    ).model_dump()
