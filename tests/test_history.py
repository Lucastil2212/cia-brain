from cia_brain.crawler import Frontier
from cia_brain.history import backfill_observations, observations_for, text_diff, version_chain
from cia_brain.models import FetchedEvent
from cia_brain.search import HybridSearcher
from cia_brain.settings import Settings


def test_observations_keep_every_capture(tmp_path):
    settings = Settings(data_dir=tmp_path, min_free_disk_gb=0, _env_file=None)
    settings.ensure_dirs()
    frontier = Frontier(settings.state_dir / "crawl.sqlite")
    url = "https://www.cia.gov/readingroom/document/1"
    first = FetchedEvent(url, url, "a" * 64, "/tmp/a", "text/html", 10, 200, "2026-01-01T00:00:00+00:00", 0)
    second = FetchedEvent(url, url, "b" * 64, "/tmp/b", "text/html", 12, 200, "2026-02-01T00:00:00+00:00", 0)
    frontier.save_download(first, {})
    frontier.save_download(second, {})
    frontier.note_not_modified(url, "2026-03-01T00:00:00+00:00")

    rows = frontier.db.execute(
        "SELECT sha256, status_code FROM observations ORDER BY id"
    ).fetchall()
    assert [row["sha256"] for row in rows] == ["a" * 64, "b" * 64, "b" * 64]
    assert [row["status_code"] for row in rows] == [200, 200, 304]
    assert frontier.db.execute("SELECT sha256 FROM downloads").fetchone()["sha256"] == "b" * 64

    listed = observations_for(settings, "b" * 64, [url])
    assert [item["fetched_at"] for item in listed] == [
        "2026-03-01T00:00:00+00:00",
        "2026-02-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    ]
    frontier.db.close()


def test_document_citation(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    settings.ensure_dirs()
    searcher = HybridSearcher(settings)
    digest = "c" * 64
    searcher.db.execute(
        """INSERT INTO documents(
             source_sha256, source_url, title, mime, fetched_at, text_chars, source_id, agency
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (
            digest,
            "https://www.cia.gov/readingroom/document/mkultra",
            "MKULTRA",
            "application/pdf",
            "2026-01-01T00:00:00+00:00",
            20,
            "cia",
            "CIA",
        ),
    )
    searcher.db.commit()
    doc = searcher.document(digest)
    assert doc["citation"] == {
        "agency": "CIA",
        "collection": "cia",
        "title": "MKULTRA",
        "source_url": "https://www.cia.gov/readingroom/document/mkultra",
        "source_sha256": digest,
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "chunk_no": None,
    }
    searcher.db.close()


def test_backfill_copies_downloads_that_predate_the_capture_log(tmp_path):
    settings = Settings(data_dir=tmp_path, min_free_disk_gb=0, _env_file=None)
    settings.ensure_dirs()
    frontier = Frontier(settings.state_dir / "crawl.sqlite")
    frontier.db.execute(
        """INSERT INTO downloads(url, final_url, sha256, path, mime, size, status_code, fetched_at, depth)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "https://www.cia.gov/readingroom/document/old",
            "https://www.cia.gov/readingroom/document/old",
            "d" * 64,
            "/tmp/old",
            "application/pdf",
            20,
            200,
            "2025-01-01T00:00:00+00:00",
            0,
        ),
    )
    frontier.db.commit()
    assert backfill_observations(frontier.db) == 1
    assert backfill_observations(frontier.db) == 0
    row = frontier.db.execute("SELECT sha256, status_code FROM observations").fetchone()
    assert row["sha256"] == "d" * 64
    assert row["status_code"] == 200
    frontier.db.close()


def test_retry_returns_a_done_url_to_the_queue(tmp_path):
    settings = Settings(data_dir=tmp_path, min_free_disk_gb=0, _env_file=None)
    settings.ensure_dirs()
    frontier = Frontier(settings.state_dir / "crawl.sqlite")
    url = "https://www.cia.gov/readingroom/document/1"
    frontier.add(url, 0, "seed")
    frontier.mark_done(url)
    frontier.retry(url, "publish failed", 1, delay=0)
    row = frontier.db.execute("SELECT status FROM frontier WHERE url=?", (url,)).fetchone()
    assert row["status"] == "queued"
    assert frontier.next()["url"] == url
    frontier.db.close()


def test_version_chain_marks_a_changed_hash():
    rows = version_chain(
        [
            {"url": "https://www.cia.gov/readingroom/a", "sha256": "a" * 64, "fetched_at": "2026-01-01", "status_code": 200},
            {"url": "https://www.cia.gov/readingroom/a", "sha256": "a" * 64, "fetched_at": "2026-02-01", "status_code": 304},
            {"url": "https://www.cia.gov/readingroom/a", "sha256": "b" * 64, "fetched_at": "2026-03-01", "status_code": 200},
        ]
    )
    by_time = sorted(rows, key=lambda item: item["fetched_at"])
    assert by_time[1]["changed"] is False
    assert by_time[2]["changed"] is True
    assert by_time[2]["previous_sha256"] == "a" * 64
    diff = text_diff("alpha\nkeep\n", "beta\nkeep\n")
    assert "-alpha" in diff and "+beta" in diff


def test_holdings_can_be_limited_to_one_collection(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    settings.ensure_dirs()
    searcher = HybridSearcher(settings)
    searcher.db.execute(
        """INSERT INTO documents(source_sha256, source_url, title, mime, fetched_at, text_chars, source_id, agency)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("a" * 64, "https://www.cia.gov/readingroom/a", "CIA memo", "text/html", "2026-01-01", 10, "cia", "CIA"),
    )
    searcher.db.execute(
        """INSERT INTO documents(source_sha256, source_url, title, mime, fetched_at, text_chars, source_id, agency)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("b" * 64, "https://vault.fbi.gov/b", "FBI file", "text/html", "2026-01-02", 10, "fbi", "FBI"),
    )
    searcher.db.commit()
    cia = searcher.list_documents(source_id="cia")
    assert cia["total"] == 1
    assert cia["documents"][0]["agency"] == "CIA"
    searcher.db.close()


def _insert_version(searcher, sha: str, url: str, fetched: str, text: str) -> None:
    searcher.db.execute(
        """INSERT INTO documents(
             source_sha256, source_url, title, mime, fetched_at, text_chars, source_id, agency
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (sha, url, "Memo", "text/html", fetched, len(text), "cia", "CIA"),
    )
    doc_id = searcher.db.execute(
        "SELECT id FROM documents WHERE source_sha256=?", (sha,)
    ).fetchone()[0]
    searcher.db.execute(
        "INSERT INTO chunks(document_id, chunk_no, text) VALUES (?,?,?)",
        (doc_id, 0, text),
    )
    searcher.db.commit()


def test_catalog_lists_the_current_capture_only(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    settings.ensure_dirs()
    searcher = HybridSearcher(settings)
    url = "https://www.cia.gov/readingroom/document/memo"
    old, new = "a" * 64, "b" * 64
    _insert_version(searcher, old, url, "2026-01-01T00:00:00+00:00", "mkultra memo alpha")
    _insert_version(searcher, new, url, "2026-06-01T00:00:00+00:00", "mkultra memo beta")
    listed = searcher.list_documents(source_id="cia")
    assert listed["total"] == 1
    assert listed["documents"][0]["source_sha256"] == new
    hits = searcher.search("mkultra", 10)
    assert {hit["source_sha256"] for hit in hits} == {new}
    older = searcher.search("mkultra", 10, sha256=old)
    assert older and older[0]["source_sha256"] == old
    assert searcher.latest_sha(url) == new
    searcher.db.close()


def test_normalized_documents_lists_stored_hashes(tmp_path):
    import gzip
    import json

    from cia_brain.indexer import normalized_documents

    settings = Settings(data_dir=tmp_path, _env_file=None)
    settings.ensure_dirs()
    digest = "ab" * 32
    path = settings.normalized_dir / digest[:2] / f"{digest}.json.gz"
    path.parent.mkdir(parents=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump({"source_sha256": digest, "text": "memo"}, stream)
    junk = settings.normalized_dir / "zz" / "not-a-hash.json.gz"
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"nope")
    found = normalized_documents(settings)
    assert [item[0] for item in found] == [digest]


def test_redirect_keeps_one_current_capture(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    settings.ensure_dirs()
    searcher = HybridSearcher(settings)
    requested = "https://www.cia.gov/readingroom/document/memo"
    searcher.db.execute(
        """INSERT INTO documents(
             source_sha256, source_url, requested_url, title, mime, fetched_at, text_chars, source_id, agency
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("a" * 64, "https://www.cia.gov/readingroom/document/old-target", requested, "Old", "text/html", "2026-01-01T00:00:00+00:00", 4, "cia", "CIA"),
    )
    searcher.db.execute(
        """INSERT INTO documents(
             source_sha256, source_url, requested_url, title, mime, fetched_at, text_chars, source_id, agency
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        ("b" * 64, "https://www.cia.gov/readingroom/document/new-target", requested, "New", "text/html", "2026-06-01T00:00:00+00:00", 4, "cia", "CIA"),
    )
    searcher.db.commit()
    listed = searcher.list_documents()
    assert listed["total"] == 1
    assert listed["documents"][0]["source_sha256"] == "b" * 64
    assert searcher.latest_sha(requested) == "b" * 64
    searcher.db.close()


def test_tagged_document_is_not_counted_in_another_collection(tmp_path):
    settings = Settings(data_dir=tmp_path, _env_file=None)
    settings.ensure_dirs()
    searcher = HybridSearcher(settings)
    searcher.db.execute(
        """INSERT INTO documents(
             source_sha256, source_url, title, mime, fetched_at, text_chars, source_id, agency
           ) VALUES (?,?,?,?,?,?,?,?)""",
        ("c" * 64, "https://www.cia.gov/readingroom/document/shared", "File", "text/html", "2026-01-01T00:00:00+00:00", 4, "fbi", "FBI"),
    )
    searcher.db.commit()
    assert searcher.list_documents(source_id="cia")["total"] == 0
    assert searcher.list_documents(source_id="fbi")["total"] == 1
    searcher.db.close()


def test_document_page_shows_a_byte_change():
    from cia_brain.api import _document_page

    page = _document_page(
        {
            "source_sha256": "b" * 64,
            "mime": "text/html",
            "text": "beta",
            "citation": {
                "agency": "CIA",
                "collection": "cia",
                "title": "Memo",
                "source_url": "https://www.cia.gov/readingroom/document/1",
                "source_sha256": "b" * 64,
                "fetched_at": "2026-03-01T00:00:00+00:00",
            },
            "versions": [
                {
                    "fetched_at": "2026-03-01T00:00:00+00:00",
                    "status_code": 200,
                    "sha256": "b" * 64,
                    "changed": True,
                }
            ],
            "text_diff": "--- previous\n+++ current\n-alpha\n+beta\n",
            "chunks": [],
        }
    )
    assert "bytes changed" in page
    assert "Change from previous capture" in page
    assert "CIA" in page
    assert f"/d/" not in page or "Memo" in page
