"""Shared Retry-After parsing for the archive crawler."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

MAX_RETRY_AFTER = 86_400


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int:
    """Seconds to wait before another request. 0 when the header is missing or useless.

    Accepts delta-seconds and HTTP-date, both allowed by RFC 9110. Caps at one day
    so a hostile or mistaken header cannot pause a source indefinitely.
    """
    if value is None:
        return 0
    raw = str(value).strip()
    if not raw:
        return 0
    try:
        seconds = int(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return 0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        seconds = int((when - current).total_seconds())
    if seconds < 0:
        return 0
    return min(seconds, MAX_RETRY_AFTER)
