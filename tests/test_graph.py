import pytest

from cia_brain.graph import (
    KnowledgeGraph,
    extract_entities,
    extract_entities_rules,
    infer_relation,
    resolve_ner_method,
)
from cia_brain.settings import Settings


def graph(tmp_path, ner="rules"):
    s = Settings(data_dir=tmp_path, graph_ner=ner, _env_file=None)
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


def test_aliases_offsets_and_evidence(tmp_path):
    text = "CIA worked with NASA. Central Intelligence Agency reviewed the findings."
    entities = extract_entities_rules(text)
    cia = [e for e in entities if e["label"] == "Central Intelligence Agency"]
    assert len(cia) == 2 and cia[0]["id"] == cia[1]["id"]
    assert text[cia[0]["start"] : cia[0]["end"]] == "CIA"
    assert cia[0]["method"] == "alias"
    g = graph(tmp_path)
    g.ingest(document("a", text))
    g.ingest(document("a", text))
    assert g.stats()["edges"] == 1
    assert g.stats()["ner_backend"] == "rules"
    edge = g.neighborhood(cia[0]["id"])["edges"][0]
    assert edge["inferred"] and edge["relation"] == "worked_with"
    assert text[edge["start"] : edge["end"]] == edge["evidence"]


def test_rules_person_and_org_suffix():
    text = "Allen Dulles directed the National Reconnaissance Office from Langley."
    entities = extract_entities_rules(text)
    by_kind = {}
    for e in entities:
        by_kind.setdefault(e["kind"], set()).add(e["label"])
    assert "Allen Dulles" in by_kind.get("person", set())
    assert any("Office" in label for label in by_kind.get("organization", set()))
    assert "Langley" in by_kind.get("location", set())


def test_typed_relation_patterns():
    assert (
        infer_relation(
            "CIA worked with NASA.",
            "Central Intelligence Agency",
            "National Aeronautics and Space Administration",
        )
        == "worked_with"
    )
    assert (
        infer_relation(
            "Report released by ODNI yesterday.",
            "Report",
            "Office of the Director of National Intelligence",
        )
        == "released_by"
    )
    assert (
        infer_relation(
            "CIA and NASA met.",
            "Central Intelligence Agency",
            "National Aeronautics and Space Administration",
        )
        == "co_mentioned"
    )


def test_correlations_and_no_cross_sentence_edges(tmp_path):
    g = graph(tmp_path)
    g.ingest(document("a", "CIA worked with NASA."))
    g.ingest(document("b", "NASA worked with NOAA."))
    g.ingest(document("c", "FBI. USGS."))
    hits = g.correlations("a" * 64)["results"]
    assert len(hits) == 1 and hits[0]["sha"] == "b" * 64
    assert hits[0]["score"] == 1 / 3
    assert g.stats()["edges"] == 2
    assert g.stats()["typed_edges"] == 2


def test_spatial_correlations(tmp_path):
    g = graph(tmp_path)
    g.ingest(
        document(
            "a",
            "Quake near Boston.",
            geometry={"type": "Point", "coordinates": [-71.0, 42.3]},
        )
    )
    g.ingest(
        document(
            "b",
            "Quake near Providence.",
            geometry={"type": "Point", "coordinates": [-71.4, 41.8]},
        )
    )
    g.ingest(
        document(
            "c",
            "Quake near Tokyo.",
            geometry={"type": "Point", "coordinates": [139.7, 35.7]},
        )
    )
    hits = g.spatial_correlations("a" * 64, radius_km=100)["results"]
    assert len(hits) == 1 and hits[0]["sha"] == "b" * 64
    assert hits[0]["distance_km"] < 100


def test_feed_revision_replay_does_not_restore_stale_entities(tmp_path):
    g = graph(tmp_path)
    old = document("a", "CIA and NASA.", record_id="same", source_url="https://api.gov/#same")
    new = document(
        "b",
        "FBI and NOAA.",
        record_id="same",
        source_url=old["source_url"],
        fetched_at="2026-09-22T00:00:00+00:00",
    )
    g.ingest(new)
    g.ingest(old)
    assert not g.entities("Central Intelligence Agency")
    assert g.entities("Federal Bureau")


def test_parameterized_query(tmp_path):
    g = graph(tmp_path)
    g.ingest(document("a", "CIA and NASA."))
    assert g.entities("' OR 1=1 --") == []
    assert g.stats()["documents"] == 1


def test_odni_dia_aliases():
    text = "ODNI and DIA reviewed the CRS briefing."
    labels = {e["label"] for e in extract_entities_rules(text)}
    assert "Office of the Director of National Intelligence" in labels
    assert "Defense Intelligence Agency" in labels
    assert "Congressional Research Service" in labels


def test_resolve_ner_method_rules_forced():
    assert resolve_ner_method("rules") == "rules"


@pytest.mark.skipif(
    resolve_ner_method("spacy") != "spacy",
    reason="spaCy model not installed",
)
def test_spacy_ner_types_person_and_keeps_aliases():
    text = "Director Allen Dulles met CIA officials in Washington."
    entities = extract_entities(text, method="spacy")
    methods = {e["method"] for e in entities}
    assert "spacy-sm" in methods or "alias" in methods
    labels = {e["label"]: e["kind"] for e in entities}
    assert labels.get("Central Intelligence Agency") == "organization"
    # Person span should be typed when the model fires.
    persons = [e for e in entities if e["kind"] == "person"]
    assert persons
