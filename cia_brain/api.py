from __future__ import annotations

import gzip
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth_routes import require_admin, router as auth_router
from .db import database_configured
from .jobs import run_job
from .log import configure_logging
from .middleware import AuthRateLimitMiddleware
from .paths import require_sha256
from .scheduler import public_status, write_job_result
from .search import HybridSearcher
from .settings import assert_secure_settings, get_settings

s = get_settings()
configure_logging(s.log_level)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    assert_secure_settings(s)
    if database_configured(s):
        from .auth import migrate

        migrate(s)
        log.info("account schema migrated")
    yield


app = FastAPI(
    title="CIA Brain Public Archive Research Catalog",
    description=(
        "Local library-style discovery over public U.S. government archives, "
        "live multimodal feeds, and a source-grounded knowledge graph. "
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
_searcher: HybridSearcher | None = None

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=12, ge=1, le=100)
    mime: str | None = Field(default=None, max_length=120)
    since: str | None = Field(default=None, max_length=64)
    until: str | None = Field(default=None, max_length=64)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class TriggerJobRequest(BaseModel):
    job: str = Field(min_length=1, max_length=64)


def searcher() -> HybridSearcher:
    global _searcher
    if _searcher is None:
        _searcher = HybridSearcher(s)
    return _searcher


def _parse_sha256(value: str) -> str:
    try:
        return require_sha256(value)
    except ValueError as exc:
        raise HTTPException(400, "invalid sha256") from exc


@app.get("/v1/sources")
def sources():
    from .sources import catalog
    statuses = {}
    path = s.state_dir / "feeds.sqlite"
    if path.exists():
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            statuses = {r["id"]: dict(r) for r in db.execute("SELECT * FROM feeds")}
    return {"sources": [item | {"status": statuses.get(item["id"])} for item in catalog(s)]}


def graph():
    if not s.graph_enabled:
        raise HTTPException(503, "Knowledge graph is disabled")
    from .graph import KnowledgeGraph
    return KnowledgeGraph(s)


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
    profile = graph().entity_profile(entity_id)
    if profile is None:
        raise HTTPException(404, "Entity not found")
    neighborhood = graph().neighborhood(entity_id, limit)
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
):
    digest = _parse_sha256(sha256) if sha256 else None
    results = searcher().search(q, top_k, mime=mime, since=since, until=until, sha256=digest)
    return {"query": q, "count": len(results), "results": results}


@app.get("/v1/documents")
def list_documents(
    limit: int = Query(default=40, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    mime: str | None = None,
    q: str | None = None,
):
    return searcher().list_documents(limit=limit, offset=offset, mime=mime, q=q)


@app.get("/v1/documents/{sha256}")
def get_document(sha256: str):
    digest = _parse_sha256(sha256)
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
                if key in record and key not in doc:
                    doc[key] = record[key]
                elif key in record and key == "text" and not doc.get("chunks"):
                    doc["text"] = record[key]
        except Exception:
            pass
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


@app.get("/v1/crawl/stats")
def crawl_stats():
    path = s.state_dir / "crawl.sqlite"
    if not path.exists():
        return {"frontier": {}, "downloads": 0}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    frontier = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM frontier GROUP BY status")}
    downloads = db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    db.close()
    return {"frontier": frontier, "downloads": downloads}


@app.get("/v1/pipeline/jobs")
def pipeline_jobs():
    return public_status(s)


@app.post("/v1/pipeline/jobs/trigger")
def pipeline_trigger(req: TriggerJobRequest, _admin=Depends(require_admin)):
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
def raw(sha256: str):
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
