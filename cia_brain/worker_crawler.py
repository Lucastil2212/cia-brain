import asyncio

from .crawler import Crawler
from .log import configure_logging
from .settings import get_settings


def main():
    s = get_settings()
    configure_logging(s.log_level)
    asyncio.run(Crawler(s).run())


if __name__ == "__main__":
    main()
