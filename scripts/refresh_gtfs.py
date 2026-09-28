#!/usr/bin/env python3
"""Refresh the vendored RTD GTFS static feeds, installing each pick the day it
takes effect.

RTD publishes ONE service pick at a time, and puts it up days before it starts:

    Jun26 pick   feed_version stamped 2026-05-21   effective 20260607-20260926
    Sep26 pick   feed_version stamped 2026-09-22   effective 20260927-20270102

Consecutive picks share no trip_ids whatsoever -- not one of the ~10.4k Jun26
bus trips carried over into Sep26 -- and the RT feed keeps emitting the
*current* pick's ids right up to the changeover.  So a plain download-and-
overwrite job is not merely unhelpful here, it is worse than doing nothing:
installing Sep26 on the day RTD published it would have blinded on-time
detection for the five days leading up to the 27th, where leaving the feed
stale cost twenty hours.

Hence two stages.  A download that is not effective yet is parked in
``gtfs-static/incoming/<folder>/`` and promoted into place only once
``feed_start_date`` has arrived.  Both stages are committed, so the pending
pick is visible in the repo and survives between runs of a stateless CI job.

Promotion additionally waits for the service day to roll (~3am Denver).  A
service day runs past midnight, so a feed swapped in at 00:05 on its start date
breaks the tail of the *outgoing* day -- which is exactly the trips that went
missing on 2026-09-27, the last of them an A-Line run that ended at 02:33.

Usage:
    ./scripts/refresh_gtfs.py              # fetch, then stage or promote
    ./scripts/refresh_gtfs.py --promote-only   # no network; install anything due
    ./scripts/refresh_gtfs.py --force      # install what RTD serves now, regardless

After a promotion the backend must be restarted to drop its in-process CSV
cache; scripts/staging-update.sh does that on its own when gtfs-static/ moves.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

DENVER = ZoneInfo("America/Denver")

# RTD publishes a separate zip per service type; gtfs-static/ mirrors that split.
FEEDS: dict[str, str] = {
    "light_rail": "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Operated_Light_Rail_GTFS.zip",
    "op_commuter_rail": "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Operated_Commuter_Rail_GTFS.zip",
    "op_motorbus": "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Operated_Motorbus_GTFS.zip",
    "pur_commuter_rail": "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Purchased_Transportation_Commuter_Rail_GTFS.zip",
    "pur_motorbus": "https://www.rtd-denver.com/files/gtfs/RTD_Denver_Direct_Purchased_Transportation_Motorbus_GTFS.zip",
}

# Hour (Denver) after which a pick starting today may be installed.  RTD's
# service day rolls somewhere around 3am; before that, the outgoing pick is
# still the one describing the vehicles on the street.
SERVICE_DAY_ROLL_HOUR = 3

USER_AGENT = "TransitDen-GTFS-Refresh/1.0 (+https://github.com/ImMattic/rtdstats)"

REPO_ROOT = Path(__file__).resolve().parents[1]
GTFS_ROOT = REPO_ROOT / "gtfs-static"
INCOMING_ROOT = GTFS_ROOT / "incoming"


class FeedInfo:
    """The three fields of feed_info.txt this script decides on."""

    def __init__(self, version: str, start: date | None, end: date | None) -> None:
        self.version = version
        self.start = start
        self.end = end

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FeedInfo({self.version!r}, {self.start}, {self.end})"


def _parse_date(raw: str | None) -> date | None:
    """GTFS ``YYYYMMDD`` -> date, or None if absent/malformed."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def read_feed_info(folder: Path) -> FeedInfo | None:
    """Parse ``feed_info.txt`` out of an extracted feed folder."""
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
    return FeedInfo(
        version=(row.get("feed_version") or "").strip(),
        start=_parse_date(row.get("feed_start_date")),
        end=_parse_date(row.get("feed_end_date")),
    )


def is_due(info: FeedInfo, now: datetime) -> bool:
    """Has this pick's service period actually begun?

    True once the start date's service day has rolled.  A feed with no
    ``feed_start_date`` is treated as due: we can't date-gate what RTD didn't
    date, and holding it back forever would be the worse failure.
    """
    if info.start is None:
        return True
    roll = datetime.combine(info.start, time(SERVICE_DAY_ROLL_HOUR), tzinfo=DENVER)
    return now >= roll


def download_feed(url: str, dest: Path) -> None:
    """Fetch one zip and extract its .txt members flat into ``dest``.

    RTD's zips are sometimes flat and sometimes nested a directory deep, so
    members are matched by suffix and written by basename rather than by path.
    """
    # Explicit User-Agent is load-bearing, not politeness: rtd-denver.com answers
    # 403 to urllib's default "Python-urllib/3.x" while serving any descriptive
    # agent normally.  (The url also 308-redirects, which urllib has followed
    # since 3.11.)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed https urls
        payload = response.read()

    dest.mkdir(parents=True, exist_ok=True)
    for stale in dest.glob("*.txt"):
        stale.unlink()

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [m for m in archive.namelist() if m.lower().endswith(".txt")]
        if not members:
            raise RuntimeError(f"no .txt members in the zip from {url}")
        for member in members:
            target = dest / Path(member).name
            with archive.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def install(src: Path, dest: Path) -> None:
    """Replace ``dest``'s .txt files with ``src``'s."""
    dest.mkdir(parents=True, exist_ok=True)
    for stale in dest.glob("*.txt"):
        stale.unlink()
    for txt in sorted(src.glob("*.txt")):
        shutil.copy2(txt, dest / txt.name)


def _describe(info: FeedInfo | None) -> str:
    if info is None:
        return "none"
    span = f"{info.start or '?'}..{info.end or '?'}"
    return f"{info.version or '(unversioned)'} [{span}]"


class Result:
    """What one run did, for the summary and the workflow's outputs."""

    def __init__(self) -> None:
        self.promoted: list[str] = []
        self.staged: list[str] = []
        self.unchanged: list[str] = []
        self.failed: list[str] = []
        # A promotion that lands after its start date means service already ran
        # against a schedule we didn't hold -- those arrivals are recoverable
        # from stored positions, so the commit asks for a replay.
        self.late = False
        self.lines: list[str] = []

    @property
    def changed(self) -> bool:
        return bool(self.promoted or self.staged)


def refresh(
    *,
    now: datetime,
    fetch: bool = True,
    force: bool = False,
    feeds: dict[str, str] | None = None,
    gtfs_root: Path | None = None,
) -> Result:
    """Bring ``gtfs-static/`` up to date, one folder at a time."""
    feeds = feeds if feeds is not None else FEEDS
    root = gtfs_root or GTFS_ROOT
    incoming_root = root / "incoming"
    result = Result()

    for folder in sorted(feeds):
        live_dir = root / folder
        staged_dir = incoming_root / folder
        live = read_feed_info(live_dir)

        # ── Stage 1: install anything already staged and now due ───────────
        # Deliberately before the download and independent of it, so a pick
        # parked yesterday still goes in on time if RTD (or the network) is
        # having a bad morning.
        staged = read_feed_info(staged_dir)
        if staged and (force or is_due(staged, now)):
            install(staged_dir, live_dir)
            shutil.rmtree(staged_dir, ignore_errors=True)
            result.promoted.append(folder)
            if staged.start is not None and staged.start < now.date():
                result.late = True
            result.lines.append(f"{folder}: promoted staged {_describe(staged)}")
            live, staged = staged, None

        if not fetch:
            if folder not in result.promoted:
                result.unchanged.append(folder)
            continue

        # ── Stage 2: ask RTD what it is serving now ────────────────────────
        with tempfile.TemporaryDirectory() as tmp:
            fresh_dir = Path(tmp) / folder
            try:
                download_feed(feeds[folder], fresh_dir)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                result.failed.append(folder)
                result.lines.append(f"{folder}: DOWNLOAD FAILED ({exc})")
                continue

            fresh = read_feed_info(fresh_dir)
            if fresh is None:
                result.failed.append(folder)
                result.lines.append(f"{folder}: downloaded feed has no feed_info.txt")
                continue

            if live is not None and fresh.version == live.version:
                # Already installed.  Clear any stale staging area: it can only
                # hold a pick RTD has since withdrawn.
                if staged_dir.exists():
                    shutil.rmtree(staged_dir, ignore_errors=True)
                    result.lines.append(f"{folder}: dropped stale staged feed")
                    result.staged.append(folder)  # a tree change to commit
                if folder not in result.promoted:
                    result.unchanged.append(folder)
                    result.lines.append(f"{folder}: current {_describe(live)}")
                continue

            if force or is_due(fresh, now):
                install(fresh_dir, live_dir)
                shutil.rmtree(staged_dir, ignore_errors=True)
                result.promoted.append(folder)
                if fresh.start is not None and fresh.start < now.date():
                    result.late = True
                result.lines.append(
                    f"{folder}: installed {_describe(fresh)} (was {_describe(live)})"
                )
            elif staged is not None and staged.version == fresh.version:
                result.unchanged.append(folder)
                result.lines.append(
                    f"{folder}: {_describe(fresh)} already staged, effective {fresh.start}"
                )
            else:
                install(fresh_dir, staged_dir)
                result.staged.append(folder)
                result.lines.append(
                    f"{folder}: staged {_describe(fresh)}, effective {fresh.start}"
                )

    return result


def expiry_summary(gtfs_root: Path | None = None) -> tuple[date | None, list[str]]:
    """Earliest ``feed_end_date`` across the live feeds, plus a per-folder note."""
    root = gtfs_root or GTFS_ROOT
    earliest: date | None = None
    lines: list[str] = []
    for folder in sorted(FEEDS):
        info = read_feed_info(root / folder)
        lines.append(f"  {folder}: {_describe(info)}")
        if info and info.end and (earliest is None or info.end < earliest):
            earliest = info.end
    return earliest, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--promote-only",
        action="store_true",
        help="install anything already staged and due; make no network calls",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="install whatever RTD serves now, ignoring feed_start_date",
    )
    args = parser.parse_args(argv)

    now = datetime.now(tz=DENVER)
    result = refresh(now=now, fetch=not args.promote_only, force=args.force)

    print(f"RTD GTFS refresh @ {now:%Y-%m-%d %H:%M %Z}")
    for line in result.lines:
        print(f"  {line}")

    earliest, lines = expiry_summary()
    print("\nInstalled feeds:")
    for line in lines:
        print(line)
    if earliest:
        days = (earliest - now.date()).days
        print(f"\nEarliest feed_end_date: {earliest} ({days} days out)")

    # Consumed by .github/workflows/gtfs-refresh.yml.
    if out_path := os.environ.get("GITHUB_OUTPUT"):
        summary = "; ".join(result.lines) or "no change"
        with open(out_path, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if result.changed else 'false'}\n")
            handle.write(f"promoted={'true' if result.promoted else 'false'}\n")
            handle.write(f"backfill={'true' if result.late else 'false'}\n")
            handle.write(f"summary={summary}\n")

    if result.failed:
        print(f"\nFAILED: {', '.join(result.failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
