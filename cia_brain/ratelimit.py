"""Postgres-backed sliding rate limits for anonymous and API-key traffic."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from .db import DatabaseUnavailable, database_configured, db_conn
from .settings import Settings, get_settings

log = logging.getLogger(__name__)

# In-process fallback when DATABASE_URL is absent or the database is unreachable.
_MEMORY: dict[tuple[str, str], int] = {}
_MEMORY_LOCK = threading.Lock()
_MEMORY_MAX_KEYS = 50_000


def _truncate(now: datetime, seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), UTC)


def _headers(limit: int, remaining: int, reset_at: datetime) -> dict[str, str]:
    return {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(max(0, remaining)),
        "X-RateLimit-Reset": str(int(reset_at.timestamp())),
    }


def _prune_memory(now: datetime) -> None:
    cutoff = (now - timedelta(days=2)).isoformat()
    for key in [k for k in _MEMORY if k[1] < cutoff]:
        del _MEMORY[key]


def _memory_check(
    bucket_key: str, limit: int, window_start: datetime, reset_at: datetime, now: datetime
) -> tuple[bool, dict[str, str]]:
    key = (bucket_key, window_start.isoformat())
    with _MEMORY_LOCK:
        if len(_MEMORY) > _MEMORY_MAX_KEYS:
            _prune_memory(now)
        count = _MEMORY.get(key, 0)
        if count >= limit:
            return False, _headers(limit, 0, reset_at)
        _MEMORY[key] = count + 1
    return True, _headers(limit, limit - (count + 1), reset_at)


def _db_check(
    bucket_key: str,
    limit: int,
    window_start: datetime,
    reset_at: datetime,
    now: datetime,
    s: Settings,
) -> tuple[bool, dict[str, str]]:
    with db_conn(s) as conn:
        conn.execute(
            "DELETE FROM rate_buckets WHERE window_start < %s",
            (now - timedelta(days=2),),
        )
        # Single upsert avoids the read-then-write race between concurrent requests.
        row = conn.execute(
            """
            INSERT INTO rate_buckets(bucket_key, window_start, count) VALUES (%s, %s, 1)
            ON CONFLICT (bucket_key, window_start)
            DO UPDATE SET count = rate_buckets.count + 1
            WHERE rate_buckets.count < %s
            RETURNING count
            """,
            (bucket_key, window_start, limit),
        ).fetchone()
    if row is None:
        return False, _headers(limit, 0, reset_at)
    count = int(row[0])
    return True, _headers(limit, limit - count, reset_at)


def check_rate_limit(
    bucket_key: str,
    *,
    limit: int,
    window_seconds: int,
    settings: Settings | None = None,
) -> tuple[bool, dict[str, str | int]]:
    """Return (allowed, headers). Increments the counter when allowed."""
    if limit <= 0:
        return True, {}
    s = settings or get_settings()
    now = datetime.now(UTC)
    window_start = _truncate(now, window_seconds)
    reset_at = window_start + timedelta(seconds=window_seconds)

    if not database_configured(s):
        return _memory_check(bucket_key, limit, window_start, reset_at, now)
    try:
        return _db_check(bucket_key, limit, window_start, reset_at, now, s)
    except DatabaseUnavailable:
        return _memory_check(bucket_key, limit, window_start, reset_at, now)
    except Exception as exc:
        # Rate limiting must never take the API down with it.
        log.warning("rate limit store unavailable, using in-memory limits: %s", exc)
        return _memory_check(bucket_key, limit, window_start, reset_at, now)
