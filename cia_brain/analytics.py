from __future__ import annotations

import duckdb

from .settings import Settings


def query(settings: Settings, sql: str) -> list[dict]:
    docs = str(settings.parquet_dir / "documents.parquet").replace("'", "''")
    chunks = str(settings.parquet_dir / "chunks.parquet").replace("'", "''")
    con = duckdb.connect(database=":memory:", read_only=False)
    try:
        con.execute(f"CREATE VIEW documents AS SELECT * FROM read_parquet('{docs}')")
        con.execute(f"CREATE VIEW chunks AS SELECT * FROM read_parquet('{chunks}')")
        rel = con.execute(sql)
        cols = [x[0] for x in rel.description]
        return [dict(zip(cols, row)) for row in rel.fetchall()]
    finally:
        con.close()
