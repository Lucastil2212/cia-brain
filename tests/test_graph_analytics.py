from cia_brain.graph import KnowledgeGraph
from cia_brain.settings import Settings


def graph(tmp_path):
    s = Settings(data_dir=tmp_path, graph_ner="rules", _env_file=None)
    s.ensure_dirs()
    return KnowledgeGraph(s)


def document(char, text, **kwargs):
    return {
        "source_sha256": char * 64,
        "source_url": f"https://example.gov/{char}",
        "text": text,
        "fetched_at": "2026-09-21T00:00:00+00:00",
        **kwargs,
    }


def test_overview_and_analytics(tmp_path):
    g = graph(tmp_path)
    g.ingest(document("a", "CIA worked with NASA in Langley."))
    g.ingest(document("b", "NASA worked with NOAA near Washington."))
    g.ingest(document("c", "FBI and CIA reviewed the CRS briefing."))
    analytics = g.analytics(hub_limit=10)
    assert analytics["entities"] >= 3
    assert analytics["relations"]
    assert analytics["hubs"]
    overview = g.overview(limit=20)
    assert overview["nodes"]
    assert any(link["relation"] for link in overview["links"]) or overview["links"] == []


def test_shortest_path(tmp_path):
    g = graph(tmp_path)
    g.ingest(document("a", "CIA worked with NASA."))
    g.ingest(document("b", "NASA worked with NOAA."))
    entities = {e["label"]: e["id"] for e in g.entities()}
    cia = entities["Central Intelligence Agency"]
    noaa = entities["National Oceanic and Atmospheric Administration"]
    path = g.shortest_path(cia, noaa)
    assert path["hops"] == 2
    assert path["path"][0] == cia and path["path"][-1] == noaa


def test_entity_profile(tmp_path):
    g = graph(tmp_path)
    g.ingest(document("a", "CIA worked with NASA."))
    cia = g.entities("Central Intelligence")[0]["id"]
    profile = g.entity_profile(cia)
    assert profile["degree"] >= 1
    assert profile["neighbors"]
