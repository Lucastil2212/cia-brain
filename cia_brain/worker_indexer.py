import asyncio
import atexit
import logging

from .bus import consume_forever
from .indexer import SearchStore
from .log import configure_logging
from .settings import get_settings

log = logging.getLogger(__name__)


async def main_async():
    s = get_settings()
    configure_logging(s.log_level)
    store = await asyncio.to_thread(SearchStore, s)
    atexit.register(store.flush)
    try:
        replayed = await asyncio.to_thread(store.index_missing)
        if replayed:
            log.info("indexed %s stored documents that were missing from search", replayed)
    except Exception:
        log.exception("replay of stored documents failed")

    async def handler(payload: dict):
        from .paths import path_under

        normalized = path_under(payload["normalized_path"], s.normalized_dir)
        await asyncio.to_thread(store.index_normalized, str(normalized))

    try:
        await consume_forever(s, "cia.extracted", "indexer-v1", handler)
    finally:
        store.flush()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
