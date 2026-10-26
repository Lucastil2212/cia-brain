"""Failure-mode tests: concurrency, unreachable stores, and unwritable paths."""

from concurrent.futures import ThreadPoolExecutor

from cia_brain import db, ratelimit
from cia_brain.search import HybridSearcher
from cia_brain.settings import Settings


def _seeded_searcher(tmp_path) -> HybridSearcher:
    s = Settings(data_dir=tmp_path, _env_file=None)
    s.ensure_dirs()
    h = HybridSearcher(s)
    for i in range(200):
        h.db.execute(
            "INSERT INTO documents(source_sha256,source_url,title,mime,fetched_at,text_chars) "
            "VALUES (?,?,?,?,?,?)",
            (f"{i:064x}", f"https://example.gov/{i}", f"Memo {i}", "text/html", "2026-09-01", 40),
        )
        doc_id = h.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        h.db.execute(
            "INSERT INTO chunks(document_id,chunk_no,text) VALUES (?,?,?)",
            (doc_id, 0, f"soviet satellite cable berlin memo {i}"),
        )
    h.db.commit()
    return h


def test_searcher_survives_concurrent_requests(tmp_path):
    h = _seeded_searcher(tmp_path)
    calls = [
        h.stats,
        h.facets,
        lambda: h.list_documents(limit=40),
        lambda: h.search("soviet satellite", 10),
        lambda: h.document(f"{7:064x}"),
    ]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda i: calls[i % len(calls)](), range(400)))
    assert len(results) == 400
    assert h.stats()["documents"] == 200


def test_lexical_search_without_vector_index(tmp_path):
    h = _seeded_searcher(tmp_path)
    results = h.search("berlin", 5)
    assert results
    assert all("berlin" in r["text"] for r in results)


def test_rate_limit_falls_back_to_memory_when_database_is_down(tmp_path, monkeypatch):
    s = Settings(
        data_dir=tmp_path,
        database_url="postgresql://u:p@127.0.0.1:1/x",
        jwt_secret="a-strong-unique-secret-value-1234567890",
        _env_file=None,
    )

    def unavailable(*_args, **_kwargs):
        raise db.DatabaseUnavailable("account database unavailable: PoolTimeout")

    monkeypatch.setattr(ratelimit, "_db_check", unavailable)
    for _ in range(2):
        ok, _headers = ratelimit.check_rate_limit("ip:down:m", limit=2, window_seconds=60, settings=s)
        assert ok
    ok, headers = ratelimit.check_rate_limit("ip:down:m", limit=2, window_seconds=60, settings=s)
    assert not ok
    assert headers["X-RateLimit-Remaining"] == "0"


def test_disabled_database_reports_generic_problem(monkeypatch):
    monkeypatch.setattr(db, "_disabled_reason", None)
    db.disable_database("JWT_SECRET must be a strong unique value")
    problem = db.database_problem()
    assert problem
    assert "JWT_SECRET" not in problem


def test_unwritable_data_dir_falls_back_to_temp(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    s = Settings(data_dir=blocked / "lake", _env_file=None)
    s.ensure_dirs()
    assert s.data_dir == tmp_path / "cia-brain"
    assert s.state_dir.is_dir()
