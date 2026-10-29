"""Rate-limit behavior for the archive crawler and raw file reads."""

import asyncio
import time
from contextlib import asynccontextmanager
from email.utils import formatdate

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cia_brain.backoff import parse_retry_after
from cia_brain.crawler import Crawler, HostThrottle
from cia_brain.middleware import AuthRateLimitMiddleware
from cia_brain.ratelimit import _MEMORY
from cia_brain.settings import Settings


def test_parse_retry_after_seconds_date_and_garbage():
    assert parse_retry_after(None) == 0
    assert parse_retry_after("  ") == 0
    assert parse_retry_after("90") == 90
    assert parse_retry_after("999999") == 86_400
    assert parse_retry_after("-5") == 0
    assert parse_retry_after("not-a-date") == 0
    future = formatdate(time.time() + 600, usegmt=True)
    assert 580 <= parse_retry_after(future) <= 620
    past = formatdate(time.time() - 60, usegmt=True)
    assert parse_retry_after(past) == 0


class _Allow:
    async def can_fetch(self, url):
        return True

    def delay(self, url):
        return 0.0


def _crawler(tmp_path, monkeypatch, status, headers, max_retries=5):
    settings = Settings(
        data_dir=tmp_path,
        min_free_disk_gb=0,
        max_retries=max_retries,
        allowed_hosts="www.cia.gov",
        archive_sources="",
        _env_file=None,
    )
    settings.ensure_dirs()
    monkeypatch.setattr("cia_brain.crawler.disk_free_gb", lambda path: 100.0)

    class Resp:
        def __init__(self):
            self.status = status
            self.headers = headers
            self.url = "https://www.cia.gov/readingroom/document/abc"

    @asynccontextmanager
    async def fake_get(*args, **kwargs):
        yield Resp()

    monkeypatch.setattr("cia_brain.crawler.checked_get", fake_get)
    crawler = Crawler(settings)
    url = "https://www.cia.gov/readingroom/document/abc"
    crawler.frontier.add(url, 0, "seed")
    return crawler, url


def _hit(crawler, url, times):
    async def run():
        for _ in range(times):
            # The production throttle would sleep out the Retry-After. The test
            # checks that the pause was recorded, without waiting minutes.
            crawler.throttle.blocked_until.clear()
            crawler.frontier.db.execute(
                "UPDATE frontier SET status='queued', next_attempt=0 WHERE url=?",
                (url,),
            )
            crawler.frontier.db.commit()
            row = crawler.frontier.next()
            await crawler._process(None, _Allow(), row)

    asyncio.run(run())


def test_crawler_429_keeps_the_url_and_pauses_the_host(tmp_path, monkeypatch):
    when = formatdate(time.time() + 180, usegmt=True)
    crawler, url = _crawler(tmp_path, monkeypatch, 429, {"Retry-After": when}, max_retries=2)
    _hit(crawler, url, 4)
    row = crawler.frontier.db.execute("SELECT * FROM frontier WHERE url=?", (url,)).fetchone()
    assert row["status"] == "queued"
    assert row["tries"] == 4
    assert row["last_error"] == "HTTP 429"
    assert row["next_attempt"] >= time.time() + 160
    assert crawler.throttle.blocked_until.get("www.cia.gov", 0) >= time.monotonic() + 160
    crawler.frontier.db.close()


def test_crawler_404_is_skipped_without_host_pause(tmp_path, monkeypatch):
    crawler, url = _crawler(tmp_path, monkeypatch, 404, {})
    _hit(crawler, url, 1)
    row = crawler.frontier.db.execute("SELECT status, last_error FROM frontier").fetchone()
    assert row["status"] == "skipped"
    assert row["last_error"] == "HTTP 404"
    assert "www.cia.gov" not in crawler.throttle.blocked_until
    crawler.frontier.db.close()


def test_crawler_503_retries_then_skips_after_the_budget(tmp_path, monkeypatch):
    crawler, url = _crawler(tmp_path, monkeypatch, 503, {"Retry-After": "45"}, max_retries=2)
    _hit(crawler, url, 1)
    row = crawler.frontier.db.execute("SELECT status, last_error, next_attempt FROM frontier").fetchone()
    assert row["status"] == "queued"
    assert row["last_error"] == "HTTP 503"
    assert row["next_attempt"] >= time.time() + 40
    assert crawler.throttle.blocked_until.get("www.cia.gov", 0) > time.monotonic()
    _hit(crawler, url, 1)
    row = crawler.frontier.db.execute("SELECT status FROM frontier").fetchone()
    assert row["status"] == "skipped"
    crawler.frontier.db.close()


def test_host_throttle_waits_out_a_block():
    throttle = HostThrottle()

    async def run():
        throttle.block("https://www.cia.gov/a", 0.05)
        started = time.monotonic()
        await throttle.wait("https://www.cia.gov/b", 0)
        return time.monotonic() - started

    assert asyncio.run(run()) >= 0.04


def test_raw_reads_do_not_share_the_search_budget(tmp_path):
    _MEMORY.clear()
    settings = Settings(
        data_dir=tmp_path,
        database_url="",
        auth_required=False,
        trust_proxy_headers=False,
        anon_rate_limit_per_minute=2,
        anon_rate_limit_per_day=100,
        raw_rate_limit_per_minute=4,
        raw_rate_limit_per_day=100,
        _env_file=None,
    )
    app = FastAPI()
    app.add_middleware(AuthRateLimitMiddleware, settings=settings)

    @app.get("/v1/stats")
    def stats():
        return {"ok": True}

    @app.get("/v1/raw/{sha256}")
    def raw(sha256: str):
        return {"sha256": sha256}

    client = TestClient(app)
    assert client.get("/v1/stats").status_code == 200
    assert client.get("/v1/stats").status_code == 200
    blocked = client.get("/v1/stats")
    assert blocked.status_code == 429
    assert "per minute" in blocked.json()["detail"]

    digest = "ab" * 32
    for _ in range(4):
        assert client.get(f"/v1/raw/{digest}").status_code == 200
    raw_blocked = client.get(f"/v1/raw/{digest}")
    assert raw_blocked.status_code == 429
    # Search budget stays exhausted; raw exhaustion did not reset it.
    assert client.get("/v1/stats").status_code == 429
    _MEMORY.clear()
