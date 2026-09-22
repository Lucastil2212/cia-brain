import asyncio
import logging

from .bus import connect, consume_forever, publish_json
from .extract import normalize_document
from .log import configure_logging
from .settings import get_settings


async def main_async():
    s = get_settings()
    configure_logging(s.log_level)
    nc, js = await connect(s)

    async def handler(payload: dict):
        event = await asyncio.to_thread(normalize_document, payload, s)
        await publish_json(js, "cia.extracted", event.to_dict())

    try:
        await consume_forever(s, "cia.fetched", "extractor-v1", handler)
    finally:
        await nc.drain()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
