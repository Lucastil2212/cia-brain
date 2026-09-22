from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

import numpy as np
from usearch.index import Index

from .embedding import Embedder
from .settings import Settings

log = logging.getLogger(__name__)


class HybridSearcher:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db_path = settings.state_dir / "search.sqlite"
        self.vector_path = settings.state_dir / "vectors.usearch"
        self.meta_path = settings.state_dir / "vector-meta.json"
        self._embedder: Embedder | None = None
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._index: Index | None = None
        self._mtime = -1.0
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Initialize an empty read-compatible store when no indexer has run yet."""
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
              id INTEGER PRIMARY KEY,
              source_sha256 TEXT UNIQUE NOT NULL,
              source_url TEXT NOT NULL,
              source_urls_json TEXT NOT NULL DEFAULT '[]',
              requested_url TEXT,
              title TEXT,
              mime TEXT,
              size INTEGER,
              fetched_at TEXT,
              extracted_at TEXT,
              text_chars INTEGER
            );
            CREATE TABLE IF NOT EXISTS chunks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              document_id INTEGER NOT NULL,
              chunk_no INTEGER NOT NULL,
              text TEXT NOT NULL,
              FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
              UNIQUE(document_id, chunk_no)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
              text, content='chunks', content_rowid='id', tokenize='porter unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
              INSERT INTO chunks_fts(chunks_fts,rowid,text) VALUES('delete',old.id,old.text);
              INSERT INTO chunks_fts(rowid,text) VALUES(new.id,new.text);
            END;
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self.db.commit()

    def _get_embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = Embedder(self.s)
        return self._embedder

    def _load_index(self) -> Index | None:
        if not self.vector_path.exists() or not self.meta_path.exists():
            return None
        mtime = self.vector_path.stat().st_mtime
        if self._index is not None and mtime == self._mtime:
            return self._index
        with self._lock:
            mtime = self.vector_path.stat().st_mtime
            if self._index is not None and mtime == self._mtime:
                return self._index
            meta = json.loads(self.meta_path.read_text())
            embedder = self._get_embedder()
            if meta.get("model") != embedder.model_name:
                raise RuntimeError(
                    f"vector model mismatch: index={meta.get('model')} "
                    f"runtime={embedder.model_name}; rebuild index"
                )
            idx = Index(ndim=int(meta["dim"]), metric="cos", dtype="f32")
            idx.load(str(self.vector_path))
            self._index = idx
            self._mtime = mtime
            return idx

    def _lexical(self, query: str, limit: int) -> list[int]:
        # Quoting the user query avoids treating punctuation as FTS operators.
        terms = [t for t in query.replace('"', " ").split() if t]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in terms[:32])
        rows = self.db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def _semantic(self, query: str, limit: int) -> list[int]:
        idx = self._load_index()
        if idx is None or len(idx) == 0:
            return []
        vec = self._get_embedder().embed(["query: " + query])[0]
        matches = idx.search(vec, min(limit, len(idx)))
        return [int(k) for k in matches.keys]

    def _hydrate(self, ranked: list[int], scores: dict[int, float]) -> list[dict]:
        if not ranked:
            return []
        placeholders = ",".join("?" * len(ranked))
        rows = self.db.execute(
            f"""SELECT c.id,c.chunk_no,c.text,d.source_sha256,d.source_url,d.source_urls_json,
                      d.title,d.mime,d.fetched_at,d.text_chars
               FROM chunks c JOIN documents d ON d.id=c.document_id
               WHERE c.id IN ({placeholders})""",
            ranked,
        ).fetchall()
        by_id = {int(r["id"]): r for r in rows}
        out = []
        for cid in ranked:
            row = by_id.get(cid)
            if not row:
                continue
            item = dict(row)
            try:
                item["source_urls"] = json.loads(item.pop("source_urls_json"))
            except Exception:
                item["source_urls"] = [item["source_url"]]
            item["score"] = scores[cid]
            out.append(item)
        return out

    def _passes_filters(
        self,
        item: dict,
        *,
        mime: str | None,
        since: str | None,
        until: str | None,
        sha256: str | None,
    ) -> bool:
        if mime and not (item.get("mime") or "").startswith(mime):
            return False
        if sha256 and item.get("source_sha256") != sha256.lower():
            return False
        fetched = item.get("fetched_at") or ""
        if since and fetched < since:
            return False
        if until and fetched > until:
            return False
        return True

    def search(
        self,
        query: str,
        top_k: int | None = None,
        *,
        mime: str | None = None,
        since: str | None = None,
        until: str | None = None,
        sha256: str | None = None,
    ) -> list[dict]:
        top_k = top_k or self.s.default_top_k
        filtered = any([mime, since, until, sha256])
        candidate_k = max(50, top_k * (8 if filtered else 5))
        lexical = self._lexical(query, candidate_k)
        semantic = self._semantic(query, candidate_k)
        scores: dict[int, float] = {}
        for rank, cid in enumerate(lexical, 1):
            scores[cid] = scores.get(cid, 0.0) + self.s.hybrid_lexical_weight / (self.s.rrf_k + rank)
        for rank, cid in enumerate(semantic, 1):
            scores[cid] = scores.get(cid, 0.0) + self.s.hybrid_semantic_weight / (self.s.rrf_k + rank)
        ranked = sorted(scores, key=scores.get, reverse=True)
        hydrated = self._hydrate(ranked, scores)
        if filtered:
            hydrated = [
                item
                for item in hydrated
                if self._passes_filters(item, mime=mime, since=since, until=until, sha256=sha256)
            ]
        return hydrated[:top_k]

    def document(self, sha256: str) -> dict | None:
        row = self.db.execute(
            """SELECT id,source_sha256,source_url,source_urls_json,requested_url,title,mime,size,
                      fetched_at,extracted_at,text_chars
               FROM documents WHERE source_sha256=?""",
            (sha256.lower(),),
        ).fetchone()
        if not row:
            return None
        doc = dict(row)
        try:
            doc["source_urls"] = json.loads(doc.pop("source_urls_json"))
        except Exception:
            doc["source_urls"] = [doc["source_url"]]
        chunks = self.db.execute(
            "SELECT id,chunk_no,text FROM chunks WHERE document_id=? ORDER BY chunk_no",
            (doc["id"],),
        ).fetchall()
        doc["chunks"] = [dict(c) for c in chunks]
        return doc

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        mime: str | None = None,
        q: str | None = None,
    ) -> dict:
        where: list[str] = []
        params: list = []
        if mime:
            where.append("mime LIKE ?")
            params.append(f"{mime}%")
        if q:
            where.append("(title LIKE ? OR source_url LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.execute(f"SELECT COUNT(*) FROM documents{clause}", params).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT source_sha256,source_url,title,mime,size,fetched_at,text_chars
                FROM documents{clause}
                ORDER BY (fetched_at IS NULL), fetched_at DESC, id DESC
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
        return {"total": total, "limit": limit, "offset": offset, "documents": [dict(r) for r in rows]}

    def facets(self) -> dict:
        mime_rows = self.db.execute(
            """SELECT COALESCE(mime,'(unknown)') AS mime, COUNT(*) AS n
               FROM documents GROUP BY 1 ORDER BY n DESC LIMIT 40"""
        ).fetchall()
        return {"mime": [{"value": r["mime"], "count": r["n"]} for r in mime_rows]}

    def stats(self) -> dict:
        docs = self.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        chars = self.db.execute("SELECT COALESCE(SUM(text_chars),0) FROM documents").fetchone()[0]
        crawl = {}
        crawl_path = self.s.state_dir / "crawl.sqlite"
        if crawl_path.exists():
            try:
                cdb = sqlite3.connect(f"file:{crawl_path}?mode=ro", uri=True)
                crawl = {
                    r[0]: r[1]
                    for r in cdb.execute("SELECT status,COUNT(*) FROM frontier GROUP BY status")
                }
                cdb.close()
            except Exception as exc:
                log.warning("crawl stats unavailable: %s", exc)
        return {
            "documents": docs,
            "chunks": chunks,
            "text_chars": chars,
            "embedding_backend": self.s.embedding_backend,
            "embedding_model": (
                self.s.embedding_model
                if self.s.embedding_backend.lower() == "fastembed"
                else self.s.ollama_embed_model
            ),
            "frontier": crawl,
        }
