"""PostgreSQL connection helpers for account and rate-limit state."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

from .settings import Settings, get_settings

log = logging.getLogger(__name__)

_pool = None
_lock = threading.Lock()
_schema_lock = threading.Lock()
_disabled_reason: str | None = None
_last_error: str | None = None
_retry_at = 0.0
_schema_ready = False


class DatabaseUnavailable(RuntimeError):
    """The account database is configured but cannot be used right now."""


def database_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool((s.database_url or "").strip())


def database_problem() -> str | None:
    """Client-safe reason the configured database is unusable, or None if it looks healthy."""
    if _disabled_reason:
        return "account features are disabled by server configuration"
    if _last_error and time.monotonic() < _retry_at:
        return _last_error
    return None


def disable_database(reason: str) -> None:
    """Refuse all database use for this process (e.g. insecure JWT configuration)."""
    global _disabled_reason
    _disabled_reason = reason


def _mark_failed(settings: Settings, exc: BaseException) -> None:
    global _last_error, _retry_at
    _last_error = f"account database unavailable: {type(exc).__name__}"
    _retry_at = time.monotonic() + max(0.0, settings.db_retry_after_seconds)
    log.warning("account database unavailable: %s", exc)


def get_pool(settings: Settings | None = None):
    global _pool
    s = settings or get_settings()
    if not database_configured(s):
        raise RuntimeError("DATABASE_URL is not configured")
    problem = database_problem()
    if problem:
        raise DatabaseUnavailable(problem)
    if _pool is None:
        with _lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool

                pool = ConnectionPool(
                    conninfo=s.database_url,
                    min_size=1,
                    max_size=max(4, s.db_pool_max),
                    timeout=s.db_pool_timeout_seconds,
                    kwargs={"autocommit": False, "connect_timeout": s.db_connect_timeout_seconds},
                    open=False,
                )
                pool.open(wait=False)
                _pool = pool
    return _pool


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    from .auth import SCHEMA

    # Concurrent CREATE TABLE IF NOT EXISTS can still collide in Postgres catalogs.
    with _schema_lock:
        if _schema_ready:
            return
        conn.execute(SCHEMA)
        conn.commit()
        _schema_ready = True


@contextmanager
def db_conn(settings: Settings | None = None):
    global _last_error
    import psycopg
    from psycopg_pool import PoolTimeout

    s = settings or get_settings()
    pool = get_pool(s)
    try:
        with pool.connection() as conn:
            _ensure_schema(conn)
            yield conn
            conn.commit()
    except (PoolTimeout, psycopg.OperationalError) as exc:
        _mark_failed(s, exc)
        raise DatabaseUnavailable(_last_error or "account database unavailable") from exc
    _last_error = None


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
