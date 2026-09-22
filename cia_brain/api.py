from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .jobs import run_job
from .log import configure_logging
from .scheduler import public_status, write_job_result
from .search import HybridSearcher
from .settings import get_settings

s = get_settings()
configure_logging(s.log_level)
app = FastAPI(title="CIA Brain Search & Discovery", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
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


@app.get("/v1/graph/entities")
def graph_entities(q: str = Query(default="", max_length=200),
                   limit: int = Query(default=50, ge=1, le=200)):
    return {"entities": graph().entities(q, limit)}


@app.get("/v1/graph/entities/{entity_id}")
def graph_neighbors(entity_id: str, limit: int = Query(default=100, ge=1, le=500)):
    return graph().neighborhood(entity_id, limit)


@app.get("/v1/graph/documents/{sha256}")
def graph_document(sha256: str):
    doc = graph().document(sha256)
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
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise HTTPException(400, "invalid sha256")
    kg = graph()
    radius = radius_km if radius_km is not None else s.graph_spatial_radius_km
    if mode == "spatial":
        return kg.spatial_correlations(sha256, limit, radius)
    if mode == "all":
        return {
            "sha256": sha256,
            "entity": kg.correlations(sha256, limit),
            "spatial": kg.spatial_correlations(sha256, limit, radius),
        }
    return kg.correlations(sha256, limit)


@app.get("/healthz")
def healthz():
    return {"ok": True}


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
    q: str = Query(min_length=1),
    top_k: int = Query(default=12, ge=1, le=100),
    mime: str | None = None,
    since: str | None = None,
    until: str | None = None,
    sha256: str | None = None,
):
    results = searcher().search(
        q, top_k, mime=mime, since=since, until=until, sha256=sha256.lower() if sha256 else None
    )
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
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
        raise HTTPException(400, "invalid sha256")
    doc = searcher().document(sha256.lower())
    if not doc:
        raise HTTPException(404, "document not found")
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
def pipeline_trigger(req: TriggerJobRequest):
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
def pipeline_run_once(job: str):
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
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
        raise HTTPException(400, "invalid sha256")
    matches = list((s.raw_dir / sha256[:2] / sha256[2:4]).glob(f"{sha256}.*"))
    if not matches:
        raise HTTPException(404, "not found")
    return FileResponse(matches[0])


@app.get("/v1/config/public")
def public_config():
    return {
        "agent_url": "/agent",
        "llm_model": s.llm_model,
        "embedding_backend": s.embedding_backend,
        "embedding_model": s.embedding_model,
        "recrawl_after_hours": s.recrawl_after_hours,
        "job_intervals": {
            "recrawl_stale": s.job_recrawl_interval_seconds,
            "parquet_compact": s.job_parquet_compact_interval_seconds,
            "frontier_seed": s.job_frontier_seed_interval_seconds,
            "health_snapshot": s.job_health_interval_seconds,
        },
    }


@app.api_route("/agent/{path:path}", methods=["GET", "POST"])
async def agent_proxy(path: str, request: Request):
    """Proxy to the local Ollama agent so the UI stays same-origin."""
    url = f"{s.agent_api_url.rstrip('/')}/{path}"
    body = await request.body()
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as client:
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

    uvicorn.run("cia_brain.api:app", host="0.0.0.0", port=8080, reload=False)
