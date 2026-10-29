from cia_brain.crawler import Frontier
from cia_brain.history import observations_for
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
