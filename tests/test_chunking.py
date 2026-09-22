from cia_brain.indexer import chunk_text


def test_chunk_text_overlap_and_termination():
    text = "A" * 5000
    chunks = chunk_text(text, 1000, 100)
    assert len(chunks) >= 5
    assert all(chunks)
    assert max(map(len, chunks)) <= 1000
