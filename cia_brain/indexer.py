from __future__ import annotations

import gzip
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

import numpy as np
import pandas as pd
from usearch.index import Index

from .embedding import Embedder
from .settings import Settings

log = logging.getLogger(__name__)


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if overlap >= size:
        overlap = max(0, size // 5)
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            pivot = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end))
            if pivot > start + size // 2:
                end = pivot + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


class SearchStore:
    def __init__(self, settings: Settings, embedder: Embedder | None = None):
        self.s = settings
        self.db_path = settings.state_dir / "search.sqlite"
        self.vector_path = settings.state_dir / "vectors.usearch"
        self.meta_path = settings.state_dir / "vector-meta.json"
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
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
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(documents)")}
        if "source_urls_json" not in cols:
            self.db.execute("ALTER TABLE documents ADD COLUMN source_urls_json TEXT NOT NULL DEFAULT '[]'")
        self.db.commit()
        self.embedder = embedder or Embedder(settings)
        self.lock = threading.RLock()
        self.index = self._load_or_new_index()
        self._docs_since_compact = 0
        self._docs_since_persist = 0
        self._dirty_vectors = False

    def _load_or_new_index(self) -> Index:
        dim = self.embedder.dimension
        if self.meta_path.exists() and self.vector_path.exists():
            try:
                meta = json.loads(self.meta_path.read_text())
                if meta.get("model") == self.embedder.model_name and int(meta.get("dim")) == dim:
                    idx = Index(ndim=dim, metric="cos", dtype="f32")
                    idx.load(str(self.vector_path))
                    return idx
            except Exception as exc:
                log.warning("vector index reload failed; rebuilding required: %s", exc)
        return Index(ndim=dim, metric="cos", dtype="f32")

    def _persist_index(self) -> None:
        tmp = self.vector_path.with_suffix(".tmp")
        self.index.save(str(tmp))
        os.replace(tmp, self.vector_path)
        meta = {"model": self.embedder.model_name, "dim": self.embedder.dimension, "backend": self.embedder.backend}
        self.meta_path.write_text(json.dumps(meta, indent=2))

    def index_normalized(self, normalized_path: str, compact: bool = True) -> None:
        with gzip.open(normalized_path, "rt", encoding="utf-8") as f:
            doc = json.load(f)
        chunks = chunk_text(doc.get("text", ""), self.s.chunk_chars, self.s.chunk_overlap)
        with self.lock:
            row = self.db.execute("SELECT id FROM documents WHERE source_sha256=?", (doc["source_sha256"],)).fetchone()
            if row:
                document_id = int(row["id"])
                old_ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,))]
                for key in old_ids:
                    try:
                        self.index.remove(int(key))
                    except Exception:
                        pass
                self.db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
                self.db.execute(
                    """UPDATE documents SET source_url=?,source_urls_json=?,requested_url=?,title=?,mime=?,size=?,fetched_at=?,
                       extracted_at=?,text_chars=? WHERE id=?""",
                    (
                        doc["source_url"], json.dumps(doc.get("source_urls", [doc["source_url"]])),
                        doc.get("requested_url"), doc.get("title"), doc.get("mime"), doc.get("size"),
                        doc.get("fetched_at"), doc.get("extracted_at"), doc.get("text_chars"), document_id,
                    ),
                )
            else:
                cur = self.db.execute(
                    """INSERT INTO documents(source_sha256,source_url,source_urls_json,requested_url,title,mime,size,fetched_at,extracted_at,text_chars)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        doc["source_sha256"], doc["source_url"], json.dumps(doc.get("source_urls", [doc["source_url"]])),
                        doc.get("requested_url"), doc.get("title"), doc.get("mime"), doc.get("size"),
                        doc.get("fetched_at"), doc.get("extracted_at"), doc.get("text_chars"),
                    ),
                )
                document_id = int(cur.lastrowid)
            ids: list[int] = []
            for i, chunk in enumerate(chunks):
                cur = self.db.execute(
                    "INSERT INTO chunks(document_id,chunk_no,text) VALUES(?,?,?)",
                    (document_id, i, chunk),
                )
                ids.append(int(cur.lastrowid))
            self.db.commit()
            if chunks:
                batch_size = max(1, self.s.embed_batch_size)
                for start in range(0, len(chunks), batch_size):
                    batch = chunks[start:start + batch_size]
                    batch_ids = ids[start:start + batch_size]
                    vectors = self.embedder.embed(["passage: " + c for c in batch])
                    self.index.add(np.asarray(batch_ids, dtype=np.uint64), vectors)
                self._docs_since_persist += 1
                self._dirty_vectors = True
                if self._docs_since_persist >= max(1, self.s.vector_persist_every):
                    self._persist_index()
                    self._docs_since_persist = 0
                    self._dirty_vectors = False
        self._docs_since_compact += 1
        if compact and self._docs_since_compact >= max(1, self.s.parquet_compact_every):
            self.flush()
            self.compact_parquet()
            self._docs_since_compact = 0
        log.info("indexed %s chunks from %s", len(chunks), doc["source_url"])

    def flush(self) -> None:
        """Persist any dirty vector index state to disk."""
        with self.lock:
            if self._dirty_vectors:
                self._persist_index()
                self._docs_since_persist = 0
                self._dirty_vectors = False

    def compact_parquet(self) -> None:
        out = self.s.parquet_dir / "documents.parquet"
        chunks_out = self.s.parquet_dir / "chunks.parquet"
        try:
            docs = pd.read_sql_query("SELECT * FROM documents", self.db)
            chunks = pd.read_sql_query("SELECT * FROM chunks", self.db)
            docs.to_parquet(out, index=False, compression="zstd")
            chunks.to_parquet(chunks_out, index=False, compression="zstd")
        except Exception as exc:
            log.warning("parquet compaction skipped: %s", exc)

    def rebuild(self) -> None:
        with self.lock:
            self.db.execute("DELETE FROM chunks")
            self.db.execute("DELETE FROM documents")
            self.db.commit()
            self.index = Index(ndim=self.embedder.dimension, metric="cos", dtype="f32")
            self.vector_path.unlink(missing_ok=True)
            for path in sorted(self.s.normalized_dir.glob("*/*.json.gz")):
                self.index_normalized(str(path), compact=False)
            self.flush()
            self.compact_parquet()
