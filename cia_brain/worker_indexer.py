import asyncio
import atexit

from .bus import consume_forever
from .indexer import SearchStore
from .log import configure_logging
from .settings import get_settings


async def main_async():
    s = get_settings()
    configure_logging(s.log_level)
    store = await asyncio.to_thread(SearchStore, s)
    atexit.register(store.flush)

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
