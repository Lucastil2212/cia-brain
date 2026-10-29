from __future__ import annotations

import importlib.util
import json
import logging
import sqlite3
import threading
from contextlib import closing
from typing import TYPE_CHECKING

from .history import citation_for, ensure_provenance_columns
from .settings import Settings

if TYPE_CHECKING:
    from usearch.index import Index

    from .embedding import Embedder

log = logging.getLogger(__name__)


class HybridSearcher:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db_path = settings.state_dir / "search.sqlite"
        self.vector_path = settings.state_dir / "vectors.usearch"
        self.meta_path = settings.state_dir / "vector-meta.json"
        self._embedder: Embedder | None = None
        self.db = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._index: Index | None = None
        self._mtime = -1.0
        self._lock = threading.RLock()
        # FastAPI runs sync routes on a thread pool; one sqlite connection must not be shared
        # by concurrent cursors.
        self._db_lock = threading.RLock()
        self._semantic_disabled: str | None = None
        missing = [m for m in ("numpy", "usearch") if importlib.util.find_spec(m) is None]
        if missing:
            self._semantic_disabled = f"semantic search unavailable: {', '.join(missing)} missing"
            log.info("%s; serving lexical results only", self._semantic_disabled)
        self._ensure_schema()

    def _rows(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._db_lock:
            return self.db.execute(sql, params).fetchall()

    def _scalar(self, sql: str, params=()):
        with self._db_lock:
            row = self.db.execute(sql, params).fetchone()
        return row[0] if row else None

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
        ensure_provenance_columns(self.db)
        self.db.commit()

    def _get_embedder(self) -> Embedder:
        if self._embedder is None:
            from .embedding import Embedder

            self._embedder = Embedder(self.s)
        return self._embedder

    def _load_index(self) -> Index | None:
        if not self.vector_path.exists() or not self.meta_path.exists():
            return None
        from usearch.index import Index

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

    def _lexical(self, query: str, limit: int, *, latest_only: bool = True) -> list[int]:
        # Quoting the user query avoids treating punctuation as FTS operators.
        terms = [t for t in query.replace('"', " ").split() if t]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in terms[:32])
        if latest_only:
            sql = f"""
                SELECT chunks_fts.rowid
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN documents d ON d.id = c.document_id
                WHERE chunks_fts MATCH ?
                  AND d.id IN ({self._latest_id_sql("")})
                ORDER BY bm25(chunks_fts)
                LIMIT ?
            """
        else:
            sql = (
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts) LIMIT ?"
            )
        try:
            rows = self._rows(sql, (fts_query, limit))
        except sqlite3.OperationalError as exc:
            log.warning("lexical search failed for %r: %s", query[:80], exc)
            return []
        return [int(r[0]) for r in rows]

    @property
    def semantic_available(self) -> bool:
        return self._semantic_disabled is None

    def _semantic(self, query: str, limit: int) -> list[int]:
        if self._semantic_disabled:
            return []
        try:
            idx = self._load_index()
            if idx is None or len(idx) == 0:
                return []
            vec = self._get_embedder().embed(["query: " + query])[0]
            matches = idx.search(vec, min(limit, len(idx)))
            return [int(k) for k in matches.keys]
        except ImportError as exc:
            # Slim installs (e.g. Vercel) ship without the pipeline extra.
            self._semantic_disabled = f"semantic search unavailable: {exc.name or exc}"
            log.info("%s; serving lexical results only", self._semantic_disabled)
        except Exception as exc:
            log.warning("semantic search failed; serving lexical results only: %s", exc)
        return []

    def _hydrate(self, ranked: list[int], scores: dict[int, float]) -> list[dict]:
        if not ranked:
            return []
        placeholders = ",".join("?" * len(ranked))
        rows = self._rows(
            f"""SELECT c.id,c.chunk_no,c.text,d.source_sha256,d.source_url,d.source_urls_json,
                      d.title,d.mime,d.fetched_at,d.text_chars,d.source_id,d.agency
               FROM chunks c JOIN documents d ON d.id=c.document_id
               WHERE c.id IN ({placeholders})""",
            ranked,
        )
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
            if not item.get("source_id"):
                from .sources import source_for_url

                origin = source_for_url(item.get("source_url") or "", self.s)
                if origin:
                    item["source_id"] = origin.id
                    item["agency"] = item.get("agency") or origin.agency
            item["citation"] = citation_for(item)
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
        source_id: str | None = None,
    ) -> bool:
        if mime and not (item.get("mime") or "").startswith(mime):
            return False
        if source_id and (item.get("source_id") or "") != source_id:
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
        source_id: str | None = None,
    ) -> list[dict]:
        top_k = top_k or self.s.default_top_k
        filtered = any([mime, since, until, sha256, source_id])
        candidate_k = max(50, top_k * (8 if filtered else 5))
        # Current captures are selected before the window is cut. A hash lookup
        # still searches older text. Semantic search over-fetches, then drops
        # superseded hits, because the vector index cannot filter by URL.
        lexical = self._lexical(query, candidate_k, latest_only=not sha256)
        semantic = self._semantic(query, candidate_k if sha256 else candidate_k * 4)
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
                if self._passes_filters(
                    item, mime=mime, since=since, until=until, sha256=sha256, source_id=source_id
                )
            ]
        # A chosen hash still opens that capture. Ordinary search shows the current one.
        if not sha256:
            hydrated = self._prefer_latest(hydrated)
        return hydrated[:top_k]

    def _prefer_latest(self, items: list[dict]) -> list[dict]:
        urls = list(dict.fromkeys(item.get("source_url") for item in items if item.get("source_url")))
        if not urls:
            return items
        placeholders = ",".join("?" * len(urls))
        rows = self._rows(
            f"""SELECT source_url, source_sha256 FROM (
                  SELECT source_url, source_sha256,
                         ROW_NUMBER() OVER (
                           PARTITION BY source_url
                           ORDER BY (fetched_at IS NULL), fetched_at DESC, id DESC
                         ) AS rn
                  FROM documents
                  WHERE source_url IN ({placeholders})
                ) WHERE rn = 1""",
            urls,
        )
        latest = {row["source_url"]: row["source_sha256"] for row in rows}
        return [
            item
            for item in items
            if latest.get(item.get("source_url"), item.get("source_sha256")) == item.get("source_sha256")
        ]

    def latest_sha(self, source_url: str) -> str | None:
        if not source_url:
            return None
        rows = self._rows(
            """SELECT source_sha256 FROM documents
               WHERE source_url=?
               ORDER BY (fetched_at IS NULL), fetched_at DESC, id DESC
               LIMIT 1""",
            (source_url,),
        )
        return rows[0]["source_sha256"] if rows else None

    def _latest_id_sql(self, clause: str) -> str:
        return f"""
            SELECT id FROM (
              SELECT id,
                     ROW_NUMBER() OVER (
                       PARTITION BY source_url
                       ORDER BY (fetched_at IS NULL), fetched_at DESC, id DESC
                     ) AS rn
              FROM documents
              {clause}
            )
            WHERE rn = 1
        """

    def document(self, sha256: str) -> dict | None:
        rows = self._rows(
            """SELECT id,source_sha256,source_url,source_urls_json,requested_url,title,mime,size,
                      fetched_at,extracted_at,text_chars,source_id,agency
               FROM documents WHERE source_sha256=?""",
            (sha256.lower(),),
        )
        if not rows:
            return None
        doc = dict(rows[0])
        try:
            doc["source_urls"] = json.loads(doc.pop("source_urls_json"))
        except Exception:
            doc["source_urls"] = [doc["source_url"]]
        chunks = self._rows(
            "SELECT id,chunk_no,text FROM chunks WHERE document_id=? ORDER BY chunk_no",
            (doc["id"],),
        )
        doc["chunks"] = [dict(c) for c in chunks]
        if not doc.get("source_id"):
            from .sources import source_for_url

            origin = source_for_url(doc.get("source_url") or "", self.s)
            if origin:
                doc["source_id"] = origin.id
                doc["agency"] = doc.get("agency") or origin.agency
        doc["citation"] = citation_for(doc)
        return doc

    def list_documents(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        mime: str | None = None,
        q: str | None = None,
        source_id: str | None = None,
    ) -> dict:
        from .sources import collection_url_prefixes

        where: list[str] = []
        params: list = []
        if mime:
            where.append("mime LIKE ?")
            params.append(f"{mime}%")
        if source_id:
            prefixes = collection_url_prefixes(source_id)
            likes = " OR ".join("source_url LIKE ?" for _ in prefixes)
            where.append(f"(source_id = ? OR {likes})" if likes else "source_id = ?")
            params.append(source_id)
            params.extend(f"{prefix}%" for prefix in prefixes)
        if q:
            where.append("(title LIKE ? OR source_url LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        latest = self._latest_id_sql(clause)
        total = self._scalar(f"SELECT COUNT(*) FROM ({latest})", params) or 0
        rows = self._rows(
            f"""SELECT source_sha256,source_url,title,mime,size,fetched_at,text_chars,source_id,agency
                FROM documents
                WHERE id IN ({latest})
                ORDER BY (fetched_at IS NULL), fetched_at DESC, id DESC
                LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        )
        return {"total": total, "limit": limit, "offset": offset, "documents": [dict(r) for r in rows]}

    def facets(self) -> dict:
        latest = self._latest_id_sql("")
        mime_rows = self._rows(
            f"""SELECT COALESCE(mime,'(unknown)') AS mime, COUNT(*) AS n
                FROM documents WHERE id IN ({latest})
                GROUP BY 1 ORDER BY n DESC LIMIT 40"""
        )
        collections = self._rows(
            f"""SELECT COALESCE(NULLIF(source_id, ''), '(unassigned)') AS source_id, COUNT(*) AS n
                FROM documents WHERE id IN ({latest})
                GROUP BY 1 ORDER BY n DESC LIMIT 40"""
        )
        return {
            "mime": [{"value": r["mime"], "count": r["n"]} for r in mime_rows],
            "collections": [{"value": r["source_id"], "count": r["n"]} for r in collections],
        }

    def stats(self) -> dict:
        docs = self._scalar("SELECT COUNT(*) FROM documents") or 0
        chunks = self._scalar("SELECT COUNT(*) FROM chunks") or 0
        chars = self._scalar("SELECT COALESCE(SUM(text_chars),0) FROM documents") or 0
        crawl = {}
        crawl_path = self.s.state_dir / "crawl.sqlite"
        if crawl_path.exists():
            try:
                with closing(sqlite3.connect(f"file:{crawl_path}?mode=ro", uri=True)) as cdb:
                    crawl = {
                        r[0]: r[1]
                        for r in cdb.execute("SELECT status,COUNT(*) FROM frontier GROUP BY status")
                    }
            except Exception as exc:
                log.warning("crawl stats unavailable: %s", exc)
        return {
            "documents": docs,
            "chunks": chunks,
            "text_chars": chars,
            "semantic_search": self.semantic_available,
            "embedding_backend": self.s.embedding_backend,
            "embedding_model": (
                self.s.embedding_model
                if self.s.embedding_backend.lower() == "fastembed"
                else self.s.ollama_embed_model
            ),
            "frontier": crawl,
        }
