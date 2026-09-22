"""PostgreSQL connection helpers for account and rate-limit state."""

from __future__ import annotations

from contextlib import contextmanager

from .settings import Settings, get_settings

_pool = None


def database_configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool((s.database_url or "").strip())


def get_pool(settings: Settings | None = None):
    global _pool
    s = settings or get_settings()
    if not database_configured(s):
        raise RuntimeError("DATABASE_URL is not configured")
    if _pool is None:
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            conninfo=s.database_url,
            min_size=1,
            max_size=max(4, s.db_pool_max),
            kwargs={"autocommit": False},
            open=True,
        )
    return _pool


@contextmanager
def db_conn(settings: Settings | None = None):
    pool = get_pool(settings)
    with pool.connection() as conn:
        yield conn
        conn.commit()


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
