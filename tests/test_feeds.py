import asyncio
import gzip
import json

import httpx

from cia_brain.extract import normalize_document
from cia_brain.feeds import FeedPoller, parse_records
from cia_brain.settings import Settings
from cia_brain.sources import SOURCES, archive_url_allowed, selected_sources

USGS = next(s for s in SOURCES if s.id == "usgs")
DATA = {
    "features": [
        {
            "id": "quake1",
            "properties": {"title": "USGS earthquake", "time": 1000},
            "geometry": {"type": "Point", "coordinates": [-70, 40, 5]},
        }
    ]
}


class Bus:
    def __init__(self):
        self.events = []
        self.fail = False

    async def publish(self, subject, body):
        if self.fail:
            raise RuntimeError("bus unavailable")
        self.events.append(json.loads(body))


def make_poller(tmp_path):
    settings = Settings(data_dir=tmp_path, min_free_disk_gb=0, _env_file=None)
    settings.ensure_dirs()
    bus = Bus()
    return settings, bus, FeedPoller(settings, bus)


def force_due(poller):
    poller.db.execute("UPDATE feeds SET next_poll=0")
    poller.db.commit()


def test_poll_dedup_and_normalize(tmp_path):
    settings, bus, poller = make_poller(tmp_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=DATA))
        ) as client:
            await poller.poll(client, USGS)
            force_due(poller)
            await poller.poll(client, USGS)

    asyncio.run(run())
    assert len(bus.events) == 1
    event = normalize_document(bus.events[0], settings)
    with gzip.open(event.normalized_path, "rt") as stream:
        doc = json.load(stream)
    assert doc["geometry"]["coordinates"] == [-70, 40, 5]
    assert doc["published_at"].startswith("1970-01-01T00:00:01")
    assert len(doc["snapshot_sha256"]) == 64
    assert (settings.state_dir / "graph.sqlite").exists()
    poller.db.close()


def test_publish_failure_retries_record(tmp_path):
    _, bus, poller = make_poller(tmp_path)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=DATA))
        ) as client:
            bus.fail = True
            await poller.poll(client, USGS)
            assert poller.db.execute("SELECT count(*) FROM records").fetchone()[0] == 0
            bus.fail = False
            force_due(poller)
            await poller.poll(client, USGS)

    asyncio.run(run())
    assert len(bus.events) == 1
    poller.db.close()


def test_conditional_poll_and_redirect_rejection(tmp_path):
    _, bus, poller = make_poller(tmp_path)
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=DATA, headers={"etag": '"v1"'})
        if len(calls) == 2:
            assert request.headers["if-none-match"] == '"v1"'
            return httpx.Response(304)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=False
        ) as client:
            for _ in range(3):
                force_due(poller)
                await poller.poll(client, USGS)

    asyncio.run(run())
    assert len(calls) == 3 and len(bus.events) == 1
    assert poller.db.execute("SELECT error FROM feeds").fetchone()[0]
    poller.db.close()


def test_other_modalities():
    swpc = next(s for s in SOURCES if s.id == "swpc")
    assert (
        next(iter(parse_records(swpc, [["time_tag", "speed"], ["2026-09-21 10:00", "400"]])))[
            "measurements"
        ]["speed"]
        == "400"
    )
    nasa = next(s for s in SOURCES if s.id == "nasa")
    record = next(
        iter(
            parse_records(
                nasa,
                {
                    "collection": {
                        "items": [
                            {
                                "data": [{"nasa_id": "v1", "media_type": "video"}],
                                "href": "https://images-assets.nasa.gov/a",
                            }
                        ]
                    }
                },
            )
        )
    )
    assert record["media"]["type"] == "video"


def test_rss_and_nara_catalog_parsers():
    from cia_brain.feeds import parse_nara_catalog, parse_rss

    rss = parse_rss(
        b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Alert</title><guid>g1</guid><link>https://example.gov/a</link>
        <description>Flood watch</description><pubDate>Mon, 21 Sep 2026 12:00:00 GMT</pubDate></item>
        </channel></rss>"""
    )
    assert rss[0]["record_id"] == "g1" and "Flood" in rss[0]["text"]
    atom = parse_rss(
        b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
        <entry><id>urn:1</id><title>Brief</title><updated>2026-09-21T12:00:00Z</updated>
        <summary>ODNI update</summary><link href="https://example.gov/b"/></entry></feed>"""
    )
    assert atom[0]["record_id"] == "urn:1"
    catalog = parse_nara_catalog(
        {
            "opaResponse": {
                "results": {
                    "result": [{"naId": "123", "title": "Declassified memo", "description": "text"}]
                }
            }
        }
    )
    assert catalog[0]["record_id"] == "123"
    assert "catalog.archives.gov/id/123" in catalog[0]["properties"]["catalog_url"]


def test_source_scope():
    settings = Settings(_env_file=None)
    assert "vault.fbi.gov" in settings.allowed_host_set
    assert "crsreports.congress.gov" in settings.allowed_host_set
    assert "www.dni.gov" in settings.allowed_host_set
    assert archive_url_allowed("https://history.state.gov/historicaldocuments/test", settings)
    assert not archive_url_allowed("https://history.state.gov/other", settings)
    assert archive_url_allowed("https://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/doc.pdf", settings)
    assert archive_url_allowed("https://crsreports.congress.gov/product/pdf/R/R12345", settings)
    assert selected_sources("") == []
    assert {s.id for s in selected_sources("state_rss,nara_catalog")} == {"state_rss", "nara_catalog"}
