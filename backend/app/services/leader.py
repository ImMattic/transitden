"""Elect one ingesting worker when uvicorn runs more than one process.

Every worker runs the scheduler, but only the holder of a Postgres session-level
advisory lock actually ingests. The lock lives on a dedicated connection, so a
leader that dies frees it the moment its connection drops and a follower takes
over on its next tick.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.database import engine

logger = logging.getLogger(__name__)

_LOCK_KEY = 7_284_001

_leader_conn: AsyncConnection | None = None


async def _drop_leadership() -> None:
    global _leader_conn
    conn, _leader_conn = _leader_conn, None
    if conn is None:
        return
    # close() would hand the connection back to the pool with the lock still
    # held on its session; invalidate() ends the session, which frees the lock.
    try:
        await conn.invalidate()
        await conn.close()
    except Exception:
        logger.debug("Error while dropping ingest leadership", exc_info=True)


async def is_ingest_leader() -> bool:
    """True if this process holds the ingest lock, taking it if it is free."""
    global _leader_conn
    if engine.dialect.name != "postgresql":
        return True

    if _leader_conn is not None:
        try:
            await _leader_conn.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.warning("Ingest leader connection lost; giving up leadership")
            await _drop_leadership()

    conn = None
    try:
        conn = await engine.connect()
        # No open transaction: idle_in_transaction_session_timeout would kill it.
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        row = await conn.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
        )
        acquired = bool(row.scalar_one())
    except Exception:
        logger.exception("Ingest leader election failed")
        if conn is not None:
            await conn.close()
        return False

    if not acquired:
        await conn.close()
        return False

    _leader_conn = conn
    logger.info("This worker is now the ingest leader")
    return True
