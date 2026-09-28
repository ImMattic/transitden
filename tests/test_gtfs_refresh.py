"""Staging vs. promotion in scripts/refresh_gtfs.py.

The thing under test is a *restraint*, so these are the tests that matter most
in this change: RTD publishes the next service pick days before it takes effect
and consecutive picks share no trip_ids, so a refresher that installs eagerly
blinds on-time detection for the whole gap.  Installing Sep26 the day RTD put it
up would have cost five days where staleness cost twenty hours.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from scripts.refresh_gtfs import DENVER, FeedInfo, is_due, read_feed_info, refresh

FEEDS = {"combined": "https://example.invalid/google_transit.zip"}


def write_feed(
    folder: Path,
    version: str,
    start: date,
    end: date,
    trips: str = "A",
    *,
    stop_times: str | None = "",
) -> None:
    """Minimal but structurally real feed folder.

    ``stop_times`` defaults to matching ``trips``, which is what a sound feed
    looks like.  Pass a different trip_id to simulate the 2026-09-27 breakage
    (stop_times.txt from another pick), or None to omit the file entirely.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "feed_info.txt").write_text(
        '"feed_publisher_name","feed_publisher_url","feed_lang",'
        '"feed_start_date","feed_end_date","feed_version"\n'
        f'"RTD","http://rtd-denver.com","en","{start:%Y%m%d}","{end:%Y%m%d}","{version}"\n',
        encoding="utf-8",
    )
    (folder / "trips.txt").write_text(f'"trip_id"\n"{trips}"\n', encoding="utf-8")
    if stop_times is not None:
        scheduled = stop_times or trips
        (folder / "stop_times.txt").write_text(
            f'"trip_id","stop_id","stop_sequence"\n"{scheduled}","S1","1"\n', encoding="utf-8"
        )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "gtfs-static"


def fake_download(version: str, start: date, end: date, trips: str = "A", **kw):
    """Stand in for the network: writes the pick RTD is currently serving."""

    def _download(url: str, dest: Path) -> None:
        write_feed(dest, version, start, end, trips, **kw)

    return _download


def patch_download(monkeypatch, fn) -> None:
    monkeypatch.setattr("scripts.refresh_gtfs.download_feed", fn)


# ── The service-day gate ──────────────────────────────────────────────────


def test_pick_is_not_due_before_its_start_date():
    pick = FeedInfo("Sep26", date(2026, 9, 27), date(2027, 1, 2))
    assert not is_due(pick, datetime(2026, 9, 22, 12, 0, tzinfo=DENVER))


def test_pick_is_not_due_before_the_service_day_rolls():
    """00:05 on the start date is still the *previous* service day."""
    pick = FeedInfo("Sep26", date(2026, 9, 27), date(2027, 1, 2))
    assert not is_due(pick, datetime(2026, 9, 27, 0, 5, tzinfo=DENVER))
    assert not is_due(pick, datetime(2026, 9, 27, 2, 33, tzinfo=DENVER))
    assert is_due(pick, datetime(2026, 9, 27, 3, 20, tzinfo=DENVER))


def test_undated_pick_is_treated_as_due():
    """Better to install an undated feed than to hold it back forever."""
    assert is_due(FeedInfo("x", None, None), datetime(2026, 9, 27, tzinfo=DENVER))


# ── Staging ───────────────────────────────────────────────────────────────


def test_future_pick_is_staged_and_live_feed_left_alone(root, monkeypatch):
    """The regression this whole script exists to prevent."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26), trips="OLD")
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2), "NEW"))

    result = refresh(
        now=datetime(2026, 9, 22, 4, 20, tzinfo=DENVER),
        feeds=FEEDS,
        gtfs_root=root,
    )

    assert result.staged == ["combined"]
    assert result.promoted == []
    live = read_feed_info(root / "combined")
    assert live.version == "Jun26", "the in-service pick must not be replaced early"
    assert "OLD" in (root / "combined" / "trips.txt").read_text()
    assert read_feed_info(root / "incoming" / "combined").version == "Sep26"


def test_staged_pick_is_promoted_once_due_without_network(root, monkeypatch):
    """Promotion runs before (and independently of) the download.

    A pick parked yesterday still goes in on time even if RTD is unreachable
    this morning.
    """
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26), trips="OLD")
    write_feed(
        root / "incoming" / "combined", "Sep26", date(2026, 9, 27), date(2027, 1, 2), "NEW"
    )

    def explode(url: str, dest: Path) -> None:  # pragma: no cover - must not run
        raise AssertionError("promote-only must not touch the network")

    patch_download(monkeypatch, explode)

    result = refresh(
        now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER),
        fetch=False,
        feeds=FEEDS,
        gtfs_root=root,
    )

    assert result.promoted == ["combined"]
    assert read_feed_info(root / "combined").version == "Sep26"
    assert "NEW" in (root / "combined" / "trips.txt").read_text()
    assert not (root / "incoming" / "combined").exists()


def test_restaging_the_same_pick_is_not_a_change(root, monkeypatch):
    """A daily run over an already-staged pick must not churn the repo."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    write_feed(root / "incoming" / "combined", "Sep26", date(2026, 9, 27), date(2027, 1, 2))
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2)))

    result = refresh(
        now=datetime(2026, 9, 24, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert not result.changed
    assert result.unchanged == ["combined"]


# ── Promotion ─────────────────────────────────────────────────────────────


def test_pick_installs_on_its_start_date(root, monkeypatch):
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2), "NEW"))

    result = refresh(
        now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert result.promoted == ["combined"]
    assert result.late is False, "an on-time install opens no gap, so no backfill"
    assert read_feed_info(root / "combined").version == "Sep26"


def test_late_install_asks_for_a_backfill(root, monkeypatch):
    """Service ran against a schedule we didn't hold; those arrivals are
    recoverable from stored positions, so the commit carries [backfill]."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2)))

    result = refresh(
        now=datetime(2026, 9, 30, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert result.promoted == ["combined"]
    assert result.late is True


def test_unchanged_pick_is_a_no_op(root, monkeypatch):
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    patch_download(monkeypatch, fake_download("Jun26", date(2026, 6, 7), date(2026, 9, 26)))

    result = refresh(
        now=datetime(2026, 7, 1, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert not result.changed
    assert result.unchanged == ["combined"]


def test_force_installs_a_future_pick(root, monkeypatch):
    """The manual escape hatch, for when RTD's dates are wrong."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2)))

    result = refresh(
        now=datetime(2026, 9, 22, 4, 20, tzinfo=DENVER),
        force=True,
        feeds=FEEDS,
        gtfs_root=root,
    )

    assert result.promoted == ["combined"]
    assert read_feed_info(root / "combined").version == "Sep26"


def test_download_failure_leaves_the_live_feed_intact(root, monkeypatch):
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26), trips="OLD")

    def boom(url: str, dest: Path) -> None:
        raise OSError("connection reset")

    patch_download(monkeypatch, boom)

    result = refresh(
        now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert result.failed == ["combined"]
    assert not result.changed
    assert read_feed_info(root / "combined").version == "Jun26"
    assert "OLD" in (root / "combined" / "trips.txt").read_text()


def test_install_replaces_every_txt_rather_than_merging(root, monkeypatch):
    """Two picks' stop_times must never coexist in one folder."""
    live = root / "combined"
    write_feed(live, "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    (live / "orphan.txt").write_text("stale\n", encoding="utf-8")
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2)))

    refresh(now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root)

    assert not (live / "orphan.txt").exists()


def test_empty_gtfs_root_installs_whatever_is_published(root, monkeypatch):
    """First-run / disaster-recovery: nothing live means take what RTD has."""
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2)))

    result = refresh(
        now=datetime(2026, 10, 5, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert result.promoted == ["combined"]
    assert read_feed_info(root / "combined").version == "Sep26"


def test_the_real_2026_09_27_timeline(root, monkeypatch):
    """End to end over the incident's actual dates, one day at a time."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26), trips="OLD")
    patch_download(monkeypatch, fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2), "NEW"))

    # Sep 22-26: RTD is already serving Sep26, but it isn't in service yet.
    for day in range(22, 27):
        refresh(
            now=datetime(2026, 9, day, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
        )
        assert read_feed_info(root / "combined").version == "Jun26", (
            f"Sep {day}: Jun26 is still the pick on the street"
        )

    # Sep 27, after the service day rolls.
    result = refresh(
        now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )
    assert result.promoted == ["combined"]
    assert read_feed_info(root / "combined").version == "Sep26"
    assert result.late is False


# ── Refusing a structurally broken feed ───────────────────────────────────
# RTD really does publish these: on 2026-09-27 two of its five per-service-type
# exports were internally inconsistent while carrying entirely valid dates.


def test_feed_whose_stop_times_belong_to_another_pick_is_refused(root, monkeypatch):
    """The exact shape of RTD's broken op_motorbus export."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26), trips="OLD")
    patch_download(
        monkeypatch,
        fake_download(
            "Sep26", date(2026, 9, 27), date(2027, 1, 2), "NEW", stop_times="OLD"
        ),
    )

    result = refresh(
        now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert result.failed == ["combined"]
    assert result.promoted == []
    assert read_feed_info(root / "combined").version == "Jun26", (
        "a broken feed installed over a working one is worse than staleness"
    )


def test_feed_with_header_only_stop_times_is_refused(root, monkeypatch):
    """RTD's broken light_rail export: stop_times.txt present but empty."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    patch_download(
        monkeypatch,
        fake_download("Sep26", date(2026, 9, 27), date(2027, 1, 2), stop_times=None),
    )

    result = refresh(
        now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert result.failed == ["combined"]
    assert read_feed_info(root / "combined").version == "Jun26"


def test_broken_pick_is_not_staged_for_later_promotion(root, monkeypatch):
    """Refusing must happen before staging, or the break just lands on time."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    patch_download(
        monkeypatch,
        fake_download(
            "Sep26", date(2026, 9, 27), date(2027, 1, 2), "NEW", stop_times="OLD"
        ),
    )

    result = refresh(
        now=datetime(2026, 9, 22, 4, 20, tzinfo=DENVER), feeds=FEEDS, gtfs_root=root
    )

    assert result.failed == ["combined"]
    assert not (root / "incoming" / "combined").exists()


def test_a_staged_feed_that_went_bad_is_refused_at_promotion(root, monkeypatch):
    """Belt and braces: validate again on the way in, not only on the way out."""
    write_feed(root / "combined", "Jun26", date(2026, 6, 7), date(2026, 9, 26))
    write_feed(
        root / "incoming" / "combined",
        "Sep26",
        date(2026, 9, 27),
        date(2027, 1, 2),
        "NEW",
        stop_times="OLD",
    )

    result = refresh(
        now=datetime(2026, 9, 27, 4, 20, tzinfo=DENVER),
        fetch=False,
        feeds=FEEDS,
        gtfs_root=root,
    )

    assert result.failed == ["combined"]
    assert read_feed_info(root / "combined").version == "Jun26"


def test_validate_feed_accepts_a_sound_feed(root):
    from scripts.refresh_gtfs import validate_feed

    write_feed(root / "combined", "Sep26", date(2026, 9, 27), date(2027, 1, 2))
    assert validate_feed(root / "combined") == []
