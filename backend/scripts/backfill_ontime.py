#!/usr/bin/env python
"""Backfill observed on-time arrivals from already-stored vehicle positions.

The on-time rework (migration 004) derives arrivals by geofencing live
``vehicle_positions`` against the static timepoint schedule.  Going forward the
ingest loop does this, but the dashboards would start empty.  This script
replays every stored position through the same ``classify_arrival`` logic so the
new ``stop_arrival_events`` table — and the on-time continuous aggregates built
on it — have history immediately.

It is idempotent, and it never leaves the dashboards empty: the replay fills a
scratch copy (``stop_arrival_events_backfill``) while the live table keeps
serving the old history, and only a *successful* replay is swapped in — one
transaction that replaces just the range the replay covered, so arrivals the
live ingest wrote in the meantime survive.  Crash, OOM or Ctrl-C before the swap
changes nothing.

Positions are processed oldest-first so the earliest snapshot near a timepoint
wins (de-duplicated per trip/stop/service-date in memory).  Trip origins are
timed by departure rather than arrival, which needs positions in time order —
the keyset scan below already guarantees that, and one ``OriginDepartureTracker``
spans all batches so a layover straddling a batch boundary still resolves.
Finally it refreshes the on-time continuous aggregates: the replayed rows land
below the aggregates' watermark, so without that step the dashboards would still
read empty (real-time aggregation only surfaces raw rows *newer* than the
watermark).

The swap refuses to run if the replay produced no arrivals, or fewer than half
the rows already stored for the same range — pass ``--force`` when a shrink that
large is expected (e.g. a deliberately stricter detection radius).

Re-run this after changing the origin-departure logic or radius: rows written by
the previous rules are not rewritten in place.

Run inside the backend container:

    docker compose exec backend python scripts/backfill_ontime.py
    docker compose exec backend python scripts/backfill_ontime.py --batch-size 20000

Or from a local venv. The ``-m`` form requires your shell's cwd to be
``backend/`` (it's a plain package lookup, so run from anywhere else — e.g.
the repo root — and you'll get ``ModuleNotFoundError: No module named
'scripts'``):

    cd backend && python -m scripts.backfill_ontime

Running it as a script instead sidesteps that — it self-adds ``backend/`` to
sys.path, so this works from the repo root too (and picks up the root
``.env`` for DATABASE_URL, matching how the app itself resolves settings):

    python backend/scripts/backfill_ontime.py

Long replays can exceed the API's server-side query timeout; disable it for
this process with STATEMENT_TIMEOUT_MS=0.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Allow running as `python backend/scripts/backfill_ontime.py` too.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import MetaData, insert, select, text  # noqa: E402

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.stop_arrival import StopArrivalEvent  # noqa: E402
from app.models.vehicle_position import VehiclePosition  # noqa: E402
from app.services.gtfs_schedule import (  # noqa: E402
    load_trip_origin_timepoints,
    load_trip_shape_dist_schedule,
)
from app.services.ontime import (  # noqa: E402
    ActiveTrips,
    OriginDepartureTracker,
    TerminusFallbackTracker,
    classify_arrival,
    classify_segment_arrivals,
)

_VP = VehiclePosition
_LIVE = StopArrivalEvent.__table__
_SHADOW_NAME = "stop_arrival_events_backfill"
# Same columns, different table: the replay writes here so the live table stays
# readable and intact until the swap. Never created via SQLAlchemy DDL (see
# _create_shadow) — this exists so the batch inserts below can target it.
_SHADOW = _LIVE.to_metadata(MetaData(), name=_SHADOW_NAME)
# `id` is excluded from the copy so the live table's own sequence assigns fresh
# ids; deriving the rest from the model keeps this honest as columns are added.
_COPY_COLS = ", ".join(c.name for c in _LIVE.columns if c.name != "id")


async def _create_shadow() -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # A shadow left behind by an interrupted run is scrap — start clean.
            await session.execute(text(f"DROP TABLE IF EXISTS {_SHADOW_NAME};"))
            # UNLOGGED: pure scratch space, cheaper to write, and losing it to a
            # crash costs nothing but a re-run. LIKE ... INCLUDING DEFAULTS
            # carries the id sequence default; a plain table is fine here since
            # nothing queries it by time.
            await session.execute(
                text(
                    f"CREATE UNLOGGED TABLE {_SHADOW_NAME} "
                    f"(LIKE {_LIVE.name} INCLUDING DEFAULTS);"
                )
            )


async def _drop_shadow() -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(text(f"DROP TABLE IF EXISTS {_SHADOW_NAME};"))


async def _swap(*, force: bool) -> int:
    """Move the replayed arrivals into the live table in one transaction.

    Only the range the replay actually covered (``timestamp <= watermark``) is
    replaced, so arrivals the live ingest wrote during the replay are kept.
    Readers see the old history right up to the commit — there is no window
    where the table is empty.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            rebuilt, watermark = (
                await session.execute(
                    text(f"SELECT count(*), max(timestamp) FROM {_SHADOW_NAME};")
                )
            ).one()
            if not rebuilt:
                raise SystemExit(
                    "Replay produced 0 arrivals — refusing to swap. "
                    f"{_LIVE.name} is untouched. Is gtfs-static current, and "
                    "does vehicle_positions have rows?"
                )

            live = (
                await session.execute(
                    text(f"SELECT count(*) FROM {_LIVE.name} WHERE timestamp <= :hi;"),
                    {"hi": watermark},
                )
            ).scalar_one()
            # A replay that collapses the history is far more likely to be a bug
            # (stale gtfs-static, a botched radius) than a real result.
            if live and rebuilt * 2 < live and not force:
                raise SystemExit(
                    f"Replay produced {rebuilt} arrivals but {live} are already "
                    f"stored up to {watermark} — refusing to swap away more than "
                    "half the history. Re-run with --force if that shrink is "
                    "expected."
                )

            await session.execute(
                text(f"DELETE FROM {_LIVE.name} WHERE timestamp <= :hi;"),
                {"hi": watermark},
            )
            await session.execute(
                text(
                    f"INSERT INTO {_LIVE.name} ({_COPY_COLS}) "
                    f"SELECT {_COPY_COLS} FROM {_SHADOW_NAME};"
                )
            )
    return rebuilt


def _dedupe(
    candidates: list[dict],
    seen: set[tuple[str, int, date]],
    finished: set[tuple[str, date]],
    schedule: dict[str, list[tuple[int, str, int, float, float, float]]],
    terminus: TerminusFallbackTracker,
) -> list[dict]:
    """Keep the first event per (trip, stop, service date).

    Mirrors ``detect_arrivals``, so a replay lands on the same rows the live
    loop would have: a run is closed as soon as its terminus arrival is
    recorded, and later sightings under the same trip_id (a vehicle turning
    around, or looping back past stops it already served) add nothing.
    ``seen``, ``finished`` and the terminus tracker are all mutated.
    """
    out: list[dict] = []
    for event in candidates:
        trip_id = event["trip_id"]
        run = (trip_id, event["service_date"])
        if run in finished:
            continue
        key = (trip_id, event["stop_sequence"], event["service_date"])
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
        timepoints = schedule.get(trip_id)
        if timepoints and event["stop_sequence"] == timepoints[-1][0]:
            finished.add(run)
            terminus.forget(trip_id, event["service_date"])
    return out


async def _backfill(batch_size: int) -> int:
    schedule = load_trip_shape_dist_schedule()
    if not schedule:
        raise SystemExit("No timepoint schedule loaded — is gtfs-static present?")

    origins = load_trip_origin_timepoints()
    # One registry for the whole replay, so the misassignment guard can tell a
    # bus a headway late from a trip_id alias exactly as the live loop does.
    active_trips = ActiveTrips()
    tracker = OriginDepartureTracker(origins, active_trips=active_trips)
    terminus = TerminusFallbackTracker(schedule, active_trips=active_trips)

    seen: set[tuple[str, int, date]] = set()
    # Runs closed by their terminus arrival — see _dedupe.
    finished: set[tuple[str, date]] = set()
    # Each trip's previous fix, carried across batches so a stop passed
    # over a batch boundary is still interpolated.
    last_fix: dict[str, tuple[dict, datetime]] = {}
    total = 0
    # Keyset pagination on (timestamp, id) — cheap over a hypertable and stable.
    last_ts = None
    last_id = -1

    async with AsyncSessionLocal() as session:
        while True:
            stmt = (
                select(
                    _VP.trip_id, _VP.route_id, _VP.latitude, _VP.longitude,
                    _VP.bearing, _VP.current_status, _VP.current_stop_sequence,
                    _VP.timestamp, _VP.id,
                )
                .order_by(_VP.timestamp, _VP.id)
                .limit(batch_size)
            )
            if last_ts is not None:
                stmt = stmt.where(
                    (_VP.timestamp > last_ts)
                    | ((_VP.timestamp == last_ts) & (_VP.id > last_id))
                )
            rows = (await session.execute(stmt)).all()
            if not rows:
                break

            # Register the batch before classifying it, mirroring the live
            # loop's per-poll registration.
            for row in rows:
                active_trips.observe(row[0], row[7])

            candidates: list[dict] = []
            for trip_id, route_id, lat, lon, bearing, cur_status, cur_stop_seq, ts, vp_id in rows:
                last_ts, last_id = ts, vp_id
                vp_row = {
                    "trip_id": trip_id, "route_id": route_id,
                    "latitude": lat, "longitude": lon,
                    "bearing": bearing,
                    "current_status": cur_status,
                    "current_stop_sequence": cur_stop_seq,
                }
                terminus.feed(vp_row, ts)
                departure = tracker.feed(vp_row, ts)
                if departure is not None:
                    candidates.append(departure)
                origin = origins.get(trip_id or "")
                skip_sequence = origin[0] if origin else None

                # Stops passed between this fix and the trip's previous one.
                # The keyset scan is in timestamp order and last_fix spans
                # batches, so this matches the live loop exactly. Listed first
                # so an interpolated crossing time beats the point match in
                # _dedupe.
                previous = last_fix.get(trip_id) if trip_id else None
                if previous is not None:
                    candidates.extend(
                        classify_segment_arrivals(
                            previous[0], previous[1], vp_row, ts, schedule,
                            skip_sequence=skip_sequence,
                            active_trips=active_trips,
                        )
                    )
                if trip_id:
                    last_fix[trip_id] = (vp_row, ts)

                arrival = classify_arrival(
                    vp_row,
                    schedule,
                    ts,
                    skip_sequence=skip_sequence,
                    active_trips=active_trips,
                )
                if arrival is not None:
                    candidates.append(arrival)

            # Trips that went quiet at their origin, and trips that went quiet
            # on approach to their terminus: resolve both against this batch's
            # watermark rather than holding them until the end of the replay.
            if last_ts is not None:
                candidates.extend(tracker.flush(last_ts))
                candidates.extend(terminus.flush(last_ts))

            events = _dedupe(candidates, seen, finished, schedule, terminus)
            if events:
                async with AsyncSessionLocal() as writer:
                    async with writer.begin():
                        await writer.execute(insert(_SHADOW), events)
                total += len(events)

            if last_ts is not None:
                active_trips.prune(last_ts)

            print(f"  …processed up to {last_ts}: {total} arrivals so far", flush=True)
            if len(rows) < batch_size:
                break

    # Anything still sitting at an origin, or still short of its terminus, when
    # the positions ran out.
    watermark = last_ts or datetime.now(timezone.utc)
    trailing = _dedupe(
        tracker.flush(watermark, force=True) + terminus.flush(watermark, force=True),
        seen, finished, schedule, terminus,
    )
    if trailing:
        async with AsyncSessionLocal() as writer:
            async with writer.begin():
                await writer.execute(insert(_SHADOW), trailing)
        total += len(trailing)

    return total


async def _refresh_caggs() -> None:
    # refresh_continuous_aggregate() can't run inside a transaction.
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for name in ("trip_ontime_hourly", "stop_delay_daily"):
            await conn.execute(
                text(f"CALL refresh_continuous_aggregate('{name}', NULL, NULL);")
            )


async def _run(batch_size: int, *, force: bool) -> None:
    print(f"Building {_SHADOW_NAME} ({_LIVE.name} stays live) …")
    await _create_shadow()
    print("Replaying vehicle positions …")
    total = await _backfill(batch_size)
    print(f"Replayed {total} arrival events. Swapping into {_LIVE.name} …")
    swapped = await _swap(force=force)
    await _drop_shadow()
    print(f"Swapped in {swapped} rows. Refreshing aggregates …")
    await _refresh_caggs()
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=20000)
    parser.add_argument(
        "--force", action="store_true",
        help="swap even if the replay produced less than half the stored rows",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.batch_size, force=args.force))


if __name__ == "__main__":
    main()
