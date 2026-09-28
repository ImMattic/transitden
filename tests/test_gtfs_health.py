"""The trip_id match-rate gauge and its alerting (services/gtfs_health.py).

This is the detector for the 2026-09-27 class of failure: the static schedule
stops describing the service RTD is running, ingestion notices nothing, and the
on-time stats, the stop timeline and the Trip Explorer all go dark together.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import gtfs_health


@pytest.fixture(autouse=True)
def clean_state():
    gtfs_health.reset_state()
    yield
    gtfs_health.reset_state()


@pytest.fixture
def settings(monkeypatch):
    cfg = SimpleNamespace(
        ntfy_topic_url="https://ntfy.invalid/transitden",
        gtfs_match_rate_alert_threshold=0.5,
        gtfs_match_rate_min_samples=200,
        gtfs_feed_expiry_warn_days=10,
        gtfs_alert_cooldown_hours=12,
    )
    monkeypatch.setattr(gtfs_health, "_settings", cfg)
    return cfg


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound notifications instead of posting them."""
    out: list[tuple[str, str]] = []

    async def _notify(title, body, *, priority, tags):
        out.append((title, body))

    monkeypatch.setattr(gtfs_health, "_notify", _notify)
    return out


def write_root(tmp_path: Path, end: date, start: date | None = None) -> Path:
    """A gtfs-static tree whose feeds lapse on ``end``."""
    root = tmp_path / "gtfs-static"
    start = start or (end - timedelta(days=90))
    for folder in gtfs_health.TRANSIT_FOLDERS:
        folder_path = root / folder
        folder_path.mkdir(parents=True)
        (folder_path / "feed_info.txt").write_text(
            '"feed_start_date","feed_end_date","feed_version"\n'
            f'"{start:%Y%m%d}","{end:%Y%m%d}","TestPick"\n',
            encoding="utf-8",
        )
    return root


def today() -> date:
    return datetime.now(tz=timezone.utc).date()


def feed_schedule(monkeypatch, known: set[str]) -> None:
    """Pretend the installed schedule contains exactly ``known`` trip_ids."""
    import app.services.gtfs_schedule as sched

    monkeypatch.setattr(sched, "load_trip_stop_schedule", lambda *a, **k: {t: [] for t in known})


# ── Match rate ────────────────────────────────────────────────────────────


def test_match_rate_is_none_before_anything_is_observed():
    assert gtfs_health.match_rate() == (None, 0)


def test_match_rate_pools_across_polls(monkeypatch):
    feed_schedule(monkeypatch, {"a", "b", "c"})
    gtfs_health.observe_trip_ids(["a", "b", "zzz"])
    gtfs_health.observe_trip_ids(["c", "yyy"])

    rate, observed = gtfs_health.match_rate()
    assert observed == 5
    assert rate == pytest.approx(3 / 5)


def test_blank_trip_ids_are_not_counted(monkeypatch):
    """A position with no trip_id says nothing about the schedule."""
    feed_schedule(monkeypatch, {"a"})
    gtfs_health.observe_trip_ids(["a", None, ""])

    assert gtfs_health.match_rate() == (1.0, 1)


def test_window_is_bounded(monkeypatch):
    """A rollover must not stay masked by hours of healthy history."""
    feed_schedule(monkeypatch, {"a"})
    for _ in range(gtfs_health._WINDOW_POLLS * 2):
        gtfs_health.observe_trip_ids(["a"])
    for _ in range(gtfs_health._WINDOW_POLLS):
        gtfs_health.observe_trip_ids(["gone"])

    rate, _ = gtfs_health.match_rate()
    assert rate == 0.0


def test_observe_never_raises(monkeypatch):
    """A broken gauge must not take the poller down with it."""
    import app.services.gtfs_schedule as sched

    def boom(*a, **k):
        raise RuntimeError("stop_times.txt is a directory")

    monkeypatch.setattr(sched, "load_trip_stop_schedule", boom)
    gtfs_health.observe_trip_ids(["a", "b"])  # must not propagate

    assert gtfs_health.match_rate() == (None, 0)


# ── Status classification ─────────────────────────────────────────────────


def test_status_unknown_until_enough_samples(monkeypatch, tmp_path, settings):
    feed_schedule(monkeypatch, set())
    gtfs_health.observe_trip_ids([f"t{i}" for i in range(10)])
    root = write_root(tmp_path, end=today() + timedelta(days=60))

    assert gtfs_health.snapshot(gtfs_static_root=root)["status"] == "unknown"


def test_status_ok_when_ids_resolve(monkeypatch, tmp_path, settings):
    known = {f"t{i}" for i in range(300)}
    feed_schedule(monkeypatch, known)
    gtfs_health.observe_trip_ids(sorted(known))
    root = write_root(tmp_path, end=today() + timedelta(days=60))

    status = gtfs_health.snapshot(gtfs_static_root=root)
    assert status["status"] == "ok"
    assert status["match_rate"] == 1.0
    assert status["days_until_expiry"] == 60


def test_status_mismatch_on_a_pick_rollover(monkeypatch, tmp_path, settings):
    """The 2026-09-27 shape: the feed hasn't expired *yet*, but every live
    trip_id has already moved to the next pick."""
    feed_schedule(monkeypatch, {"old-1", "old-2"})
    gtfs_health.observe_trip_ids([f"new-{i}" for i in range(300)])
    root = write_root(tmp_path, end=today() + timedelta(days=3))

    status = gtfs_health.snapshot(gtfs_static_root=root)
    assert status["status"] == "mismatch"
    assert status["match_rate"] == 0.0


def test_status_expiring_inside_the_warning_window(monkeypatch, tmp_path, settings):
    known = {f"t{i}" for i in range(300)}
    feed_schedule(monkeypatch, known)
    gtfs_health.observe_trip_ids(sorted(known))
    root = write_root(tmp_path, end=today() + timedelta(days=5))

    assert gtfs_health.snapshot(gtfs_static_root=root)["status"] == "expiring"


def test_status_expired_when_the_period_has_passed(monkeypatch, tmp_path, settings):
    known = {f"t{i}" for i in range(300)}
    feed_schedule(monkeypatch, known)
    gtfs_health.observe_trip_ids(sorted(known))
    root = write_root(tmp_path, end=today() - timedelta(days=1))

    status = gtfs_health.snapshot(gtfs_static_root=root)
    assert status["status"] == "expired"
    assert status["days_until_expiry"] == -1


def test_earliest_feed_end_date_governs(monkeypatch, tmp_path, settings):
    """One lapsed sub-feed darkens that mode even while the others are fine."""
    root = write_root(tmp_path, end=today() + timedelta(days=60))
    (root / gtfs_health.TRANSIT_FOLDERS[0] / "feed_info.txt").write_text(
        '"feed_start_date","feed_end_date","feed_version"\n'
        f'"20260607","{(today() + timedelta(days=2)):%Y%m%d}","Short"\n',
        encoding="utf-8",
    )

    assert gtfs_health.days_until_expiry(gtfs_static_root=root) == 2


def test_staged_next_pick_is_reported(monkeypatch, tmp_path, settings):
    """So /meta/feed can say "the next pick is parked and due on the 27th"."""
    root = write_root(tmp_path, end=today() + timedelta(days=5))
    staged = root / "incoming" / gtfs_health.TRANSIT_FOLDERS[0]
    staged.mkdir(parents=True)
    (staged / "feed_info.txt").write_text(
        '"feed_start_date","feed_end_date","feed_version"\n'
        f'"{(today() + timedelta(days=6)):%Y%m%d}","20270102","Next"\n',
        encoding="utf-8",
    )

    status = gtfs_health.snapshot(gtfs_static_root=root)
    assert status["staged_next_pick"]["version"] == "Next"


# ── Alerting ──────────────────────────────────────────────────────────────


async def test_mismatch_alerts_once_then_cools_down(monkeypatch, tmp_path, settings, sent):
    feed_schedule(monkeypatch, set())
    gtfs_health.observe_trip_ids([f"new-{i}" for i in range(300)])
    root = write_root(tmp_path, end=today() + timedelta(days=30))

    await gtfs_health.check_and_alert(gtfs_static_root=root)
    await gtfs_health.check_and_alert(gtfs_static_root=root)

    assert len(sent) == 1
    assert "out of sync" in sent[0][0]


async def test_recovery_is_announced(monkeypatch, tmp_path, settings, sent):
    root = write_root(tmp_path, end=today() + timedelta(days=30))

    feed_schedule(monkeypatch, set())
    gtfs_health.observe_trip_ids([f"new-{i}" for i in range(300)])
    await gtfs_health.check_and_alert(gtfs_static_root=root)

    # Feed refreshed: the same ids now resolve.
    gtfs_health.reset_state()
    known = {f"new-{i}" for i in range(300)}
    feed_schedule(monkeypatch, known)
    gtfs_health.observe_trip_ids(sorted(known))
    gtfs_health._mismatch_active = True
    await gtfs_health.check_and_alert(gtfs_static_root=root)

    assert "back in sync" in sent[-1][0]


async def test_healthy_feed_sends_nothing(monkeypatch, tmp_path, settings, sent):
    known = {f"t{i}" for i in range(300)}
    feed_schedule(monkeypatch, known)
    gtfs_health.observe_trip_ids(sorted(known))
    root = write_root(tmp_path, end=today() + timedelta(days=60))

    await gtfs_health.check_and_alert(gtfs_static_root=root)

    assert sent == []


async def test_expiry_warns_before_anything_has_broken(monkeypatch, tmp_path, settings, sent):
    """The early warning: ids still resolve, but the period is nearly up."""
    known = {f"t{i}" for i in range(300)}
    feed_schedule(monkeypatch, known)
    gtfs_health.observe_trip_ids(sorted(known))
    root = write_root(tmp_path, end=today() + timedelta(days=4))

    await gtfs_health.check_and_alert(gtfs_static_root=root)

    assert len(sent) == 1
    assert "expiring" in sent[0][0]


async def test_alerting_off_without_a_topic_url(monkeypatch, tmp_path, settings):
    """No topic configured: status is still reported, nothing is posted."""
    settings.ntfy_topic_url = ""
    posted: list[str] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):  # pragma: no cover - must not run
            posted.append(url)

    monkeypatch.setattr(gtfs_health.aiohttp, "ClientSession", lambda **kw: FakeSession())

    feed_schedule(monkeypatch, set())
    gtfs_health.observe_trip_ids([f"new-{i}" for i in range(300)])
    root = write_root(tmp_path, end=today() + timedelta(days=30))

    status = await gtfs_health.check_and_alert(gtfs_static_root=root)

    assert status["status"] == "mismatch"
    assert status["alerts_configured"] is False
    assert posted == []


async def test_unreachable_notifier_is_not_an_outage(monkeypatch, tmp_path, settings):
    """ntfy being down must not surface as an ingest failure."""

    def boom(**kw):
        raise OSError("name resolution failed")

    monkeypatch.setattr(gtfs_health.aiohttp, "ClientSession", boom)

    feed_schedule(monkeypatch, set())
    gtfs_health.observe_trip_ids([f"new-{i}" for i in range(300)])
    root = write_root(tmp_path, end=today() + timedelta(days=30))

    status = await gtfs_health.check_and_alert(gtfs_static_root=root)

    assert status["status"] == "mismatch"
