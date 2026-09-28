"""Filters implausible GPS jumps out of the live position stream.

RTD's GTFS-RT feed occasionally reports a vehicle somewhere it could not
plausibly have reached since its last fix — a coordinate glitch — and then,
a poll or two later, reports it right back near where it actually was. Taken
at face value this reads as a teleport: on the map the route line jumps
around, and fed into ``services/ontime.py`` unfiltered, a single bad fix is
enough for ``OriginDepartureTracker`` to record a real-looking but wrong
departure (or an arrival) before the bounce-back is ever seen.

The fix: gate each new fix against the fastest a real vehicle on this route
could plausibly have covered since its last *trusted* fix. A fix that fails
that check is not trusted immediately, nor thrown away — it is held
(``pending``) until the same vehicle's next report resolves it one of two
ways:
  * the next fix continues on from the suspect one (the same speed check
    passes against it) — the jump was real, so the held fix is released,
    untouched;
  * the next fix instead lands back near where the vehicle was *before* the
    suspect fix — the suspect was noise, so it is released flagged
    ``is_anomalous`` and the vehicle's trusted position never moved.
A fix that resolves neither way (a second, unrelated wild jump) is
conservatively flagged as noise too, so held state can't chain indefinitely.
A fix that goes unresolved for ``position_anomaly_hold_seconds`` (the
vehicle simply stopped reporting) is released untouched — holding forever
would just mean the position is never written at all.

Flagged rows are still written to ``vehicle_positions``, never deleted, so
the raw feed stays available for tuning thresholds later. Every reader (the
map, trip replay, historical/export, on-time detection, the occupancy cagg)
filters them out via ``is_anomalous``.

The public entry point is ``filter_batch``: run one poll's rows through a
``PositionAnomalyFilter`` and get back every row now safe to insert and
(when not flagged) feed to ``detect_arrivals``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Any

from app.config import get_settings
from app.services.ontime import is_rail_route

_settings = get_settings()
_EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS84 points, in metres."""
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(a))


def _key(row: dict[str, Any]) -> str | None:
    """Identity to track speed against — the physical vehicle, not the trip.

    Falls back to trip_id when the feed omits vehicle_id; a row with neither
    carries no identity to check speed against, so callers pass it through
    unfiltered rather than guessing.
    """
    return row.get("vehicle_id") or row.get("trip_id") or None


@dataclass
class _Fix:
    lat: float
    lon: float
    time: datetime


@dataclass
class _Pending:
    row: dict[str, Any]
    fix: _Fix
    first_seen: datetime


class PositionAnomalyFilter:
    """Streams positions and holds back implausible jumps for one resolution.

    Mirrors the pending/resolve shape of ``OriginDepartureTracker`` in
    services/ontime.py: state per vehicle, fed one poll's rows at a time via
    ``feed``, with ``flush_stale`` releasing anything that never got resolved.
    The same instance is reused across polls — see ``ingestion.py``.
    """

    def __init__(
        self,
        *,
        max_speed_mps: float | None = None,
        max_speed_rail_mps: float | None = None,
        hold_seconds: int | None = None,
    ) -> None:
        self._max_speed_mps = (
            _settings.position_max_speed_mps if max_speed_mps is None else max_speed_mps
        )
        self._max_speed_rail_mps = (
            _settings.position_max_speed_rail_mps
            if max_speed_rail_mps is None
            else max_speed_rail_mps
        )
        self._hold = timedelta(
            seconds=(
                _settings.position_anomaly_hold_seconds
                if hold_seconds is None
                else hold_seconds
            )
        )
        self._last_good: dict[str, _Fix] = {}
        self._pending: dict[str, _Pending] = {}

    def _max_speed(self, route_id: str | None) -> float:
        return self._max_speed_rail_mps if is_rail_route(route_id) else self._max_speed_mps

    def _plausible(self, a: _Fix, b: _Fix, route_id: str | None) -> bool:
        dt = (b.time - a.time).total_seconds()
        if dt <= 1:
            # Too close in time to judge (or a clock hiccup) — treat as fine
            # rather than false-flagging.
            return True
        speed = _haversine_m(a.lat, a.lon, b.lat, b.lon) / dt
        return speed <= self._max_speed(route_id)

    def feed(self, row: dict[str, Any], actual_time: datetime) -> list[dict[str, Any]]:
        """Absorb one position; returns 0-2 rows now resolved and safe to use.

        Every row this method returns carries ``is_anomalous`` set — never
        left for the caller to default — so downstream code never has to
        guess about a row this filter has touched.
        """
        lat, lon = row.get("latitude"), row.get("longitude")
        key = _key(row)
        if key is None or lat is None or lon is None:
            row["is_anomalous"] = False
            return [row]

        fix = _Fix(lat, lon, actual_time)
        route_id = row.get("route_id")
        out: list[dict[str, Any]] = []

        pending = self._pending.pop(key, None)
        if pending is not None:
            if self._plausible(pending.fix, fix, route_id):
                # This fix continues on from the held one — the jump was real.
                pending.row["is_anomalous"] = False
                out.append(pending.row)
                self._last_good[key] = pending.fix
            else:
                last_good = self._last_good.get(key)
                if last_good is not None and self._plausible(last_good, fix, route_id):
                    # Back near where the vehicle was *before* the jump — the
                    # held fix was noise, not a resolved position.
                    pending.row["is_anomalous"] = True
                    out.append(pending.row)
                else:
                    # Neither confirmed nor denied (a second, unrelated wild
                    # jump). Flag the held fix as noise so state can't chain
                    # indefinitely; this row gets evaluated fresh below.
                    pending.row["is_anomalous"] = True
                    out.append(pending.row)

        last_good = self._last_good.get(key)
        if last_good is None or self._plausible(last_good, fix, route_id):
            row["is_anomalous"] = False
            self._last_good[key] = fix
            out.append(row)
        else:
            self._pending[key] = _Pending(row=row, fix=fix, first_seen=actual_time)

        return out

    def flush_stale(self, now: datetime) -> list[dict[str, Any]]:
        """Release any pending fix the vehicle never reported again to resolve.

        Silence after a suspect jump can neither confirm nor deny it, and
        holding forever would mean a real position is simply never written.
        Default to trusting it — the geofence/segment/misassignment guards in
        services/ontime.py still catch an implausible schedule match.
        """
        out: list[dict[str, Any]] = []
        for key, pending in list(self._pending.items()):
            if now - pending.first_seen < self._hold:
                continue
            del self._pending[key]
            pending.row["is_anomalous"] = False
            self._last_good[key] = pending.fix
            out.append(pending.row)
        return out

    def prune(self, now: datetime, *, max_age: timedelta = timedelta(hours=2)) -> None:
        """Drop state for vehicles that have gone quiet, so memory stays bounded."""
        stale = [k for k, f in self._last_good.items() if now - f.time > max_age]
        for k in stale:
            del self._last_good[k]


def filter_batch(
    tracker: PositionAnomalyFilter,
    rows: list[dict[str, Any]],
    default_time: datetime,
) -> list[dict[str, Any]]:
    """Run one poll's rows through ``tracker`` and return what's safe to use.

    That includes this poll's newly-accepted-or-flagged fixes plus any older
    suspect fix that just got resolved (or, via ``flush_stale``, gave up
    waiting) — every returned row carries a definite ``is_anomalous``.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        out.extend(tracker.feed(row, row.get("timestamp") or default_time))
    out.extend(tracker.flush_stale(default_time))
    tracker.prune(default_time)
    return out
