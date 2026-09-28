"""Full stop-by-stop trip timeline: schedule parsing + service-day anchoring.

Covers the pieces added for the "every scheduled stop, origin → terminus"
timeline on the trip detail page:

  * ``load_trip_stop_sequence`` — per-trip scan of ``stop_times.txt`` that keeps
    *every* stop (not just timepoints), against a synthetic GTFS root.
  * ``_service_day_anchor`` — placing an absolute clock on stops that were never
    geofenced, from either an observed arrival or the position track.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.v1.vehicles import _DENVER, _service_day_anchor, _signal_gaps
from app.models.stop_arrival import StopArrivalEvent
from app.services.gtfs_decoder import TRANSIT_FOLDERS
from app.services.gtfs_schedule import load_trip_stop_sequence

_STOP_TIMES = (
    '"trip_id","arrival_time","departure_time","stop_id","stop_sequence",'
    '"stop_headsign","pickup_type","drop_off_type","shape_dist_traveled","timepoint"\n'
    '"T1","06:00:00","06:00:00","S1","1",,"0","1",,"1"\n'
    '"T1","06:04:00","06:04:00","S2","2",,"0","0",,"0"\n'
    '"T1","06:09:00","06:09:00","S3","3",,"0","0",,"1"\n'
    '"T1","06:15:00","06:15:00","S4","4",,"1","0",,"1"\n'
    '"T2","07:00:00","07:00:00","S1","1",,"0","0",,"1"\n'
)

_STOPS = (
    '"stop_id","stop_name","stop_lat","stop_lon"\n'
    '"S1","Origin Station","39.10","-104.10"\n'
    '"S2","Second & Main","39.20","-104.20"\n'
    '"S3","Third & Elm","39.30","-104.30"\n'
    '"S4","Terminus Station","39.40","-104.40"\n'
)


def _gtfs_root(tmp_path):
    # Name the fixture folder after whatever sub-feed the app actually reads,
    # so a change to the gtfs-static layout doesn't silently empty these tests.
    folder = tmp_path / TRANSIT_FOLDERS[0]
    folder.mkdir()
    (folder / "stop_times.txt").write_text(_STOP_TIMES, encoding="utf-8")
    (folder / "stops.txt").write_text(_STOPS, encoding="utf-8")
    return tmp_path


def test_stop_sequence_keeps_every_stop_in_order(tmp_path):
    rows = load_trip_stop_sequence("T1", _gtfs_root(tmp_path))

    assert [r["stop_sequence"] for r in rows] == [1, 2, 3, 4]
    assert [r["stop_name"] for r in rows] == [
        "Origin Station",
        "Second & Main",
        "Third & Elm",
        "Terminus Station",
    ]
    # Intermediate non-timepoint stop is retained (old timeline dropped it).
    assert rows[1]["is_timepoint"] is False
    assert rows[0]["is_timepoint"] is True
    assert rows[0]["arrival_secs"] == 6 * 3600


def test_stop_sequence_marks_termini_pickup_dropoff(tmp_path):
    rows = load_trip_stop_sequence("T1", _gtfs_root(tmp_path))

    assert rows[0]["drop_off_type"] == "1"  # origin: board only
    assert rows[-1]["pickup_type"] == "1"   # terminus: alight only


def test_stop_sequence_unknown_trip_is_empty(tmp_path):
    assert load_trip_stop_sequence("NOPE", _gtfs_root(tmp_path)) == ()


def test_stop_sequence_prefix_scan_does_not_bleed_between_trips(tmp_path):
    # "T1" must not pick up "T2" rows despite the shared leading character.
    rows = load_trip_stop_sequence("T1", _gtfs_root(tmp_path))
    assert all(r["stop_id"] != "S1" or r["stop_sequence"] == 1 for r in rows)
    assert len(rows) == 4


def test_service_day_anchor_exact_from_observed_arrival():
    schedule = (
        {"stop_sequence": 1, "arrival_secs": 6 * 3600},
        {"stop_sequence": 2, "arrival_secs": 6 * 3600 + 240},
    )
    sched_time = datetime(2026, 6, 20, 12, 4, tzinfo=timezone.utc)
    ev = StopArrivalEvent(
        trip_id="T1", route_id="R1", stop_id="S2", stop_sequence=2,
        scheduled_time=sched_time, actual_time=sched_time, delay_seconds=0,
        service_date=sched_time.date(), timestamp=sched_time,
    )

    anchor = _service_day_anchor(schedule, {2: ev}, pos_rows=[])

    # anchor + this stop's offset reproduces the observed scheduled_time exactly.
    assert anchor + timedelta(seconds=6 * 3600 + 240) == sched_time


def test_service_day_anchor_inferred_from_position_track():
    schedule = ({"stop_sequence": 1, "arrival_secs": 6 * 3600},)

    class _Row:
        timestamp = datetime(2026, 6, 20, 12, 3, tzinfo=timezone.utc)  # 06:03 MDT

    anchor = _service_day_anchor(schedule, observed={}, pos_rows=[_Row()])

    expected_midnight = datetime(2026, 6, 20, tzinfo=_DENVER).astimezone(timezone.utc)
    assert anchor == expected_midnight


def test_signal_gaps_flags_wide_silences_only():
    t0 = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    timestamps = [
        t0,
        t0 + timedelta(seconds=30),  # ordinary poll interval
        t0 + timedelta(seconds=60),
        t0 + timedelta(seconds=360),  # 5 min silence — a real gap (NB2-style)
        t0 + timedelta(seconds=390),
    ]
    gaps = _signal_gaps(timestamps, threshold_seconds=300)
    assert len(gaps) == 1
    assert gaps[0]["start"] == timestamps[2].isoformat()
    assert gaps[0]["end"] == timestamps[3].isoformat()
    assert gaps[0]["duration_seconds"] == 300


def test_signal_gaps_empty_when_no_gap_or_too_few_points():
    t0 = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert _signal_gaps([], threshold_seconds=300) == []
    assert _signal_gaps([t0], threshold_seconds=300) == []
    assert _signal_gaps([t0, t0 + timedelta(seconds=30)], threshold_seconds=300) == []
