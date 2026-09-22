from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .crawler import Frontier, canonicalize_url, is_allowed_host
from .indexer import SearchStore
from .settings import Settings

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobResult:
    job: str
    ok: bool
    detail: dict = field(default_factory=dict)
    error: str | None = None
    ran_at: str = field(default_factory=utcnow)


def job_recrawl_stale(settings: Settings) -> JobResult:
    """Re-queue downloads older than RECRAWL_AFTER_HOURS for conditional refetch."""
    path = settings.state_dir / "crawl.sqlite"
    if not path.exists():
        return JobResult("recrawl_stale", True, {"requeued": 0, "note": "no crawl.sqlite yet"})
    frontier = Frontier(path)
    n = frontier.requeue_stale(settings.recrawl_after_hours)
    return JobResult("recrawl_stale", True, {"requeued": n, "after_hours": settings.recrawl_after_hours})


def job_parquet_compact(settings: Settings) -> JobResult:
    """Flush vectors if needed and rewrite Parquet projections from SQLite."""
    store = SearchStore(settings)
    store.flush()
    store.compact_parquet()
    docs = store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    chunks = store.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    return JobResult("parquet_compact", True, {"documents": docs, "chunks": chunks})


def job_frontier_seed(settings: Settings) -> JobResult:
    """Ensure seed URLs remain present in the crawl frontier."""
    path = settings.state_dir / "crawl.sqlite"
    frontier = Frontier(path)
    added = 0
    for url in settings.seed_url_list:
        c = canonicalize_url(url)
        if not c or not is_allowed_host(c, settings.allowed_host_set):
            continue
        before = frontier.db.execute("SELECT 1 FROM frontier WHERE url=?", (c,)).fetchone()
        frontier.add(c, 0, discovered_from="scheduler:seed")
        after = frontier.db.execute("SELECT 1 FROM frontier WHERE url=?", (c,)).fetchone()
        if before is None and after is not None:
            added += 1
    return JobResult("frontier_seed", True, {"seeded": added, "seeds": settings.seed_url_list})


def job_health_snapshot(settings: Settings) -> JobResult:
    """Capture lake + frontier counters for monitoring."""
    detail: dict = {"ran_at": utcnow()}
    search_path = settings.state_dir / "search.sqlite"
    if search_path.exists():
        db = sqlite3.connect(f"file:{search_path}?mode=ro", uri=True)
        detail["documents"] = db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        detail["chunks"] = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        db.close()
    crawl_path = settings.state_dir / "crawl.sqlite"
    if crawl_path.exists():
        db = sqlite3.connect(f"file:{crawl_path}?mode=ro", uri=True)
        detail["frontier"] = {
            r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM frontier GROUP BY status")
        }
        db.close()
    return JobResult("health_snapshot", True, detail)


def job_graph_rebuild(settings: Settings) -> JobResult:
    import gzip
    import json
    from .graph import KnowledgeGraph
    graph = KnowledgeGraph(settings)
    count = 0
    # Replay oldest first so current feed revisions remain active.
    paths = sorted(settings.normalized_dir.glob("*/*.json.gz"), key=lambda p: p.stat().st_mtime)
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            graph.ingest(json.load(stream))
        count += 1
    return JobResult("graph_rebuild", True, {"processed": count, **graph.stats()})


JOB_REGISTRY: dict[str, Callable[[Settings], JobResult]] = {
    "graph_rebuild": job_graph_rebuild,
    "recrawl_stale": job_recrawl_stale,
    "parquet_compact": job_parquet_compact,
    "frontier_seed": job_frontier_seed,
    "health_snapshot": job_health_snapshot,
}


def run_job(name: str, settings: Settings) -> JobResult:
    fn = JOB_REGISTRY.get(name)
    if fn is None:
        return JobResult(name, False, error=f"unknown job: {name}")
    started = time.perf_counter()
    try:
        result = fn(settings)
        result.detail["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result
    except Exception as exc:
        log.exception("job %s failed", name)
        return JobResult(name, False, error=str(exc), detail={"duration_ms": round((time.perf_counter() - started) * 1000, 1)})
