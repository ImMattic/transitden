"""Is the vendored static schedule still describing the service RTD is running?

Two things make it stop, and both fail silently.  The feed's service period
simply ends -- RTD rolls a new pick roughly quarterly, and consecutive picks
share no trip_ids at all -- or a refresh breaks in a way that leaves the old
files sitting in place.  Either way ingestion keeps succeeding, the live map
keeps looking perfectly healthy, and only the schedule-derived half of the site
goes dark: on-time rates, the stop timeline, and the whole Trip Explorer, which
lists a trip only once it has more than one arrival event behind it.

So the signal worth watching is not "did the download run" but *do the trip_ids
coming off the realtime feed still resolve against the schedule we hold*.  That
one number catches every cause at once -- an expired pick, a renamed RTD url, a
truncated zip, a pick that landed late, a schema change -- and it is nearly free
to compute, because the lookup it measures is one ingestion already does.  It
sat near 100% until 2026-09-27 02:33, then went to 0 and stayed there.

Nothing here may ever break ingestion: every entry point swallows its own
exceptions, in the same spirit as ``services/sports.py``.  A broken health check
that took the poller down with it would be a worse bug than the one it watches
for.
"""
from __future__ import annotations

import csv
import logging
import time
from collections import deque
from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from app.config import get_settings
from app.services.gtfs_decoder import TRANSIT_FOLDERS, resolve_gtfs_static_root

logger = logging.getLogger(__name__)

_settings = get_settings()

# Polls kept in the rolling match-rate window.  At the default 30 s cadence this
# is ~10 minutes: long enough that one weird poll can't trip an alert, short
# enough that a real changeover is caught within a few minutes of the rollover.
_WINDOW_POLLS = 20

# (observed, matched) per poll, newest last.
_window: deque[tuple[int, int]] = deque(maxlen=_WINDOW_POLLS)

# Monotonic time each alert key was last sent, for the cooldown.
_last_alert: dict[str, float] = {}

# True while we are in a mismatch state, so recovery can be announced once.
_mismatch_active = False

_ALERT_MISMATCH = "schedule_mismatch"
_ALERT_EXPIRY = "feed_expiry"


# ── Feed periods ──────────────────────────────────────────────────────────
# Read once per process.  The feeds are bind-mounted and the deploy scripts
# force-recreate the backend whenever gtfs-static/ changes, so a process never
# outlives the files it parsed.
_periods_cache: list[dict[str, Any]] | None = None


def _parse_gtfs_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def _read_feed_info(folder: Path) -> dict[str, Any] | None:
    path = folder / "feed_info.txt"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle), None)
    except OSError:
        return None
    if not row:
        return None
    return {
        "version": (row.get("feed_version") or "").strip() or None,
        "start": _parse_gtfs_date(row.get("feed_start_date")),
        "end": _parse_gtfs_date(row.get("feed_end_date")),
    }


def feed_periods(gtfs_static_root: Path | None = None) -> list[dict[str, Any]]:
    """One entry per installed sub-feed: version and service period."""
    global _periods_cache
    if _periods_cache is not None and gtfs_static_root is None:
        return _periods_cache

    root = gtfs_static_root or resolve_gtfs_static_root()
    periods: list[dict[str, Any]] = []
    for folder in TRANSIT_FOLDERS:
        info = _read_feed_info(root / folder)
        periods.append(
            {
                "feed": folder,
                "version": info["version"] if info else None,
                "start_date": info["start"].isoformat() if info and info["start"] else None,
                "end_date": info["end"].isoformat() if info and info["end"] else None,
            }
        )
    if gtfs_static_root is None:
        _periods_cache = periods
    return periods


def _staged_pick(gtfs_static_root: Path | None = None) -> dict[str, Any] | None:
    """The next pick, if scripts/refresh_gtfs.py has one parked but not yet due."""
    root = gtfs_static_root or resolve_gtfs_static_root()
    for folder in TRANSIT_FOLDERS:
        info = _read_feed_info(root / "incoming" / folder)
        if info:
            return {
                "version": info["version"],
                "effective": info["start"].isoformat() if info["start"] else None,
            }
    return None


def days_until_expiry(today: date | None = None, **kw: Any) -> int | None:
    """Days until the *earliest* installed feed stops covering service.

    The earliest one governs: once any sub-feed lapses, that mode's trips stop
    resolving even while the others are fine.
    """
    today = today or datetime.now(tz=timezone.utc).date()
    ends = [
        date.fromisoformat(p["end_date"]) for p in feed_periods(**kw) if p["end_date"]
    ]
    if not ends:
        return None
    return (min(ends) - today).days


# ── Match rate ────────────────────────────────────────────────────────────


def observe_trip_ids(trip_ids: Iterable[str | None]) -> None:
    """Record how many of this poll's realtime trip_ids exist in the schedule."""
    try:
        from app.services.gtfs_schedule import load_trip_stop_schedule

        schedule = load_trip_stop_schedule()
        observed = 0
        matched = 0
        for trip_id in trip_ids:
            if not trip_id:
                continue
            observed += 1
            if trip_id in schedule:
                matched += 1
        if observed:
            _window.append((observed, matched))
    except Exception:  # noqa: BLE001 - never break ingestion over a gauge
        logger.exception("Failed to record GTFS schedule match rate")


def match_rate() -> tuple[float | None, int]:
    """(fraction matched, trip_ids observed) over the rolling window."""
    observed = sum(o for o, _ in _window)
    matched = sum(m for _, m in _window)
    if observed == 0:
        return None, 0
    return matched / observed, observed


def reset_state() -> None:
    """Drop all in-process state.  For tests."""
    global _mismatch_active, _periods_cache
    _window.clear()
    _last_alert.clear()
    _mismatch_active = False
    _periods_cache = None


# ── Status + alerting ─────────────────────────────────────────────────────


def snapshot(**kw: Any) -> dict[str, Any]:
    """Everything /meta/feed publishes, and what the alert check reads."""
    rate, observed = match_rate()
    remaining = days_until_expiry(**kw)
    threshold = _settings.gtfs_match_rate_alert_threshold
    enough = observed >= _settings.gtfs_match_rate_min_samples

    if rate is None or not enough:
        status = "unknown"
    elif rate < threshold:
        status = "mismatch"
    elif remaining is not None and remaining < 0:
        status = "expired"
    elif remaining is not None and remaining <= _settings.gtfs_feed_expiry_warn_days:
        status = "expiring"
    else:
        status = "ok"

    return {
        "status": status,
        "match_rate": round(rate, 4) if rate is not None else None,
        "trip_ids_observed": observed,
        "match_rate_threshold": threshold,
        "days_until_expiry": remaining,
        "expiry_warn_days": _settings.gtfs_feed_expiry_warn_days,
        "feeds": feed_periods(**kw),
        "staged_next_pick": _staged_pick(**kw),
        "alerts_configured": bool(_settings.ntfy_topic_url),
    }


def _cooled_down(key: str) -> bool:
    """True if ``key`` hasn't been sent inside the cooldown."""
    last = _last_alert.get(key)
    if last is None:
        return True
    return (time.monotonic() - last) >= _settings.gtfs_alert_cooldown_hours * 3600


async def _notify(title: str, body: str, *, priority: str, tags: str) -> None:
    url = _settings.ntfy_topic_url
    if not url:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(
                url,
                data=body.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": tags},
            )
    except Exception:  # noqa: BLE001 - an unreachable notifier is not an outage
        logger.exception("Failed to send ntfy alert %r", title)


async def check_and_alert(**kw: Any) -> dict[str, Any]:
    """Evaluate feed health and push a notification when it needs attention.

    Called once per ingest cycle.  Returns the snapshot so callers can log it.
    """
    global _mismatch_active
    try:
        status = snapshot(**kw)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to evaluate GTFS feed health")
        return {}

    try:
        if status["status"] == "mismatch":
            if _cooled_down(_ALERT_MISMATCH):
                _last_alert[_ALERT_MISMATCH] = time.monotonic()
                rate = status["match_rate"]
                await _notify(
                    "TransitDen: static schedule out of sync",
                    (
                        f"Only {rate:.1%} of realtime trip_ids resolve against the "
                        f"installed GTFS schedule ({status['trip_ids_observed']} observed).\n"
                        f"Earliest feed_end_date is {status['days_until_expiry']} days out.\n"
                        "On-time stats and the Trip Explorer are dark until the feed is refreshed."
                    ),
                    priority="high",
                    tags="rotating_light",
                )
            _mismatch_active = True
        else:
            if _mismatch_active and status["status"] in {"ok", "expiring"}:
                _mismatch_active = False
                _last_alert.pop(_ALERT_MISMATCH, None)
                await _notify(
                    "TransitDen: static schedule back in sync",
                    f"trip_id match rate recovered to {status['match_rate']:.1%}.",
                    priority="default",
                    tags="white_check_mark",
                )

            if status["status"] in {"expiring", "expired"} and _cooled_down(_ALERT_EXPIRY):
                _last_alert[_ALERT_EXPIRY] = time.monotonic()
                days = status["days_until_expiry"]
                when = f"in {days} days" if days is not None and days >= 0 else "already"
                await _notify(
                    "TransitDen: GTFS feed expiring",
                    (
                        f"The installed RTD schedule lapses {when}.\n"
                        "scripts/refresh_gtfs.py should have staged the next pick — "
                        "check the Refresh RTD GTFS static feeds workflow."
                    ),
                    priority="default",
                    tags="warning",
                )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to dispatch GTFS feed health alert")

    return status
