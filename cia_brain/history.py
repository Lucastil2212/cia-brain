"""Append-only URL captures and the citation fields stored with each document."""

from __future__ import annotations

import sqlite3

OBSERVATIONS = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  final_url TEXT,
  sha256 TEXT,
  status_code INTEGER NOT NULL,
  fetched_at TEXT NOT NULL,
  mime TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_url ON observations(url, id);
CREATE INDEX IF NOT EXISTS idx_observations_sha ON observations(sha256);
"""


def ensure_observations(db: sqlite3.Connection) -> None:
    db.executescript(OBSERVATIONS)
    db.commit()


def ensure_provenance_columns(db: sqlite3.Connection) -> None:
    cols = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
    if "source_id" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN source_id TEXT")
    if "agency" not in cols:
        db.execute("ALTER TABLE documents ADD COLUMN agency TEXT")
    db.commit()


def append_observation(
    db: sqlite3.Connection,
    *,
    url: str,
    final_url: str | None,
    sha256: str | None,
    status_code: int,
    fetched_at: str,
    mime: str | None = None,
) -> None:
    db.execute(
        """INSERT INTO observations(url, final_url, sha256, status_code, fetched_at, mime)
           VALUES(?,?,?,?,?,?)""",
        (url, final_url, sha256, status_code, fetched_at, mime),
    )


def observations_for(settings, sha256: str, urls: list[str] | None = None, limit: int = 40) -> list[dict]:
    """Captures of this hash or any of its source URLs, newest first."""
    path = settings.state_dir / "crawl.sqlite"
    if not path.exists():
        return []
    wanted = []
    seen = set()
    for url in urls or []:
        if url and url not in seen:
            seen.add(url)
            wanted.append(url)
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("SELECT 1 FROM observations LIMIT 1")
    except sqlite3.Error:
        return []
    clauses = ["sha256 = ?"]
    params: list = [sha256]
    if wanted:
        clauses.append(f"url IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    params.append(limit)
    try:
        rows = db.execute(
            f"""SELECT url, final_url, sha256, status_code, fetched_at, mime
                FROM observations
                WHERE {' OR '.join(clauses)}
                ORDER BY fetched_at DESC, id DESC
                LIMIT ?""",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [dict(row) for row in rows]


def citation_for(doc: dict) -> dict:
    return {
        "agency": doc.get("agency") or "",
        "collection": doc.get("source_id") or "",
        "title": doc.get("title") or "",
        "source_url": doc.get("source_url") or "",
        "source_sha256": doc.get("source_sha256") or "",
        "fetched_at": doc.get("fetched_at") or "",
        "chunk_no": doc.get("chunk_no"),
    }
