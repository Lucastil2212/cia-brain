"""Bounded polling of documented public APIs, with durable change detection."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from .bus import connect, publish_json
from .models import FetchedEvent
from .settings import get_settings
from .sources import selected_sources
from .storage import StreamingCASWriter, disk_free_gb, utcnow, write_manifest

log = logging.getLogger(__name__)
RECORD_MIME = "application/vnd.cia-brain.record+json"
XML_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC).isoformat()
    return value


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, names: set[str]) -> str:
    for child in node:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def _parse_rss_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC).isoformat()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except Exception:
        return value


def parse_rss(body: bytes) -> list[dict]:
    """Parse RSS 2.0 / Atom feeds into structured records."""
    root = ET.fromstring(body)
    records = []
    # Atom
    for entry in root.findall("atom:entry", XML_NS) or root.findall(
        "{http://www.w3.org/2005/Atom}entry"
    ):
        identity = (
            _child_text(entry, {"id"})
            or next(
                (
                    link.attrib.get("href", "")
                    for link in entry
                    if _local(link.tag) == "link" and link.attrib.get("rel", "alternate") in {
                        "alternate",
                        "",
                    }
                ),
                "",
            )
            or _child_text(entry, {"title"})
        )
        if not identity:
            continue
        link = next(
            (l.attrib.get("href") for l in entry if _local(l.tag) == "link" and l.attrib.get("href")),
            identity,
        )
        summary = _child_text(entry, {"summary", "content", "title"})
        records.append(
            {
                "record_id": identity[:500],
                "title": _child_text(entry, {"title"}) or identity,
                "published_at": _parse_rss_date(
                    _child_text(entry, {"updated", "published", "date"})
                ),
                "text": summary,
                "properties": {"link": link, "format": "atom"},
            }
        )
    if records:
        return records
    # RSS 2.0
    channel = root.find("channel")
    items = list(channel.findall("item")) if channel is not None else root.findall(".//item")
    for item in items:
        identity = (
            _child_text(item, {"guid", "id"})
            or _child_text(item, {"link"})
            or _child_text(item, {"title"})
        )
        if not identity:
            continue
        text = _child_text(item, {"description", "summary", "content", "encoded"}) or _child_text(
            item, {"title"}
        )
        records.append(
            {
                "record_id": identity[:500],
                "title": _child_text(item, {"title"}) or identity,
                "published_at": _parse_rss_date(_child_text(item, {"pubDate", "date", "updated"})),
                "text": text,
                "properties": {
                    "link": _child_text(item, {"link"}) or identity,
                    "format": "rss",
                },
            }
        )
    return records


def parse_nara_catalog(data: dict) -> list[dict]:
    """NARA Catalog API v1 result descriptions."""
    results = (((data.get("opaResponse") or {}).get("results") or {}).get("result")) or []
    if isinstance(results, dict):
        results = [results]
    records = []
    for item in results:
        identity = str(item.get("naId") or item.get("id") or "")
        if not identity:
            continue
        title = item.get("title") or item.get("heading") or f"NARA {identity}"
        desc = item.get("description") or item.get("scopeContent") or ""
        if isinstance(desc, dict):
            desc = desc.get("p") or json.dumps(desc, ensure_ascii=False)
        locales = item.get("geographicReferenceArray") or item.get("geographicReferences") or []
        geometry = None
        if isinstance(locales, list) and locales:
            # Preserve place names; coordinates are uncommon in catalog search hits.
            text_extra = "; ".join(
                str(x.get("termName") or x) for x in locales[:20] if x
            )
        else:
            text_extra = ""
        records.append(
            {
                "record_id": identity,
                "title": title if isinstance(title, str) else str(title),
                "published_at": item.get("productionDate") or item.get("date"),
                "text": "\n".join(
                    p for p in (title if isinstance(title, str) else str(title), str(desc), text_extra) if p
                ),
                "geometry": geometry,
                "properties": {
                    "naId": identity,
                    "levelOfDescription": item.get("levelOfDescription"),
                    "localIdentifier": item.get("localIdentifier"),
                    "catalog_url": f"https://catalog.archives.gov/id/{identity}",
                },
            }
        )
    return records


def parse_records(source, data):
    """Keep upstream IDs, dates, geometries, measurements and media references intact."""
    if source.kind == "geojson":
        for item in data["features"]:
            p = item.get("properties") or {}
            identity = str(item.get("id") or p.get("id") or "")
            if not identity:
                continue
            yield {
                "record_id": identity,
                "title": p.get("title") or p.get("headline") or p.get("event") or identity,
                "published_at": timestamp(p.get("time") or p.get("sent")),
                "text": json.dumps(p, ensure_ascii=False),
                "geometry": item.get("geometry"),
                "properties": p,
            }
    elif source.kind == "timeseries":
        if not data:
            return
        columns = data[0]
        if "time_tag" not in columns:
            raise ValueError("Telemetry schema missing time_tag")
        for row in data[1:]:
            p = dict(zip(columns, row, strict=True))
            yield {
                "record_id": p["time_tag"],
                "title": "NOAA propagated solar wind",
                "published_at": p["time_tag"],
                "text": json.dumps(p),
                "measurements": p,
            }
    elif source.kind == "media":
        for item in data["collection"]["items"]:
            for p in item.get("data", []):
                if not p.get("nasa_id"):
                    continue
                yield {
                    "record_id": p["nasa_id"],
                    "title": p.get("title", "NASA media"),
                    "published_at": p.get("date_created"),
                    "text": p.get("description", "") + "\n" + ", ".join(p.get("keywords", [])),
                    "media": {
                        "type": p.get("media_type"),
                        "assets": item.get("href"),
                        "links": item.get("links", []),
                    },
                    "properties": p,
                }
    elif source.kind == "catalog":
        yield from parse_nara_catalog(data)
    elif source.kind == "rss":
        # Called with already-parsed list from parse_rss.
        yield from data


def iter_feed_records(source, body: bytes):
    if source.kind == "rss":
        return parse_rss(body)
    data = json.loads(body)
    return list(parse_records(source, data))


def archive_bytes(settings, url, mime, body, fetched_at):
    writer = StreamingCASWriter(settings, url, mime)
    try:
        writer.write(body)
        digest, path = writer.commit()
    except Exception:
        writer.abort()
        raise
    event = FetchedEvent(url, url, digest, str(path), mime, len(body), 200, fetched_at, 0)
    write_manifest(settings, digest, event.to_dict())
    return event


class FeedPoller:
    def __init__(self, settings, js):
        self.s, self.js = settings, js
        self.db = sqlite3.connect(settings.state_dir / "feeds.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS feeds(
                id TEXT PRIMARY KEY, etag TEXT, modified TEXT, next_poll REAL DEFAULT 0,
                checked_at TEXT, last_success TEXT, error TEXT, failures INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS records(
                source TEXT, id TEXT, sha TEXT, PRIMARY KEY(source,id));
        """)
        self.db.commit()

    async def poll(self, client, source):
        self.db.execute("INSERT OR IGNORE INTO feeds(id) VALUES(?)", (source.id,))
        self.db.commit()
        state = self.db.execute("SELECT * FROM feeds WHERE id=?", (source.id,)).fetchone()
        if state["next_poll"] > time.time():
            return
        headers = {}
        if state["etag"]:
            headers["If-None-Match"] = state["etag"]
        if state["modified"]:
            headers["If-Modified-Since"] = state["modified"]
        now = utcnow()
        retry_after = 0
        try:
            if disk_free_gb(self.s.data_dir) < self.s.min_free_disk_gb:
                raise RuntimeError("Low disk space")
            url = source.url.replace("year_start=2026", f"year_start={datetime.now(UTC).year}")
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 304:
                    self.db.execute(
                        "UPDATE feeds SET next_poll=?,checked_at=?,last_success=?,error=NULL,failures=0 WHERE id=?",
                        (time.time() + source.interval, now, now, source.id),
                    )
                    self.db.commit()
                    return
                if response.status_code == 429:
                    try:
                        retry_after = min(86400, int(response.headers.get("Retry-After", "0")))
                    except ValueError:
                        pass
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.s.feed_max_bytes:
                        raise ValueError("Feed exceeds FEED_MAX_BYTES")
                mime = response.headers.get("content-type", "application/json").split(";")[0]
                snapshot = archive_bytes(self.s, url, mime, bytes(body), now)
                if source.kind == "image":
                    old = self.db.execute(
                        "SELECT sha FROM records WHERE source=? AND id='image'", (source.id,)
                    ).fetchone()
                    if not old or old["sha"] != snapshot.sha256:
                        await publish_json(
                            self.js,
                            "cia.fetched",
                            snapshot.to_dict()
                            | {
                                "source_id": source.id,
                                "agency": source.agency,
                                "modalities": source.modalities,
                                "record_id": "image",
                                "title": "NOAA GOES-19 CONUS GeoColor satellite imagery",
                            },
                        )
                        self.db.execute(
                            "INSERT OR REPLACE INTO records VALUES(?,'image',?)",
                            (source.id, snapshot.sha256),
                        )
                        self.db.commit()
                else:
                    for record in iter_feed_records(source, bytes(body)):
                        record.update(
                            source_id=source.id, agency=source.agency, modalities=source.modalities
                        )
                        record_url = (
                            url.split("?")[0] + "#record=" + quote(record["record_id"], safe="")
                        )
                        event = archive_bytes(
                            self.s,
                            record_url,
                            RECORD_MIME,
                            json.dumps(record, sort_keys=True, ensure_ascii=False).encode(),
                            now,
                        )
                        old = self.db.execute(
                            "SELECT sha FROM records WHERE source=? AND id=?",
                            (source.id, record["record_id"]),
                        ).fetchone()
                        if old and old["sha"] == event.sha256:
                            continue
                        # Advance only after JetStream acknowledges; a crash may replay safely.
                        await publish_json(
                            self.js,
                            "cia.fetched",
                            event.to_dict() | {"snapshot_sha256": snapshot.sha256},
                        )
                        self.db.execute(
                            "INSERT OR REPLACE INTO records VALUES(?,?,?)",
                            (source.id, record["record_id"], event.sha256),
                        )
                        self.db.commit()
                self.db.execute(
                    "UPDATE feeds SET etag=?,modified=?,next_poll=?,checked_at=?,last_success=?,error=NULL,failures=0 WHERE id=?",
                    (
                        response.headers.get("etag"),
                        response.headers.get("last-modified"),
                        time.time() + source.interval,
                        now,
                        now,
                        source.id,
                    ),
                )
                self.db.commit()
        except Exception as exc:
            failures = state["failures"] + 1
            delay = max(source.interval, retry_after, min(3600, 30 * 2 ** min(failures, 7)))
            self.db.execute(
                "UPDATE feeds SET next_poll=?,checked_at=?,error=?,failures=? WHERE id=?",
                (time.time() + delay, now, str(exc)[:1000], failures, source.id),
            )
            self.db.commit()
            log.exception("Feed %s failed", source.id)


async def main_async():
    settings = get_settings()
    sources = selected_sources(settings.live_sources)
    nc, js = await connect(settings)
    poller = FeedPoller(settings, js)
    user_agent = settings.crawler_user_agent + (
        f" ({settings.crawler_contact})" if settings.crawler_contact else ""
    )
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": user_agent},
        ) as client:
            while True:
                for source in sources:
                    await poller.poll(client, source)
                await asyncio.sleep(5)
    finally:
        poller.db.close()
        await nc.drain()


def main():
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
