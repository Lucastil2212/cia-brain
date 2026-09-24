from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .log import configure_logging
from .settings import get_settings

s = get_settings()
configure_logging(s.log_level)
log = logging.getLogger(__name__)

_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))
    return _client


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    global _client
    if _client is not None:
        _client.close()
        _client = None


app = FastAPI(title="CIA Brain Local Agent", version="0.2.0", lifespan=lifespan)
# Same-origin via API proxy; no wildcard CORS.


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    top_k: int = Field(default=10, ge=1, le=30)


class ProductRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=4000)
    product: Literal["lesson", "study-guide", "quiz", "timeline", "brief"] = "lesson"
    top_k: int = Field(default=12, ge=1, le=30)


class ReasonRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    top_k: int = Field(default=12, ge=1, le=30)
    max_steps: int = Field(default=3, ge=1, le=5)


def retrieve(query: str, top_k: int) -> list[dict]:
    r = client().post(f"{s.search_api_url}/v1/search", json={"query": query, "top_k": top_k})
    r.raise_for_status()
    return r.json()["results"]


def context_block(results: list[dict]) -> str:
    blocks = []
    for i, r in enumerate(results, 1):
        blocks.append(
            f"[S{i}] URL: {r['source_url']}\nTitle: {r.get('title') or '(untitled)'}\n"
            f"SHA256: {r['source_sha256']}\nExcerpt:\n{r['text']}"
        )
    return "\n\n".join(blocks)


def ask_ollama(system: str, prompt: str) -> str:
    r = client().post(
        f"{s.ollama_url}/api/chat",
        json={
            "model": s.llm_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0.2},
        },
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


BASE_SYSTEM = """You are a source-grounded research assistant over a local lake of public U.S. government
material: CIA and other agency FOIA/declassification collections, CRS reports, and polled open data
feeds (weather, seismic, space weather, satellite imagery references, agency press RSS, NARA catalog hits).
Use only the supplied source excerpts for factual claims about the archive. Cite each claim with [S#].
Distinguish what a historical document or feed record states from independently established fact.
Preserve dates, agency provenance, and modality (text, geospatial, telemetry, image/media reference).
If the retrieved evidence is insufficient, say so. Do not invent citations or silently fill gaps from memory."""

PLAN_SYSTEM = """You plan research over a local multi-agency public U.S. government archive and live open-data feeds.
Given a user question, reply with 1-3 short search queries (one per line, no numbering, no commentary)
that would retrieve the most useful excerpts. Prefer precise historical names, programs, places,
agency acronyms (CIA, FBI, NSA, ODNI, DIA, NARA, CRS), and dated events."""


@app.get("/healthz")
def healthz():
    return {"ok": True, "model": s.llm_model}


@app.post("/v1/chat")
def chat(req: ChatRequest):
    results = retrieve(req.question, req.top_k)
    if not results:
        raise HTTPException(404, "no indexed evidence found")
    answer = ask_ollama(BASE_SYSTEM, f"Sources:\n{context_block(results)}\n\nQuestion: {req.question}")
    return {"answer": answer, "sources": results}


@app.post("/v1/product")
def product(req: ProductRequest):
    results = retrieve(req.topic, req.top_k)
    if not results:
        raise HTTPException(404, "no indexed evidence found")
    instructions = {
        "lesson": "Create a concise educational lesson with learning objectives, explanation, discussion questions, and source notes.",
        "study-guide": "Create a structured study guide with key terms, dates, people, claims, caveats, and review questions.",
        "quiz": "Create a 10-question quiz with an answer key. Every answer must cite a source marker.",
        "timeline": "Create a chronological timeline. Cite every dated event and flag conflicts or uncertain dates.",
        "brief": "Create a neutral research brief separating document claims, historical context contained in the sources, and open questions.",
    }[req.product]
    answer = ask_ollama(
        BASE_SYSTEM,
        f"Sources:\n{context_block(results)}\n\nTopic: {req.topic}\nTask: {instructions}",
    )
    return {"product": req.product, "content": answer, "sources": results}


@app.post("/v1/reason")
def reason(req: ReasonRequest):
    """Multi-step local-LLM orchestration: plan queries → retrieve → synthesize."""
    steps: list[dict] = []
    plan_raw = ask_ollama(PLAN_SYSTEM, req.question)
    queries = [ln.strip(" -*\t") for ln in plan_raw.splitlines() if ln.strip()][: req.max_steps]
    if not queries:
        queries = [req.question]
    steps.append({"type": "plan", "queries": queries, "raw": plan_raw})

    seen: set[str] = set()
    merged: list[dict] = []
    for q in queries:
        hits = retrieve(q, req.top_k)
        steps.append({"type": "retrieve", "query": q, "hits": len(hits)})
        for h in hits:
            key = f"{h['source_sha256']}:{h['chunk_no']}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(h)

    if not merged:
        raise HTTPException(404, "no indexed evidence found")

    # Keep strongest unique evidence first (already RRF-ranked per query).
    merged = merged[: max(req.top_k, 12)]
    answer = ask_ollama(
        BASE_SYSTEM,
        f"Sources:\n{context_block(merged)}\n\nQuestion: {req.question}\n"
        "Synthesize a careful answer. Note disagreements across sources. "
        "End with a short 'Open questions' section if gaps remain.",
    )
    steps.append({"type": "synthesize", "sources_used": len(merged)})
    return {"answer": answer, "sources": merged, "steps": steps, "model": s.llm_model}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("cia_brain.agent:app", host="0.0.0.0", port=8090, reload=False)
