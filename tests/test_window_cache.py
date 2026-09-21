"""The Trip Explorer's window cache: what is reused, for how long, and how.

The endpoint's SQL uses TimescaleDB's LAST(), which SQLite can't run, so the
API-level tests swap the window builder for a stub and check that paging and
filtering are served from one build.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1 import vehicles
from tests.test_trip_filters import describe


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    vehicles._window_cache.clear()
    vehicles._window_locks.clear()
    clock = FakeClock()
    monkeypatch.setattr(vehicles, "_time", clock)
    yield clock
    vehicles._window_cache.clear()
    vehicles._window_locks.clear()


def counting_build(result=None):
    calls = {"n": 0}

    async def build():
        calls["n"] += 1
        await asyncio.sleep(0)
        return result if result is not None else ([{"trip_id": str(calls["n"])}], {})

    return build, calls


# ── Reuse and expiry ────────────────────────────────────────────────────────


async def test_second_request_for_the_same_window_reuses_the_first_build():
    build, calls = counting_build()
    first = await vehicles._cached_window(("w",), 60, build)
    second = await vehicles._cached_window(("w",), 60, build)
    assert calls["n"] == 1
    assert first is not None and second == first


async def test_different_windows_do_not_share_an_entry():
    build, calls = counting_build()
    await vehicles._cached_window(("a",), 60, build)
    await vehicles._cached_window(("b",), 60, build)
    assert calls["n"] == 2


async def test_entry_is_rebuilt_once_its_ttl_has_passed(_fresh_cache):
    build, calls = counting_build()
    await vehicles._cached_window(("w",), 60, build)
    _fresh_cache.now += 59
    await vehicles._cached_window(("w",), 60, build)
    assert calls["n"] == 1
    _fresh_cache.now += 2
    await vehicles._cached_window(("w",), 60, build)
    assert calls["n"] == 2


async def test_zero_ttl_bypasses_the_cache():
    build, calls = counting_build()
    await vehicles._cached_window(("w",), 0, build)
    await vehicles._cached_window(("w",), 0, build)
    assert calls["n"] == 2
    assert not vehicles._window_cache


async def test_cache_keeps_only_the_most_recent_windows():
    build, _ = counting_build()
    for i in range(vehicles._WINDOW_CACHE_MAX + 1):
        await vehicles._cached_window((i,), 60, build)
    assert len(vehicles._window_cache) == vehicles._WINDOW_CACHE_MAX
    assert (0,) not in vehicles._window_cache
    assert (vehicles._WINDOW_CACHE_MAX,) in vehicles._window_cache


# ── Concurrency and failure ─────────────────────────────────────────────────


async def test_concurrent_requests_for_one_window_share_a_single_build():
    build, calls = counting_build()
    results = await asyncio.gather(
        *(vehicles._cached_window(("w",), 60, build) for _ in range(5))
    )
    assert calls["n"] == 1
    assert all(r == results[0] for r in results)
    assert not vehicles._window_locks


async def test_failed_build_is_not_cached_and_the_next_request_retries():
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("statement timeout")
        return [{"trip_id": "t1"}], {}

    with pytest.raises(RuntimeError):
        await vehicles._cached_window(("w",), 60, flaky)
    trips, _ = await vehicles._cached_window(("w",), 60, flaky)

    assert trips == [{"trip_id": "t1"}]
    assert attempts["n"] == 2
    assert not vehicles._window_locks


# ── How long a window may be reused ─────────────────────────────────────────


def test_a_window_that_can_still_grow_gets_the_short_ttl():
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    assert vehicles._window_ttl(now, now) == vehicles._settings.active_vehicles_cache_live_seconds
    just_inside = now - vehicles._MAX_TRIP_DURATION
    assert (
        vehicles._window_ttl(just_inside, now)
        == vehicles._settings.active_vehicles_cache_live_seconds
    )


def test_a_window_whose_trips_have_all_finished_gets_the_long_ttl():
    now = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    old_end = now - vehicles._MAX_TRIP_DURATION - timedelta(minutes=11)
    assert (
        vehicles._window_ttl(old_end, now)
        == vehicles._settings.active_vehicles_cache_stable_seconds
    )


# ── Through the endpoint ────────────────────────────────────────────────────

START = "2026-09-10T10:00:00Z"
END = "2026-09-10T11:00:00Z"


def window_of(n: int) -> list[dict]:
    return [
        describe(
            trip_id=f"trip{i}",
            vehicle_label=str(1000 + i),
            start_time=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=i),
            end_time=datetime(2026, 9, 10, 10, 30, tzinfo=timezone.utc) + timedelta(minutes=i),
        )
        for i in range(n)
    ]


@pytest.fixture
def stub_build(monkeypatch):
    calls = {"n": 0}

    async def fake(db, start, end, wanted_routes, strict):
        calls["n"] += 1
        trips = window_of(30)
        for t in trips:
            t["avg_delay_seconds"] = None
            t["on_time_pct"] = None
        return trips, {"trip_count": len(trips)}

    monkeypatch.setattr(vehicles, "_build_window", fake)
    return calls


async def test_paging_a_window_scans_it_once(client, stub_build):
    page1 = (await client.get(f"/api/v1/vehicles/active?start={START}&end={END}&limit=10&offset=0")).json()
    page2 = (await client.get(f"/api/v1/vehicles/active?start={START}&end={END}&limit=10&offset=10")).json()

    assert stub_build["n"] == 1
    assert [v["trip_id"] for v in page1["vehicles"]] == [f"trip{i}" for i in range(10)]
    assert [v["trip_id"] for v in page2["vehicles"]] == [f"trip{i}" for i in range(10, 20)]
    assert page2["vehicle_count"] == 30


async def test_filtering_a_cached_window_does_not_change_what_is_cached(client, stub_build):
    filtered = (
        await client.get(f"/api/v1/vehicles/active?start={START}&end={END}&vehicle_labels=1003")
    ).json()
    unfiltered = (await client.get(f"/api/v1/vehicles/active?start={START}&end={END}")).json()

    assert stub_build["n"] == 1
    assert filtered["vehicle_count"] == 1
    assert unfiltered["vehicle_count"] == 30
    assert unfiltered["window_count"] == 30


async def test_a_different_range_or_route_is_a_different_window(client, stub_build):
    await client.get(f"/api/v1/vehicles/active?start={START}&end={END}")
    await client.get(f"/api/v1/vehicles/active?start={START}&end=2026-09-10T11:30:00Z")
    await client.get(f"/api/v1/vehicles/active?start={START}&end={END}&route_ids=r15")
    assert stub_build["n"] == 3


async def test_a_window_without_an_explicit_end_is_never_cached(client, stub_build):
    recent = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await client.get(f"/api/v1/vehicles/active?start={recent}")
    await client.get(f"/api/v1/vehicles/active?start={recent}")
    assert stub_build["n"] == 2


async def test_one_requests_page_edits_do_not_leak_into_the_cached_window(client, stub_build):
    await client.get(f"/api/v1/vehicles/active?start={START}&end={END}&limit=5")
    cached_trips, _ = next(iter(vehicles._window_cache.values()))[1:]
    assert all(t["last_delay_seconds"] is None for t in cached_trips)


async def test_sorting_is_applied_before_paging_and_leaves_the_cache_alone(client, stub_build):
    # window_of() is already in start order, so a descending sort has to reach
    # back across pages: the *last* trips of the window come first.
    page1 = (await client.get(
        f"/api/v1/vehicles/active?start={START}&end={END}&limit=10&sort_by=start&sort_dir=desc"
    )).json()
    page2 = (await client.get(
        f"/api/v1/vehicles/active?start={START}&end={END}&limit=10&offset=10&sort_by=start&sort_dir=desc"
    )).json()

    assert stub_build["n"] == 1
    assert [v["trip_id"] for v in page1["vehicles"]] == [f"trip{i}" for i in range(29, 19, -1)]
    assert [v["trip_id"] for v in page2["vehicles"]] == [f"trip{i}" for i in range(19, 9, -1)]

    cached_trips, _ = next(iter(vehicles._window_cache.values()))[1:]
    assert [t["trip_id"] for t in cached_trips] == [f"trip{i}" for i in range(30)]
