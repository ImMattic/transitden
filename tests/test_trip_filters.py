"""Filter logic behind /api/v1/vehicles/active.

The endpoint's SQL uses TimescaleDB's LAST(), which SQLite can't run, so the
pieces exercised here are the pure ones: how a raw aggregate row becomes a trip,
what each filter keeps, and the facet counts the filter menu reads.  Parameter
validation is checked through the API, since it happens before any SQL runs.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1.vehicles import (
    _build_facets,
    _delay_map,
    _delay_window,
    _describe_trip,
    _matches_filters,
    _mode_of,
    _sorted_trips,
    _split_csv,
    _trip_delay_stats,
)
from app.models.stop_arrival import StopArrivalEvent

NOW = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
LIVE_CUTOFF = NOW - timedelta(seconds=90)

ROUTES_STATIC = {
    "r15": {"route_short_name": "15", "route_color": "0055B8", "route_type": "3"},
    "rE": {"route_short_name": "E", "route_color": "691F74", "route_type": "0"},
}
STOPS_STATIC = {
    "s1": {"stop_name": "Union Station"},
    "s9": {"stop_name": "Nine Mile"},
}
ENDPOINT_STOPS = {"t1": ("s1", "s9"), "t2": ("s1", "s9")}
ENDPOINT_SEQUENCES = {"t1": (1, 40), "t2": (1, 40)}


def make_row(**overrides) -> dict:
    row = {
        "vehicle_label": "1001",
        "vehicle_id": "v1",
        "trip_id": "t1",
        "route_id": "r15",
        "start_time": NOW - timedelta(minutes=50),
        "end_time": NOW - timedelta(minutes=10),
        "latitude": 39.75,
        "longitude": -104.99,
        "occupancy_status": "MANY_SEATS_AVAILABLE",
        "observation_count": 80,
        "stop_arrival_count": 30,
        "max_stop_sequence": 40,
    }
    row.update(overrides)
    return row


def describe(**overrides) -> dict:
    return _describe_trip(
        make_row(**overrides),
        ROUTES_STATIC,
        STOPS_STATIC,
        ENDPOINT_STOPS,
        ENDPOINT_SEQUENCES,
        LIVE_CUTOFF,
    )


# ── Row → trip ───────────────────────────────────────────────────────────────


def test_mode_comes_from_gtfs_route_type():
    assert _mode_of("0") == "rail"
    assert _mode_of("2") == "rail"
    assert _mode_of("3") == "bus"
    assert _mode_of("11") == "other"
    assert _mode_of(None) == "other"


def test_trip_carries_route_metadata_and_endpoints():
    trip = describe()
    assert trip["route_short_name"] == "15"
    assert trip["mode"] == "bus"
    assert trip["start_stop_name"] == "Union Station"
    assert trip["end_stop_name"] == "Nine Mile"
    assert trip["duration_minutes"] == 40.0


def test_finished_trip_at_its_terminus_is_complete():
    assert describe()["trip_status"] == "complete"


def test_finished_trip_short_of_its_terminus_is_incomplete():
    trip = describe(max_stop_sequence=22)
    assert trip["reached_terminus"] is False
    assert trip["trip_status"] == "incomplete"


def test_trip_still_reporting_is_in_progress():
    trip = describe(end_time=NOW - timedelta(seconds=30), max_stop_sequence=12)
    assert trip["in_progress"] is True
    # Being short of the terminus doesn't make a running trip an incident.
    assert trip["trip_status"] == "in_progress"


def test_trip_missing_from_the_static_schedule_is_not_flagged():
    # No entry in ENDPOINT_SEQUENCES: a GTFS gap shouldn't read as a failure.
    trip = describe(trip_id="t-unknown", max_stop_sequence=None)
    assert trip["reached_terminus"] is True
    assert trip["trip_status"] == "complete"


# ── Filters ──────────────────────────────────────────────────────────────────

NO_FILTERS = dict(
    routes=set(),
    modes=set(),
    labels=set(),
    statuses=set(),
    occupancy=set(),
    min_duration_minutes=None,
    max_duration_minutes=None,
)


def matches(trip: dict, **overrides) -> bool:
    return _matches_filters(trip, **{**NO_FILTERS, **overrides})


def test_empty_filter_set_keeps_everything():
    assert matches(describe()) is True


def test_each_group_ors_within_itself():
    trip = describe()
    assert matches(trip, routes={"r15", "rE"}) is True
    assert matches(trip, routes={"rE"}) is False


def test_groups_and_together():
    trip = describe()
    assert matches(trip, routes={"r15"}, modes={"bus"}) is True
    assert matches(trip, routes={"r15"}, modes={"rail"}) is False


def test_vehicle_label_and_status_filters():
    trip = describe()
    assert matches(trip, labels={"1001"}) is True
    assert matches(trip, labels={"9999"}) is False
    assert matches(trip, statuses={"complete"}) is True
    assert matches(trip, statuses={"in_progress"}) is False


def test_occupancy_filter_treats_a_silent_vehicle_as_unknown():
    trip = describe(occupancy_status=None)
    assert matches(trip, occupancy={"UNKNOWN"}) is True
    assert matches(trip, occupancy={"FULL"}) is False


def test_duration_bounds_are_inclusive():
    trip = describe()  # 40 minutes
    assert matches(trip, min_duration_minutes=40) is True
    assert matches(trip, max_duration_minutes=40) is True
    assert matches(trip, min_duration_minutes=41) is False
    assert matches(trip, max_duration_minutes=39) is False


def test_avg_delay_bounds_are_inclusive_and_signed():
    trip = describe()
    trip["avg_delay_seconds"] = -30.0
    assert matches(trip, min_avg_delay_seconds=-30) is True
    assert matches(trip, min_avg_delay_seconds=-29) is False
    assert matches(trip, max_avg_delay_seconds=-30) is True
    assert matches(trip, max_avg_delay_seconds=-31) is False


def test_avg_delay_bound_excludes_a_trip_with_no_observed_arrivals():
    trip = describe()
    trip["avg_delay_seconds"] = None
    assert matches(trip, min_avg_delay_seconds=0) is False
    assert matches(trip, max_avg_delay_seconds=0) is False


def test_on_time_pct_bounds_are_inclusive():
    trip = describe()
    trip["on_time_pct"] = 80.0
    assert matches(trip, min_on_time_pct=80) is True
    assert matches(trip, min_on_time_pct=81) is False
    assert matches(trip, max_on_time_pct=80) is True
    assert matches(trip, max_on_time_pct=79) is False


def test_on_time_pct_bound_excludes_a_trip_with_no_observed_arrivals():
    trip = describe()
    trip["on_time_pct"] = None
    assert matches(trip, min_on_time_pct=50) is False


# ── Avg delay / on-time aggregation ─────────────────────────────────────────
# Reads app.models.stop_arrival.StopArrivalEvent directly, unlike the endpoint's
# own aggregate SQL — plain AVG/CASE/GROUP BY, so SQLite can run it.


_arrival_ids = itertools.count(1)


def _arrival(**overrides) -> StopArrivalEvent:
    # SQLite's autoincrement doesn't kick in for this BigInteger PK under the
    # async driver used in tests, so every row needs an explicit id.
    base = dict(
        id=next(_arrival_ids),
        trip_id="t1",
        route_id="r15",
        stop_id="s1",
        stop_sequence=1,
        scheduled_time=NOW,
        actual_time=NOW,
        delay_seconds=0,
        service_date=NOW.date(),
        timestamp=NOW,
    )
    base.update(overrides)
    return StopArrivalEvent(**base)


@pytest.mark.asyncio
async def test_trip_delay_stats_averages_delay_and_scores_on_time(db_session):
    db_session.add_all(
        [
            _arrival(delay_seconds=0),
            _arrival(stop_id="s2", stop_sequence=2, delay_seconds=600),
        ]
    )
    await db_session.commit()

    stats = await _trip_delay_stats(
        db_session, NOW - timedelta(hours=1), NOW + timedelta(hours=1), {"t1"}
    )
    avg_delay, on_time_pct = stats["t1"]
    assert avg_delay == 300.0
    # Only the 0s arrival is within RTD's default ±300s on-time window.
    assert on_time_pct == 50.0


@pytest.mark.asyncio
async def test_trip_delay_stats_of_no_trip_ids_is_empty(db_session):
    assert await _trip_delay_stats(db_session, NOW, NOW, set()) == {}


@pytest.mark.asyncio
async def test_trip_delay_stats_omits_a_trip_outside_the_scan_window(db_session):
    db_session.add(_arrival())
    await db_session.commit()

    stats = await _trip_delay_stats(
        db_session, NOW + timedelta(hours=2), NOW + timedelta(hours=3), {"t1"}
    )
    assert stats == {}


# ── Facets ───────────────────────────────────────────────────────────────────


def test_facets_count_routes_vehicles_and_statuses():
    trips = [
        describe(),
        describe(trip_id="t2", max_stop_sequence=5),
        describe(
            vehicle_label="2002",
            trip_id="t2",
            route_id="rE",
            end_time=NOW - timedelta(seconds=20),
        ),
    ]
    facets = _build_facets(trips)

    assert facets["trip_count"] == 3
    assert facets["statuses"] == {"in_progress": 1, "complete": 1, "incomplete": 1}
    assert facets["modes"] == {"rail": 1, "bus": 2, "other": 0}
    assert [(r["route_short_name"], r["trip_count"]) for r in facets["routes"]] == [
        ("15", 2),
        ("E", 1),
    ]
    assert [v["vehicle_label"] for v in facets["vehicles"]] == ["1001", "2002"]
    assert facets["occupancy"] == {"MANY_SEATS_AVAILABLE": 3}


def test_facets_list_every_route_a_vehicle_served():
    trips = [describe(), describe(trip_id="t2", route_id="rE")]
    vehicles = _build_facets(trips)["vehicles"]
    assert vehicles[0]["route_short_names"] == ["15", "E"]
    assert vehicles[0]["trip_count"] == 2


def test_facets_sort_numbered_routes_numerically():
    """"15" before "120", and the lettered rail lines after both."""
    routes_static = {
        "r120": {"route_short_name": "120", "route_type": "3"},
        "r15": {"route_short_name": "15", "route_type": "3"},
        "rE": {"route_short_name": "E", "route_type": "0"},
    }
    trips = [
        _describe_trip(
            make_row(route_id=rid, vehicle_label=label),
            routes_static,
            STOPS_STATIC,
            ENDPOINT_STOPS,
            ENDPOINT_SEQUENCES,
            LIVE_CUTOFF,
        )
        for rid, label in (("r120", "1001"), ("r15", "1002"), ("rE", "1003"))
    ]
    assert [r["route_short_name"] for r in _build_facets(trips)["routes"]] == [
        "15",
        "120",
        "E",
    ]


def test_facets_of_an_empty_window_are_still_well_formed():
    facets = _build_facets([])
    assert facets["trip_count"] == 0
    assert facets["routes"] == []
    assert facets["max_duration_minutes"] == 0.0


# ── Query parameters ─────────────────────────────────────────────────────────


def test_split_csv_drops_blanks_and_whitespace():
    assert _split_csv("a, b ,,c") == ["a", "b", "c"]
    assert _split_csv(None) == []
    assert _split_csv("") == []


@pytest.mark.asyncio
async def test_unknown_status_is_rejected_before_any_query_runs(client):
    resp = await client.get("/api/v1/vehicles/active", params={"status": "exploded"})
    assert resp.status_code == 422
    assert "exploded" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_mode_is_rejected(client):
    resp = await client.get("/api/v1/vehicles/active", params={"modes": "ferry"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_negative_duration_is_rejected(client):
    resp = await client.get(
        "/api/v1/vehicles/active", params={"min_duration_minutes": -5}
    )
    assert resp.status_code == 422


def _span(start: datetime, end: datetime) -> dict:
    return {"start_time": start.isoformat(), "end_time": end.isoformat()}


def test_delay_window_hugs_the_page_trips_not_the_padded_scan_window():
    floor, ceil = NOW - timedelta(hours=10), NOW + timedelta(hours=10)
    page = [
        _span(NOW - timedelta(hours=2), NOW - timedelta(hours=1)),
        _span(NOW - timedelta(hours=1, minutes=30), NOW),
    ]
    lo, hi = _delay_window(page, floor, ceil)
    assert lo == NOW - timedelta(hours=2, minutes=5)
    assert hi == NOW + timedelta(minutes=5)


def test_delay_window_never_exceeds_the_scan_window():
    floor, ceil = NOW - timedelta(hours=1), NOW
    page = [_span(NOW - timedelta(hours=1), NOW)]
    assert _delay_window(page, floor, ceil) == (floor, ceil)


def test_delay_window_of_an_empty_page_is_the_scan_window():
    floor, ceil = NOW - timedelta(hours=1), NOW
    assert _delay_window([], floor, ceil) == (floor, ceil)


async def test_delay_map_of_no_trips_is_empty(db_session):
    assert await _delay_map(db_session, NOW - timedelta(hours=1), NOW, {}) == {}


# ── Sorting ──────────────────────────────────────────────────────────────────


def ids(trips: list[dict]) -> list[str]:
    return [t["trip_id"] for t in trips]


def test_no_sort_keeps_the_window_order_but_returns_a_new_list():
    trips = [describe(trip_id="b"), describe(trip_id="a")]
    out = _sorted_trips(trips, None, False)
    assert ids(out) == ["b", "a"]
    assert out is not trips


def test_sorting_never_reorders_the_input_list():
    trips = [describe(trip_id="a", vehicle_label="20"), describe(trip_id="b", vehicle_label="3")]
    _sorted_trips(trips, "vehicle", False)
    assert ids(trips) == ["a", "b"]


def test_occupancy_sorts_by_crowding_not_alphabetically():
    levels = ["FULL", "EMPTY", "STANDING_ROOM_ONLY", "MANY_SEATS_AVAILABLE",
              "CRUSHED_STANDING_ROOM_ONLY", "FEW_SEATS_AVAILABLE"]
    trips = [describe(trip_id=lvl, occupancy_status=lvl) for lvl in levels]
    assert ids(_sorted_trips(trips, "occupancy", False)) == [
        "EMPTY", "MANY_SEATS_AVAILABLE", "FEW_SEATS_AVAILABLE",
        "STANDING_ROOM_ONLY", "CRUSHED_STANDING_ROOM_ONLY", "FULL",
    ]
    assert ids(_sorted_trips(trips, "occupancy", True)) == [
        "FULL", "CRUSHED_STANDING_ROOM_ONLY", "STANDING_ROOM_ONLY",
        "FEW_SEATS_AVAILABLE", "MANY_SEATS_AVAILABLE", "EMPTY",
    ]


@pytest.mark.parametrize("descending", [False, True])
def test_trips_that_never_reported_occupancy_sort_last_in_either_direction(descending):
    trips = [
        describe(trip_id="none", occupancy_status=None),
        describe(trip_id="unknown", occupancy_status="UNKNOWN"),
        describe(trip_id="empty", occupancy_status="EMPTY"),
        describe(trip_id="full", occupancy_status="FULL"),
    ]
    out = ids(_sorted_trips(trips, "occupancy", descending))
    assert out[:2] == (["full", "empty"] if descending else ["empty", "full"])
    assert set(out[2:]) == {"none", "unknown"}


@pytest.mark.parametrize("descending", [False, True])
def test_missing_delay_and_on_time_sort_last_in_either_direction(descending):
    trips = [describe(trip_id=n) for n in ("none", "early", "late")]
    for t, delay in zip(trips, (None, -120.0, 400.0)):
        t["avg_delay_seconds"] = delay
        t["on_time_pct"] = delay
    for key in ("avg_delay", "on_time"):
        out = ids(_sorted_trips(trips, key, descending))
        assert out == (["late", "early", "none"] if descending else ["early", "late", "none"])


def test_route_and_vehicle_sort_numerically_with_letters_after():
    trips = [
        describe(trip_id="rE", route_id="rE", vehicle_label="E12"),
        describe(trip_id="r15", route_id="r15", vehicle_label="1010"),
        describe(trip_id="r120", route_id="r15", vehicle_label="990"),
    ]
    trips[2]["route_short_name"] = "120"
    assert ids(_sorted_trips(trips, "route", False)) == ["r15", "r120", "rE"]
    assert ids(_sorted_trips(trips, "route", True)) == ["rE", "r120", "r15"]
    assert ids(_sorted_trips(trips, "vehicle", False)) == ["r120", "r15", "rE"]


def test_start_end_and_duration_sort_by_time():
    trips = [
        describe(trip_id="long", start_time=NOW - timedelta(minutes=90), end_time=NOW),
        describe(trip_id="short", start_time=NOW - timedelta(minutes=20), end_time=NOW - timedelta(minutes=5)),
        describe(trip_id="mid", start_time=NOW - timedelta(minutes=50), end_time=NOW - timedelta(minutes=10)),
    ]
    assert ids(_sorted_trips(trips, "start", False)) == ["long", "mid", "short"]
    assert ids(_sorted_trips(trips, "end", True)) == ["long", "short", "mid"]
    assert ids(_sorted_trips(trips, "duration", True)) == ["long", "mid", "short"]


@pytest.mark.parametrize("descending", [False, True])
def test_ties_keep_the_window_order_in_both_directions(descending):
    trips = [describe(trip_id=n, occupancy_status="FULL") for n in ("a", "b", "c")]
    assert ids(_sorted_trips(trips, "occupancy", descending)) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_unknown_sort_column_or_direction_is_rejected(client):
    for params in ({"sort_by": "colour"}, {"sort_by": "duration", "sort_dir": "sideways"}):
        resp = await client.get("/api/v1/vehicles/active", params=params)
        assert resp.status_code == 422
