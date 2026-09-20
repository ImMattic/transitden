"""Ingest-leader election across uvicorn workers.

The real thing needs Postgres advisory locks, so these drive it with a fake
engine and check the protocol: who holds the lock, when a connection is handed
back versus invalidated, and how a lost leader is replaced.
"""
from __future__ import annotations

import pytest

from app.services import leader


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class FakeConn:
    def __init__(self, lock_granted: bool = True, ping_fails: bool = False):
        self.lock_granted = lock_granted
        self.ping_fails = ping_fails
        self.statements: list[str] = []
        self.closed = False
        self.invalidated = False

    async def execution_options(self, **_):
        return self

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "pg_try_advisory_lock" in sql:
            return FakeResult(self.lock_granted)
        if self.ping_fails:
            raise ConnectionError("gone")
        return FakeResult(1)

    async def invalidate(self):
        self.invalidated = True

    async def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, *conns: FakeConn, dialect: str = "postgresql"):
        self.dialect = type("D", (), {"name": dialect})()
        self._conns = list(conns)
        self.connects = 0

    async def connect(self):
        self.connects += 1
        return self._conns.pop(0)


@pytest.fixture(autouse=True)
def _reset_leader(monkeypatch):
    monkeypatch.setattr(leader, "_leader_conn", None)


def use_engine(monkeypatch, engine: FakeEngine) -> FakeEngine:
    monkeypatch.setattr(leader, "engine", engine)
    return engine


async def test_non_postgres_is_always_leader(monkeypatch):
    engine = use_engine(monkeypatch, FakeEngine(dialect="sqlite"))
    assert await leader.is_ingest_leader() is True
    assert engine.connects == 0


async def test_free_lock_makes_this_worker_leader_and_it_keeps_the_connection(monkeypatch):
    conn = FakeConn(lock_granted=True)
    engine = use_engine(monkeypatch, FakeEngine(conn))

    assert await leader.is_ingest_leader() is True
    assert await leader.is_ingest_leader() is True

    assert engine.connects == 1
    assert sum("pg_try_advisory_lock" in s for s in conn.statements) == 1
    assert not conn.closed and not conn.invalidated


async def test_held_lock_leaves_this_worker_a_follower_and_frees_its_connection(monkeypatch):
    conn = FakeConn(lock_granted=False)
    use_engine(monkeypatch, FakeEngine(conn))

    assert await leader.is_ingest_leader() is False

    assert conn.closed
    assert not conn.invalidated
    assert leader._leader_conn is None


async def test_lost_leader_connection_is_invalidated_so_the_lock_is_released(monkeypatch):
    stale = FakeConn(lock_granted=True)
    fresh = FakeConn(lock_granted=True)
    use_engine(monkeypatch, FakeEngine(stale, fresh))

    assert await leader.is_ingest_leader() is True
    stale.ping_fails = True
    assert await leader.is_ingest_leader() is True

    assert stale.invalidated
    assert leader._leader_conn is fresh


async def test_lost_leader_yields_to_a_worker_that_took_the_lock(monkeypatch):
    stale = FakeConn(lock_granted=True)
    taken = FakeConn(lock_granted=False)
    use_engine(monkeypatch, FakeEngine(stale, taken))

    assert await leader.is_ingest_leader() is True
    stale.ping_fails = True
    assert await leader.is_ingest_leader() is False
    assert leader._leader_conn is None


async def test_connect_failure_is_a_follower_not_a_crash(monkeypatch):
    class Boom(FakeEngine):
        async def connect(self):
            raise OSError("db down")

    use_engine(monkeypatch, Boom())
    assert await leader.is_ingest_leader() is False
