from __future__ import annotations

import gzip
import html
import json
import logging
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth_routes import require_admin, router as auth_router
from .db import DatabaseUnavailable, database_configured, database_problem, disable_database
from .history import citation_for, observations_for, text_diff, version_chain, version_url
from .log import configure_logging
from .middleware import AuthRateLimitMiddleware
from .paths import require_sha256
from .settings import get_settings, insecure_settings_reason

s = get_settings()
configure_logging(s.log_level)
log = logging.getLogger(__name__)

# Refuse account features rather than crash the whole app (and every static asset with it).
_insecure = insecure_settings_reason(s)
if _insecure:
    log.error("%s; account features are disabled until this is fixed", _insecure)
    disable_database(_insecure)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if database_configured(s) and database_problem() is None:
        from .auth import migrate

        try:
            migrate(s)
            log.info("account schema migrated")
        except DatabaseUnavailable as exc:
            log.warning("account schema migration deferred: %s", exc)
    yield


app = FastAPI(
    title="CIA Brain Public Archive Research Catalog",
    description=(
        "Local library-style discovery over public declassified and historical "
        "U.S. intelligence collections, and a source-grounded knowledge graph. "
        "Programmatic access uses X-API-Key or Bearer JWT with per-key rate limits."
    ),
    version="0.5.0",
    lifespan=lifespan,
)
_cors = s.cors_origin_list
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )
app.add_middleware(AuthRateLimitMiddleware, settings=s)
app.include_router(auth_router)
_searcher = None
_graph = None
_store_lock = threading.Lock()


@app.exception_handler(DatabaseUnavailable)
async def _database_unavailable(_request: Request, exc: DatabaseUnavailable):
    return JSONResponse({"detail": str(exc) or "Account database unavailable"}, status_code=503)


@app.exception_handler(sqlite3.Error)
async def _store_error(request: Request, exc: sqlite3.Error):
    log.exception("local store error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Catalog store unavailable"}, status_code=503)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=12, ge=1, le=100)
    mime: str | None = Field(default=None, max_length=120)
    since: str | None = Field(default=None, max_length=64)
    until: str | None = Field(default=None, max_length=64)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    source_id: str | None = Field(default=None, max_length=64)


class TriggerJobRequest(BaseModel):
    job: str = Field(min_length=1, max_length=64)


def searcher():
    global _searcher
    if _searcher is None:
        with _store_lock:
            if _searcher is None:
                from .search import HybridSearcher

                _searcher = HybridSearcher(s)
    return _searcher


def _pipeline():
    try:
        from .jobs import run_job
        from .scheduler import public_status, write_job_result
    except ImportError as exc:
        raise HTTPException(
            503, "Pipeline jobs are unavailable until the pipeline extra is installed"
        ) from exc
    return run_job, public_status, write_job_result


def _parse_sha256(value: str) -> str:
    try:
        return require_sha256(value)
    except ValueError as exc:
        raise HTTPException(400, "invalid sha256") from exc


@app.get("/v1/sources")
def sources():
    from .sources import catalog

    items = catalog(s)
    counts = _collection_counts()
    for item in items:
        item["document_count"] = counts.get(item["id"], 0)
    path = s.state_dir / "crawl.sqlite"
    if path.exists():
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                for item in items:
                    row = db.execute(
                        "SELECT status, last_error, updated_at FROM frontier WHERE url=?",
                        (item["url"],),
                    ).fetchone()
                    item["status"] = dict(row) if row else None
        except sqlite3.Error as exc:
            log.warning("collection status unavailable: %s", exc)
    return {"sources": items}


def _collection_counts() -> dict[str, int]:
    from .sources import SOURCES, collection_filter_sql

    path = s.state_dir / "search.sqlite"
    if not path.exists():
        return {}
    counts: dict[str, int] = {}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
        if "source_id" not in cols:
            db.close()
            return {}
        for source in SOURCES:
            match, match_params = collection_filter_sql(source.id)
            row = db.execute(
                f"""SELECT COUNT(*) FROM (
                      SELECT id,
                             ROW_NUMBER() OVER (
                               PARTITION BY COALESCE(NULLIF(requested_url, ''), source_url)
                               ORDER BY (fetched_at IS NULL), fetched_at DESC, id DESC
                             ) AS rn
                      FROM documents
                      WHERE {match}
                    ) WHERE rn = 1""",
                match_params,
            ).fetchone()
            counts[source.id] = int(row[0] if row else 0)
        db.close()
    except sqlite3.Error as exc:
        log.warning("collection counts unavailable: %s", exc)
        return {}
    return counts


def graph():
    global _graph
    if not s.graph_enabled:
        raise HTTPException(503, "Knowledge graph is disabled")
    if _graph is None:
        with _store_lock:
            if _graph is None:
                from .graph import KnowledgeGraph

                _graph = KnowledgeGraph(s)
    return _graph


@app.get("/v1/graph/stats")
def graph_stats():
    return graph().stats()


@app.get("/v1/graph/analytics")
def graph_analytics(hub_limit: int = Query(default=15, ge=5, le=50)):
    return graph().analytics(hub_limit)


@app.get("/v1/graph/overview")
def graph_overview(
    limit: int = Query(default=40, ge=5, le=100),
    relation: str = Query(default="", max_length=64),
):
    return graph().overview(limit, relation.strip())


@app.get("/v1/graph/path")
def graph_path(
    source: str = Query(min_length=8, max_length=64),
    target: str = Query(min_length=8, max_length=64),
    max_depth: int = Query(default=6, ge=1, le=10),
):
    return graph().shortest_path(source, target, max_depth)


@app.get("/v1/graph/entities")
def graph_entities(q: str = Query(default="", max_length=200),
                   limit: int = Query(default=50, ge=1, le=200)):
    return {"entities": graph().entities(q, limit)}


@app.get("/v1/graph/entities/{entity_id}")
def graph_neighbors(entity_id: str, limit: int = Query(default=100, ge=1, le=500)):
    kg = graph()
    profile = kg.entity_profile(entity_id)
    if profile is None:
        raise HTTPException(404, "Entity not found")
    neighborhood = kg.neighborhood(entity_id, limit)
    return neighborhood | {"profile": profile}


@app.get("/v1/graph/documents/{sha256}")
def graph_document(sha256: str):
    digest = _parse_sha256(sha256)
    doc = graph().document(digest)
    if doc is None:
        raise HTTPException(404, "Graph document not found")
    return doc


@app.get("/v1/graph/correlations/{sha256}")
def graph_correlations(
    sha256: str,
    limit: int = Query(default=20, ge=1, le=100),
    mode: str = Query(default="entity", pattern="^(entity|spatial|all)$"),
    radius_km: float | None = Query(default=None, ge=1, le=20000),
):
    digest = _parse_sha256(sha256)
    kg = graph()
    radius = radius_km if radius_km is not None else s.graph_spatial_radius_km
    if mode == "spatial":
        return kg.spatial_correlations(digest, limit, radius)
    if mode == "all":
        return {
            "sha256": digest,
            "entity": kg.correlations(digest, limit),
            "spatial": kg.spatial_correlations(digest, limit, radius),
        }
    return kg.correlations(digest, limit)


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "auth_required": s.auth_required,
        "database": database_configured(s),
        "database_error": database_problem() if database_configured(s) else None,
    }


@app.get("/v1/stats")
def stats():
    return searcher().stats()


@app.get("/v1/facets")
def facets():
    return searcher().facets()


@app.post("/v1/search")
def search(req: SearchRequest):
    results = searcher().search(
        req.query,
        req.top_k,
        mime=req.mime,
        since=req.since,
        until=req.until,
        sha256=req.sha256.lower() if req.sha256 else None,
        source_id=req.source_id or None,
    )
    return {"query": req.query, "count": len(results), "results": results}


@app.get("/v1/search")
def search_get(
    q: str = Query(min_length=1, max_length=2000),
    top_k: int = Query(default=12, ge=1, le=100),
    mime: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sha256: str | None = None,
    source_id: str | None = None,
):
    digest = _parse_sha256(sha256) if sha256 else None
    results = searcher().search(
        q, top_k, mime=mime, since=since, until=until, sha256=digest, source_id=source_id or None
    )
    return {"query": q, "count": len(results), "results": results}


@app.get("/v1/documents")
def list_documents(
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    mime: str | None = None,
    q: str | None = None,
    source_id: str | None = None,
):
    return searcher().list_documents(
        limit=limit, offset=offset, mime=mime, q=q, source_id=source_id or None
    )


def _record_text(digest: str) -> str:
    path = s.normalized_dir / digest[:2] / f"{digest}.json.gz"
    if not path.is_file():
        return ""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, json.JSONDecodeError, gzip.BadGzipFile):
        return ""
    text = record.get("text") or ""
    return text if isinstance(text, str) else ""


def _assemble_document(digest: str) -> dict:
    doc = searcher().document(digest)
    if not doc:
        raise HTTPException(404, "document not found")
    # Attach multimodal fields from the normalized lake record when present.
    normalized = s.normalized_dir / digest[:2] / f"{digest}.json.gz"
    if normalized.exists():
        try:
            with gzip.open(normalized, "rt", encoding="utf-8") as stream:
                record = json.load(stream)
            for key in (
                "geometry",
                "media",
                "measurements",
                "modalities",
                "agency",
                "source_id",
                "record_id",
                "published_at",
                "properties",
                "snapshot_sha256",
                "text",
            ):
                if key in record and not doc.get(key):
                    doc[key] = record[key]
        except Exception:
            pass
    if not doc.get("source_id"):
        from .sources import source_for_url

        origin = source_for_url(doc.get("source_url") or "", s)
        if origin:
            doc["source_id"] = origin.id
            doc["agency"] = doc.get("agency") or origin.agency
    doc["citation"] = citation_for(doc)
    latest = searcher().latest_sha(version_url(doc.get("requested_url"), doc.get("source_url")))
    if latest and latest != digest:
        doc["latest_sha256"] = latest
    urls = [doc.get("source_url"), doc.get("requested_url"), *(doc.get("source_urls") or [])]
    doc["observations"] = observations_for(s, digest, urls)
    doc["versions"] = version_chain(doc["observations"])
    current = doc.get("requested_url") or doc.get("source_url")
    previous = next(
        (
            row.get("previous_sha256")
            for row in doc["versions"]
            if row.get("sha256") == digest and row.get("url") == current and row.get("previous_sha256")
        ),
        None,
    )
    if previous:
        current_text = doc.get("text") or "\n".join(
            chunk.get("text") or "" for chunk in doc.get("chunks") or []
        )
        previous_text = _record_text(previous)
        if previous_text and current_text and previous_text != current_text:
            doc["previous_sha256"] = previous
            doc["text_diff"] = text_diff(previous_text[:80_000], current_text[:80_000])
    if s.graph_enabled:
        try:
            gdoc = graph().document(digest)
            if gdoc:
                doc["graph"] = {
                    "entities": gdoc.get("entities", [])[:100],
                    "metadata": gdoc.get("metadata") or {},
                }
        except Exception:
            pass
    return doc


@app.get("/v1/documents/{sha256}")
def get_document(sha256: str):
    return _assemble_document(_parse_sha256(sha256))


def _document_page(doc: dict) -> str:
    cite = doc.get("citation") or {}
    title = html.escape(cite.get("title") or doc.get("title") or "Document")
    agency = html.escape(cite.get("agency") or "Unknown agency")
    collection = html.escape(cite.get("collection") or doc.get("source_id") or "")
    source_url = cite.get("source_url") or doc.get("source_url") or ""
    digest = doc.get("source_sha256") or ""
    fetched = html.escape((cite.get("fetched_at") or "")[:19])
    latest = doc.get("latest_sha256") or ""
    newer_html = ""
    if latest and latest != digest:
        newer_html = (
            '<p class="meta">A newer capture of this URL is stored. '
            f'<a href="/d/{html.escape(latest)}">Open the current version</a>.</p>'
        )
    chunks = doc.get("chunks") or []
    if chunks:
        body = "\n\n".join(html.escape(chunk.get("text") or "") for chunk in chunks)
    else:
        body = html.escape(doc.get("text") or "No extracted text is stored for this file.")
    versions = []
    for row in doc.get("versions") or []:
        sha = row.get("sha256") or ""
        mark = "bytes changed" if row.get("changed") else "same bytes" if sha else "no hash"
        short = html.escape(sha[:12])
        if len(sha) == 64:
            label = f'<a href="/d/{html.escape(sha)}">{short}</a>'
            if sha == digest:
                label += " · this page"
        else:
            label = short or "no hash"
        versions.append(
            "<li>"
            f"{html.escape((row.get('fetched_at') or '')[:19])} · HTTP {html.escape(str(row.get('status_code') or ''))}"
            f" · {html.escape(mark)} · {label}"
            "</li>"
        )
    version_html = (
        f"<ul>{''.join(versions)}</ul>"
        if versions
        else "<p>No captures recorded yet.</p>"
    )
    diff = doc.get("text_diff") or ""
    diff_html = f"<h2>Change from previous capture</h2><pre class=\"chunk\">{html.escape(diff)}</pre>" if diff else ""
    mime = doc.get("mime") or ""
    original = ""
    if mime == "application/pdf":
        original = f'<iframe class="media-frame" title="Original PDF" src="/v1/raw/{html.escape(digest)}?inline=1"></iframe>'
    elif mime.startswith("image/"):
        original = f'<img src="/v1/raw/{html.escape(digest)}" alt="{title}" />'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{title} — CIA Brain</title>
  <link rel="stylesheet" href="/assets/styles.css" />
</head>
<body>
  <main class="frame">
    <p><a href="/">Catalog</a></p>
    <h1>{title}</h1>
    <p class="meta">{agency} · {collection} · fetched {fetched}</p>
    <p class="meta"><a href="{html.escape(source_url, quote=True)}">Government source</a>
      · <a href="/v1/raw/{html.escape(digest)}">Download original</a>
      · <code>{html.escape(digest)}</code></p>
    {newer_html}
    {original}
    <h2>Captures</h2>
    {version_html}
    {diff_html}
    <h2>Extracted text</h2>
    <pre class="chunk">{body}</pre>
  </main>
</body>
</html>"""


@app.get("/d/{sha256}", response_class=HTMLResponse)
def document_page(sha256: str):
    return HTMLResponse(_document_page(_assemble_document(_parse_sha256(sha256))))


@app.get("/v1/crawl/stats")
def crawl_stats():
    path = s.state_dir / "crawl.sqlite"
    if not path.exists():
        return {"frontier": {}, "downloads": 0}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            frontier = {
                r[0]: r[1]
                for r in db.execute("SELECT status,COUNT(*) FROM frontier GROUP BY status")
            }
            downloads = db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    except sqlite3.Error as exc:
        log.warning("crawl stats unavailable: %s", exc)
        return {"frontier": {}, "downloads": 0}
    return {"frontier": frontier, "downloads": downloads}


@app.get("/v1/pipeline/jobs")
def pipeline_jobs():
    _run_job, public_status, _write_job_result = _pipeline()
    return public_status(s)


@app.post("/v1/pipeline/jobs/trigger")
def pipeline_trigger(req: TriggerJobRequest, _admin=Depends(require_admin)):
    run_job, _public_status, write_job_result = _pipeline()
    result = run_job(req.job, s)
    if not result.ok and result.error and result.error.startswith("unknown"):
        raise HTTPException(404, result.error)
    write_job_result(s, result)
    return {
        "job": result.job,
        "ok": result.ok,
        "detail": result.detail,
        "error": result.error,
        "ran_at": result.ran_at,
    }


@app.post("/v1/pipeline/jobs/{job}/run")
def pipeline_run_once(job: str, _admin=Depends(require_admin)):
    """Run a job without requiring the long-running scheduler process."""
    run_job, _public_status, write_job_result = _pipeline()
    result = run_job(job, s)
    if not result.ok and result.error and result.error.startswith("unknown"):
        raise HTTPException(404, result.error)
    write_job_result(s, result)
    return {
        "job": result.job,
        "ok": result.ok,
        "detail": result.detail,
        "error": result.error,
        "ran_at": result.ran_at,
    }


@app.get("/v1/raw/{sha256}")
def raw(sha256: str, inline: int = Query(default=0, ge=0, le=1)):
    digest = _parse_sha256(sha256)
    matches = list((s.raw_dir / digest[:2] / digest[2:4]).glob(f"{digest}.*"))
    if not matches:
        raise HTTPException(404, "not found")
    path = matches[0]
    ext = path.suffix.lower()
    # Allow safe raster images inline for the gallery; never serve HTML/SVG as documents.
    inline_images = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    headers = {"X-Content-Type-Options": "nosniff"}
    if inline and ext == ".pdf":
        headers["Content-Disposition"] = "inline"
        return FileResponse(path, media_type="application/pdf", headers=headers)
    if ext in inline_images:
        return FileResponse(path, media_type=inline_images[ext], headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers=headers,
    )


@app.get("/v1/config/public")
def public_config():
    return {
        "agent_url": "/agent",
        "llm_model": s.llm_model,
        "embedding_backend": s.embedding_backend,
        "embedding_model": s.embedding_model,
        "recrawl_after_hours": s.recrawl_after_hours,
        "auth_required": s.auth_required,
        "allow_registration": s.allow_registration,
        "database_configured": database_configured(s),
        "api_rate_limit_per_minute": s.api_rate_limit_per_minute,
        "api_rate_limit_per_day": s.api_rate_limit_per_day,
        "anon_rate_limit_per_minute": s.anon_rate_limit_per_minute,
        "raw_rate_limit_per_minute": s.raw_rate_limit_per_minute,
        "raw_rate_limit_per_day": s.raw_rate_limit_per_day,
        "job_intervals": {
            "recrawl_stale": s.job_recrawl_interval_seconds,
            "parquet_compact": s.job_parquet_compact_interval_seconds,
            "frontier_seed": s.job_frontier_seed_interval_seconds,
            "health_snapshot": s.job_health_interval_seconds,
        },
    }


@app.api_route("/agent/{path:path}", methods=["GET", "POST"])
async def agent_proxy(path: str, request: Request):
    """Proxy to the local Ollama agent so the UI stays same-origin.

    Auth and rate limits are enforced by AuthRateLimitMiddleware (/agent is an API surface).
    """
    # Reject path traversal / absolute URLs in the proxied path segment.
    if not path or path.startswith("/") or ".." in path.split("/") or "://" in path:
        raise HTTPException(400, "invalid agent path")
    url = f"{s.agent_api_url.rstrip('/')}/{path}"
    body = await request.body()
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            upstream = await client.request(
                request.method,
                url,
                content=body if body else None,
                headers=headers,
                params=dict(request.query_params),
            )
    except httpx.ConnectError:
        raise HTTPException(
            503, "local agent unavailable; start with: docker compose --profile ai up -d"
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "local agent timed out")
    except httpx.HTTPError as exc:
        log.warning("agent proxy error: %s", exc)
        raise HTTPException(502, "local agent request failed")
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


if UI_DIR.is_dir():
    assets = UI_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def ui_index():
        return FileResponse(UI_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "cia_brain.api:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=False,
    )
