"""Apply Postgres schema migrations for accounts and rate limits."""

from __future__ import annotations

import logging

from .auth import migrate
from .db import database_configured
from .log import configure_logging
from .settings import get_settings

log = logging.getLogger(__name__)


def main() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    if not database_configured(s):
        log.warning("DATABASE_URL unset; skipping migrations")
        return
    migrate(s)
    log.info("Postgres account schema ready")


if __name__ == "__main__":
    main()
