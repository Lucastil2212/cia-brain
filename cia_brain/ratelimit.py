"""Postgres-backed sliding rate limits for anonymous and API-key traffic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .db import database_configured, db_conn
from .settings import Settings, get_settings

# In-process fallback when DATABASE_URL is absent (local compose without Postgres).
_MEMORY: dict[tuple[str, str], tuple[datetime, int]] = {}


def _truncate(now: datetime, seconds: int) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), UTC)


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
        key = (bucket_key, window_start.isoformat())
        started, count = _MEMORY.get(key, (window_start, 0))
        if started != window_start:
            count = 0
        if count >= limit:
            headers = {
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(reset_at.timestamp())),
            }
            return False, headers
        _MEMORY[key] = (window_start, count + 1)
        remaining = max(0, limit - (count + 1))
        return True, {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(reset_at.timestamp())),
        }

    with db_conn(s) as conn:
        # Drop old windows opportunistically.
        conn.execute(
            "DELETE FROM rate_buckets WHERE window_start < %s",
            (now - timedelta(days=2),),
        )
        row = conn.execute(
            "SELECT count FROM rate_buckets WHERE bucket_key=%s AND window_start=%s",
            (bucket_key, window_start),
        ).fetchone()
        count = int(row[0]) if row else 0
        if count >= limit:
            return False, {
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(reset_at.timestamp())),
            }
        if row:
            conn.execute(
                "UPDATE rate_buckets SET count=count+1 WHERE bucket_key=%s AND window_start=%s",
                (bucket_key, window_start),
            )
        else:
            conn.execute(
                "INSERT INTO rate_buckets(bucket_key, window_start, count) VALUES (%s,%s,1)",
                (bucket_key, window_start),
            )
        remaining = max(0, limit - (count + 1))
        return True, {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(reset_at.timestamp())),
        }
