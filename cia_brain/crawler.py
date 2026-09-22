from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from bs4 import BeautifulSoup

from .bus import connect, publish_json
from .models import FetchedEvent
from .settings import Settings
from .sources import archive_url_allowed
from .storage import StreamingCASWriter, disk_free_gb, utcnow, write_manifest

log = logging.getLogger(__name__)
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return ""
    query = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k.lower().startswith("utm_") or k.lower() in TRACKING_KEYS:
            continue
        query.append((k, v))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def is_allowed_host(url: str, hosts: set[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in hosts


def extract_links(html: bytes, base_url: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for tag, attr in (("a", "href"), ("img", "src"), ("link", "href"), ("source", "src"), ("video", "src"), ("audio", "src"), ("iframe", "src")):
        for node in soup.find_all(tag):
            raw = node.get(attr)
            if not raw:
                continue
            c = canonicalize_url(urljoin(base_url, raw.strip()))
            if c:
                links.add(c)
    for node in soup.find_all(srcset=True):
        for candidate in node.get("srcset", "").split(","):
            raw = candidate.strip().split(" ")[0]
            c = canonicalize_url(urljoin(base_url, raw))
            if c:
                links.add(c)
    return links


class Frontier:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS frontier (
              url TEXT PRIMARY KEY,
              depth INTEGER NOT NULL,
              discovered_from TEXT,
              status TEXT NOT NULL DEFAULT 'queued',
              tries INTEGER NOT NULL DEFAULT 0,
              next_attempt REAL NOT NULL DEFAULT 0,
              last_error TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_frontier_next ON frontier(status, next_attempt, depth);
            CREATE TABLE IF NOT EXISTS downloads (
              url TEXT PRIMARY KEY,
              final_url TEXT,
              sha256 TEXT,
              path TEXT,
              mime TEXT,
              size INTEGER,
              status_code INTEGER,
              etag TEXT,
              last_modified TEXT,
              fetched_at TEXT,
              depth INTEGER
            );
            """
        )
        self.db.commit()

    def add(self, url: str, depth: int, discovered_from: str | None = None) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO frontier(url,depth,discovered_from,status,updated_at)
               VALUES(?,?,?,?,?)""",
            (url, depth, discovered_from, "queued", utcnow()),
        )
        self.db.commit()

    def next(self):
        now = time.time()
        row = self.db.execute(
            "SELECT * FROM frontier WHERE status='queued' AND next_attempt<=? ORDER BY depth, rowid LIMIT 1",
            (now,),
        ).fetchone()
        return row

    def mark_done(self, url: str) -> None:
        self.db.execute("UPDATE frontier SET status='done',updated_at=? WHERE url=?", (utcnow(), url))
        self.db.commit()

    def mark_skip(self, url: str, reason: str) -> None:
        self.db.execute(
            "UPDATE frontier SET status='skipped',last_error=?,updated_at=? WHERE url=?",
            (reason, utcnow(), url),
        )
        self.db.commit()

    def retry(self, url: str, error: str, tries: int) -> None:
        delay = min(30 * (2 ** max(0, tries - 1)), 3600)
        self.db.execute(
            "UPDATE frontier SET tries=?,next_attempt=?,last_error=?,updated_at=? WHERE url=?",
            (tries, time.time() + delay, error[:1000], utcnow(), url),
        )
        self.db.commit()

    def save_download(self, event: FetchedEvent, headers: dict[str, str]) -> None:
        self.db.execute(
            """
            INSERT INTO downloads(url,final_url,sha256,path,mime,size,status_code,etag,last_modified,fetched_at,depth)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
              final_url=excluded.final_url,sha256=excluded.sha256,path=excluded.path,mime=excluded.mime,
              size=excluded.size,status_code=excluded.status_code,etag=excluded.etag,
              last_modified=excluded.last_modified,fetched_at=excluded.fetched_at,depth=excluded.depth
            """,
            (
                event.url,
                event.final_url,
                event.sha256,
                event.path,
                event.mime,
                event.size,
                event.status_code,
                headers.get("ETag"),
                headers.get("Last-Modified"),
                event.fetched_at,
                event.depth,
            ),
        )
        self.db.commit()

    def headers_for(self, url: str, recrawl_hours: int) -> tuple[dict[str, str], bool]:
        row = self.db.execute("SELECT * FROM downloads WHERE url=?", (url,)).fetchone()
        if not row:
            return {}, False
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
            fresh = datetime.now(timezone.utc) - fetched < timedelta(hours=recrawl_hours)
        except Exception:
            fresh = False
        if fresh:
            return {}, True
        headers = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers, False

    def requeue_stale(self, recrawl_hours: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=recrawl_hours)).isoformat()
        cur = self.db.execute(
            """UPDATE frontier SET status='queued',next_attempt=0,updated_at=?
               WHERE status='done' AND url IN (
                 SELECT url FROM downloads WHERE fetched_at IS NOT NULL AND fetched_at < ?
               )""",
            (utcnow(), cutoff),
        )
        self.db.commit()
        return int(cur.rowcount)

    def stats(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.db.execute("SELECT status,COUNT(*) FROM frontier GROUP BY status")}


class RobotsRules:
    def __init__(self, text: str, user_agent: str):
        self.user_agent = user_agent.lower()
        self.sitemaps: set[str] = set()
        groups: list[dict] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        delay: float | None = None
        directives_started = False

        def flush() -> None:
            nonlocal agents, rules, delay, directives_started
            if agents:
                groups.append({"agents": agents, "rules": rules, "delay": delay})
            agents, rules, delay, directives_started = [], [], None, False

        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = [x.strip() for x in line.split(":", 1)]
            key = key.lower()
            if key == "sitemap":
                if value:
                    self.sitemaps.add(value)
                continue
            if key == "user-agent":
                if directives_started:
                    flush()
                agents.append(value.lower())
                continue
            if not agents:
                continue
            directives_started = True
            if key == "allow":
                rules.append((True, value))
            elif key == "disallow":
                # Empty Disallow means allow all and should not create a rule.
                if value:
                    rules.append((False, value))
            elif key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    pass
        flush()

        matches: list[tuple[int, dict]] = []
        for group in groups:
            scores = []
            for token in group["agents"]:
                if token == "*":
                    scores.append(0)
                elif token and token in self.user_agent:
                    scores.append(len(token))
            if scores:
                matches.append((max(scores), group))
        best = max((score for score, _ in matches), default=-1)
        selected = [g for score, g in matches if score == best]
        self.rules = [rule for g in selected for rule in g["rules"]]
        delays = [g["delay"] for g in selected if g["delay"] is not None]
        self.crawl_delay = max(delays) if delays else None

    @staticmethod
    def _matches(pattern: str, target: str) -> bool:
        # RFC-style prefix rules plus the wildcard/end-anchor syntax used by major crawlers.
        anchored = pattern.endswith("$")
        if anchored:
            pattern = pattern[:-1]
        regex = "^" + re.escape(pattern).replace(r"\*", ".*")
        if anchored:
            regex += "$"
        return re.search(regex, target) is not None

    def can_fetch(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        matched: list[tuple[int, bool]] = []
        for allow, pattern in self.rules:
            if self._matches(pattern, target):
                # Longest matching rule wins; Allow wins ties.
                specificity = len(pattern.replace("*", ""))
                matched.append((specificity, allow))
        if not matched:
            return True
        best_len = max(x[0] for x in matched)
        return any(allow for n, allow in matched if n == best_len)


class RobotsPolicy:
    def __init__(self, session: aiohttp.ClientSession, settings: Settings):
        self.session = session
        self.settings = settings
        self.parsers: dict[str, RobotsRules] = {}
        self.sitemaps: set[str] = set()
        self.delay_by_host: dict[str, float] = {}

    async def load(self, host: str) -> None:
        if host in self.parsers:
            return
        url = f"https://{host}/robots.txt"
        try:
            async with checked_get(self.session, url, self.settings) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                text = await resp.text(errors="replace")
            parser = RobotsRules(text, self.settings.crawler_user_agent)
        except Exception as exc:
            log.warning("robots fetch failed for %s: %s; defaulting to deny", host, exc)
            parser = RobotsRules("User-agent: *\nDisallow: /", self.settings.crawler_user_agent)
        self.parsers[host] = parser
        self.sitemaps.update(parser.sitemaps)
        self.delay_by_host[host] = max(
            float(parser.crawl_delay or 0), self.settings.crawl_delay_seconds
        )

    async def can_fetch(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        await self.load(host)
        return self.parsers[host].can_fetch(url)

    def delay(self, url: str) -> float:
        host = (urlsplit(url).hostname or "").lower()
        return self.delay_by_host.get(host, self.settings.crawl_delay_seconds)


class HostThrottle:
    def __init__(self):
        self.last: dict[str, float] = {}
        self.lock = asyncio.Lock()

    async def wait(self, url: str, delay: float) -> None:
        host = (urlsplit(url).hostname or "").lower()
        async with self.lock:
            elapsed = time.monotonic() - self.last.get(host, 0.0)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)
            self.last[host] = time.monotonic()


@asynccontextmanager
async def checked_get(session, url, settings, robots=None, throttle=None, **kwargs):
    """Authorize every redirect before sending a request to the next host."""
    for hop in range(6):
        if not is_allowed_host(url, settings.allowed_host_set):
            raise ValueError("URL host is outside configured sources")
        if robots and not await robots.can_fetch(url):
            raise ValueError("robots_disallow")
        if hop and throttle:
            await throttle.wait(url, robots.delay(url))
        async with session.get(url, allow_redirects=False, **kwargs) as response:
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Redirect has no Location")
                url = canonicalize_url(urljoin(url, location))
                continue
            yield response
            return
    raise ValueError("Too many redirects")


class Crawler:
    def __init__(self, settings: Settings):
        self.s = settings
        self.frontier = Frontier(settings.state_dir / "crawl.sqlite")
        self.throttle = HostThrottle()
        self.nc = None
        self.js = None

    @property
    def user_agent(self) -> str:
        suffix = f" ({self.s.crawler_contact})" if self.s.crawler_contact else ""
        return self.s.crawler_user_agent + suffix

    async def seed_sitemaps(self, session: aiohttp.ClientSession, robots: RobotsPolicy) -> None:
        for host in {x.strip() for x in self.s.allowed_hosts.split(",") if x.strip()}:
            await robots.load(host)
        for seed in self.s.seed_url_list:
            self.frontier.add(canonicalize_url(seed), 0, "seed")
        for sitemap in sorted(robots.sitemaps):
            if not is_allowed_host(sitemap, self.s.allowed_host_set):
                continue
            await self._load_sitemap(session, robots, sitemap, 0, set())

    async def _load_sitemap(self, session, robots, url: str, level: int, seen: set[str]) -> None:
        if level > 5 or url in seen or not is_allowed_host(url, self.s.allowed_host_set):
            return
        seen.add(url)
        if not await robots.can_fetch(url):
            return
        await self.throttle.wait(url, robots.delay(url))
        try:
            async with checked_get(session, url, self.s, robots, self.throttle) as resp:
                body = await resp.read()
                ctype = resp.headers.get("Content-Type", "")
            if b"<urlset" not in body[:5000] and b"<sitemapindex" not in body[:5000] and "xml" not in ctype:
                return
            root = ET.fromstring(body)
            ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
            if root.tag.endswith("sitemapindex"):
                for loc in root.findall(f".//{ns}loc"):
                    if loc.text:
                        await self._load_sitemap(session, robots, loc.text.strip(), level + 1, seen)
            else:
                for loc in root.findall(f".//{ns}loc"):
                    if loc.text:
                        u = canonicalize_url(loc.text.strip())
                        if u and is_allowed_host(u, self.s.allowed_host_set):
                            self.frontier.add(u, 0, url)
        except Exception as exc:
            log.warning("sitemap failed %s: %s", url, exc)

    async def run(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.s.request_timeout_seconds)
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            robots = RobotsPolicy(session, self.s)
            self.nc, self.js = await connect(self.s)
            await self.seed_sitemaps(session, robots)
            log.info("crawl started: %s", self.frontier.stats())
            while True:
                row = self.frontier.next()
                if row is None:
                    requeued = self.frontier.requeue_stale(self.s.recrawl_after_hours)
                    if requeued:
                        log.info("requeued %s stale URLs", requeued)
                        continue
                    log.info("frontier empty: %s", self.frontier.stats())
                    await asyncio.sleep(60)
                    continue
                await self._process(session, robots, row)

    async def _process(self, session, robots: RobotsPolicy, row: sqlite3.Row) -> None:
        url = row["url"]
        depth = int(row["depth"])
        if depth > self.s.max_depth:
            self.frontier.mark_skip(url, "max_depth")
            return
        if not is_allowed_host(url, self.s.allowed_host_set) or not archive_url_allowed(url, self.s):
            self.frontier.mark_skip(url, "outside_source_scope")
            return
        if not await robots.can_fetch(url):
            self.frontier.mark_skip(url, "robots_disallow")
            return
        if disk_free_gb(self.s.data_dir) < self.s.min_free_disk_gb:
            log.error("low disk space; pausing crawler")
            await asyncio.sleep(60)
            return

        cond_headers, fresh = self.frontier.headers_for(url, self.s.recrawl_after_hours)
        if fresh:
            self.frontier.mark_done(url)
            return

        await self.throttle.wait(url, robots.delay(url))
        tries = int(row["tries"]) + 1
        writer = None
        try:
            async with checked_get(session, url, self.s, robots, self.throttle, headers=cond_headers) as resp:
                if resp.status == 304:
                    self.frontier.mark_done(url)
                    return
                if resp.status >= 400:
                    if tries < self.s.max_retries and resp.status in {408, 425, 429, 500, 502, 503, 504}:
                        self.frontier.retry(url, f"HTTP {resp.status}", tries)
                    else:
                        self.frontier.mark_skip(url, f"HTTP {resp.status}")
                    return
                final_url = canonicalize_url(str(resp.url))
                if not is_allowed_host(final_url, self.s.allowed_host_set):
                    self.frontier.mark_skip(url, "redirect_external")
                    return
                mime = resp.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].lower()
                writer = StreamingCASWriter(self.s, final_url, mime)
                html_buf = bytearray() if mime in {"text/html", "application/xhtml+xml"} else None
                async for chunk in resp.content.iter_chunked(1024 * 256):
                    if self.s.max_file_bytes and writer.size + len(chunk) > self.s.max_file_bytes:
                        writer.abort()
                        self.frontier.mark_skip(url, "max_file_bytes")
                        return
                    writer.write(chunk)
                    if html_buf is not None:
                        html_buf.extend(chunk)
                digest, path = writer.commit()
                event = FetchedEvent(
                    url=url,
                    final_url=final_url,
                    sha256=digest,
                    path=str(path),
                    mime=mime,
                    size=writer.size,
                    status_code=resp.status,
                    fetched_at=utcnow(),
                    depth=depth,
                )
                manifest = event.to_dict() | {"headers": {k: v for k, v in resp.headers.items()}}
                write_manifest(self.s, digest, manifest)
                self.frontier.save_download(event, dict(resp.headers))
                self.frontier.mark_done(url)
                await publish_json(self.js, "cia.fetched", event.to_dict())
                log.info("fetched %s bytes %s", event.size, url, extra={"url": url, "sha256": digest})

                if html_buf is not None and depth < self.s.max_depth:
                    for link in extract_links(bytes(html_buf), final_url):
                        if is_allowed_host(link, self.s.allowed_host_set) and archive_url_allowed(link, self.s):
                            self.frontier.add(link, depth + 1, final_url)
        except Exception as exc:
            if writer is not None:
                try:
                    writer.abort()
                except Exception:
                    pass
            if tries < self.s.max_retries:
                self.frontier.retry(url, repr(exc), tries)
            else:
                self.frontier.mark_skip(url, repr(exc))
            log.warning("fetch failed %s: %s", url, exc)
