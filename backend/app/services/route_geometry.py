"""Distance from a point to a route's drawn shape.

Used to tell a vehicle working its route from one that is merely powered on
somewhere else (the bus yard, a garage lot): the second kind can report a
perfectly good GPS fix, but that fix is nowhere near the line the route runs on.

Pure and dependency-free so it can be unit-tested without the GTFS files.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

# Metres per degree of latitude (and of longitude at the equator). Spherical is
# plenty here: the test is "within tens of metres", not survey work.
_M_PER_DEG = 111_195.0


def distance_to_shapes_m(
    lat: float,
    lon: float,
    shapes: Iterable[Sequence[Sequence[float]]],
) -> float:
    """Shortest distance in metres from ``(lat, lon)`` to any of ``shapes``.

    Each shape is a polyline of ``[lat, lon]`` points. The distance is to the
    *line between* points, not to the nearest vertex — simplified shapes have
    long straight runs, and a vehicle mid-run is right on the route while
    kilometres from its nearest vertex. Returns ``math.inf`` when there are no
    shapes, so "we know of no route line here" reads as "not on the route".
    """
    # Local equirectangular projection centred on the query point: longitude
    # degrees shrink by cos(lat), and the query point is the origin.
    kx = _M_PER_DEG * math.cos(math.radians(lat))
    ky = _M_PER_DEG
    best = math.inf

    for shape in shapes:
        if not shape:
            continue
        if len(shape) == 1:
            px, py = (shape[0][1] - lon) * kx, (shape[0][0] - lat) * ky
            best = min(best, math.hypot(px, py))
            continue

        ax, ay = (shape[0][1] - lon) * kx, (shape[0][0] - lat) * ky
        for point in shape[1:]:
            bx, by = (point[1] - lon) * kx, (point[0] - lat) * ky
            best = min(best, _origin_to_segment_m(ax, ay, bx, by))
            ax, ay = bx, by

    return best


def _origin_to_segment_m(ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from the origin to segment ``a``–``b`` (all in metres)."""
    dx, dy = bx - ax, by - ay
    seg_sq = dx * dx + dy * dy
    if seg_sq == 0:
        return math.hypot(ax, ay)
    # Projection of the origin onto the segment's line, clamped to the segment.
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_sq))
    return math.hypot(ax + t * dx, ay + t * dy)
