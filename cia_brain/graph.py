"""Local, evidence-bearing graph. Extracted links are hypotheses, not factual relations."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import sqlite3
from contextlib import contextmanager

ALIASES = {
    "CIA": ("Central Intelligence Agency", "organization"),
    "FBI": ("Federal Bureau of Investigation", "organization"),
    "NSA": ("National Security Agency", "organization"),
    "DIA": ("Defense Intelligence Agency", "organization"),
    "ODNI": ("Office of the Director of National Intelligence", "organization"),
    "DNI": ("Office of the Director of National Intelligence", "organization"),
    "NARA": ("National Archives and Records Administration", "organization"),
    "CRS": ("Congressional Research Service", "organization"),
    "NASA": ("National Aeronautics and Space Administration", "organization"),
    "NOAA": ("National Oceanic and Atmospheric Administration", "organization"),
    "USGS": ("United States Geological Survey", "organization"),
    "NWS": ("National Weather Service", "organization"),
    "FEMA": ("Federal Emergency Management Agency", "organization"),
    "DHS": ("Department of Homeland Security", "organization"),
    "DoD": ("Department of Defense", "organization"),
    "DOD": ("Department of Defense", "organization"),
    "DOS": ("Department of State", "organization"),
    "State Department": ("Department of State", "organization"),
    "USA": ("United States", "location"),
    "U.S.": ("United States", "location"),
}
ENTITY_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b[A-Z][A-Z0-9-]{1,14}\b|"
    r"\b(?:[A-Z][a-z]+(?:\s+|$)){2,5}|\bU\.S\."
)
ORG_SUFFIX_RE = re.compile(
    r"\b(?:[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,4}\s+)?"
    r"(?:Agency|Department|Committee|Bureau|Office|Administration|Service|"
    r"Institute|Center|Centre|Commission|Directorate|Authority|Council)\b"
)
PERSON_RE = re.compile(r"\b([A-Z][a-z]{1,20})\s+([A-Z][a-z]{1,20})\b")
PERSON_STOP = {
    "the", "new", "united", "national", "central", "federal", "general", "state",
    "north", "south", "east", "west", "project", "operation", "director", "office",
}
LOCATION_GAZETTEER = {
    "Washington": "location",
    "Washington, D.C.": "location",
    "New York": "location",
    "Langley": "location",
    "Fort Meade": "location",
    "Moscow": "location",
    "Beijing": "location",
    "Havana": "location",
    "Tehran": "location",
    "Baghdad": "location",
    "Kabul": "location",
    "Saigon": "location",
    "Berlin": "location",
    "London": "location",
    "Paris": "location",
    "Tokyo": "location",
    "California": "location",
    "Virginia": "location",
    "Maryland": "location",
    "Texas": "location",
    "Alaska": "location",
    "Hawaii": "location",
}
SENTENCE_RE = re.compile(r"[^\n.!?]+(?:[.!?](?!\s+[a-z]))?")

# Ordered patterns: first matching typed relation wins for a subject/object pair in a sentence.
RELATION_PATTERNS = (
    (re.compile(r"\b(?:worked with|collaborated with|partnered with|cooperated with)\b", re.I), "worked_with"),
    (re.compile(r"\b(?:released by|published by|issued by|declassified by)\b", re.I), "released_by"),
    (re.compile(r"\b(?:reported by|according to)\b", re.I), "reported_by"),
    (re.compile(r"\b(?:located in|based in|occurred in|near)\b", re.I), "located_in"),
    (re.compile(r"\b(?:transferred to|provided to|shared with)\b", re.I), "transferred_to"),
)

SPACY_LABELS = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "location",
    "LOC": "location",
    "FAC": "location",
    "DATE": "date",
    "EVENT": "event",
    "NORP": "group",
}

_spacy_nlp = None
_spacy_failed = False


def entity_id(label: str, kind: str) -> str:
    return hashlib.sha256(f"{kind}:{label.casefold()}".encode()).hexdigest()[:24]


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    a, b = span
    return any(a < y and b > x for x, y in occupied)


def _alias_mentions(text: str) -> list[tuple]:
    occupied: list[tuple[int, int]] = []
    candidates = []
    aliases = sorted(ALIASES.items(), key=lambda kv: -max(len(kv[0]), len(kv[1][0])))
    for short, (label, kind) in aliases:
        for match in re.finditer(
            r"(?<!\w)(?:" + re.escape(label) + "|" + re.escape(short) + r")(?!\w)",
            text,
            re.IGNORECASE,
        ):
            if _overlaps(match.span(), occupied):
                continue
            occupied.append(match.span())
            candidates.append((match.start(), match.end(), label, kind, 0.95, "alias"))
    for place, kind in sorted(LOCATION_GAZETTEER.items(), key=lambda kv: -len(kv[0])):
        for match in re.finditer(r"(?<!\w)" + re.escape(place) + r"(?!\w)", text):
            if _overlaps(match.span(), occupied):
                continue
            occupied.append(match.span())
            candidates.append((match.start(), match.end(), place, kind, 0.9, "gazetteer"))
    return candidates


def extract_entities_rules(text: str) -> list[dict]:
    """Deterministic offline candidates with exact character offsets."""
    candidates = _alias_mentions(text)
    occupied = [(a, b) for a, b, *_ in candidates]
    for match in ORG_SUFFIX_RE.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        label = match.group().strip()
        occupied.append(match.span())
        candidates.append((match.start(), match.end(), label, "organization", 0.75, "rules-v3"))
    for match in PERSON_RE.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        first, last = match.group(1), match.group(2)
        if first.casefold() in PERSON_STOP or last.casefold() in PERSON_STOP:
            continue
        occupied.append(match.span())
        candidates.append(
            (match.start(), match.end(), f"{first} {last}", "person", 0.65, "rules-v3")
        )
    for match in ENTITY_RE.finditer(text):
        if _overlaps(match.span(), occupied):
            continue
        label = match.group().strip()
        kind = "date" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", label) else "named_entity"
        end = match.start() + len(match.group().rstrip())
        occupied.append((match.start(), end))
        candidates.append(
            (match.start(), end, label, kind, 0.55, "rules-v3")
        )
    return _finalize(candidates)


def _load_spacy():
    global _spacy_nlp, _spacy_failed
    if _spacy_nlp is not None or _spacy_failed:
        return _spacy_nlp
    try:
        import spacy

        try:
            _spacy_nlp = spacy.load("en_core_web_sm", disable=["tagger", "parser", "lemmatizer"])
        except OSError:
            from spacy.cli import download

            download("en_core_web_sm")
            _spacy_nlp = spacy.load("en_core_web_sm", disable=["tagger", "parser", "lemmatizer"])
        if "ner" not in _spacy_nlp.pipe_names:
            _spacy_failed = True
            _spacy_nlp = None
    except Exception:
        _spacy_failed = True
        _spacy_nlp = None
    return _spacy_nlp


def extract_entities_spacy(text: str) -> list[dict]:
    """Local spaCy NER; agency aliases still override overlapping spans."""
    nlp = _load_spacy()
    if nlp is None:
        return extract_entities_rules(text)
    # spaCy has a practical doc length ceiling; chunk long FOIA text.
    max_chars = 900_000
    chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)] or [text]
    spacy_hits = []
    offset = 0
    for chunk in chunks:
        doc = nlp(chunk)
        for ent in doc.ents:
            kind = SPACY_LABELS.get(ent.label_)
            if not kind:
                continue
            label = ent.text.strip()
            if not label or len(label) < 2:
                continue
            start = offset + ent.start_char
            end = offset + ent.end_char
            spacy_hits.append((start, end, label, kind, 0.8, "spacy-sm"))
        offset += len(chunk)
    aliases = _alias_mentions(text)
    occupied = [(a, b) for a, b, *_ in aliases]
    merged = list(aliases)
    for hit in spacy_hits:
        if _overlaps((hit[0], hit[1]), occupied):
            continue
        occupied.append((hit[0], hit[1]))
        merged.append(hit)
    # Fill residual acronyms/dates the model often misses.
    for match in re.finditer(r"\b\d{4}-\d{2}-\d{2}\b|\b[A-Z]{2,8}\b", text):
        if _overlaps(match.span(), occupied):
            continue
        label = match.group()
        kind = "date" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", label) else "named_entity"
        if kind == "named_entity" and label in ALIASES:
            continue
        occupied.append(match.span())
        merged.append((match.start(), match.end(), label, kind, 0.55, "rules-v3"))
    return _finalize(merged)


def _finalize(candidates: list[tuple]) -> list[dict]:
    return sorted(
        [
            {
                "id": entity_id(label, kind),
                "label": label,
                "kind": kind,
                "start": start,
                "end": end,
                "confidence": confidence,
                "method": method,
            }
            for start, end, label, kind, confidence, method in candidates
        ],
        key=lambda x: x["start"],
    )


def resolve_ner_method(requested: str = "auto") -> str:
    name = (requested or "auto").strip().lower()
    if name == "rules":
        return "rules"
    if name == "spacy":
        return "spacy" if _load_spacy() is not None else "rules"
    if name == "auto":
        return "spacy" if _load_spacy() is not None else "rules"
    raise ValueError(f"Unknown graph NER method: {requested}")


def extract_entities(text: str, method: str = "auto") -> list[dict]:
    """Extract entities via spaCy when available, otherwise deterministic rules."""
    resolved = resolve_ner_method(method)
    if resolved == "spacy":
        return extract_entities_spacy(text)
    return extract_entities_rules(text)


def infer_relation(sentence: str, subject_label: str, object_label: str) -> str:
    """Pattern-based relation type; defaults to co_mentioned."""
    for pattern, name in RELATION_PATTERNS:
        match = pattern.search(sentence)
        if not match:
            continue
        # Prefer subject ... relation ... object word order when both appear.
        subj = sentence.casefold().find(subject_label.casefold())
        obj = sentence.casefold().find(object_label.casefold())
        if subj >= 0 and obj >= 0 and subj < match.start() < obj:
            return name
        if obj >= 0 and subj >= 0 and obj < match.start() < subj:
            # Reverse directional labels where the pattern implies direction.
            if name.endswith("_by"):
                return name
            if name == "transferred_to":
                return "transferred_to"
            if name == "located_in":
                return "located_in"
            return name
        return name
    return "co_mentioned"


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def point_coords(geometry) -> tuple[float, float] | None:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        return None
    coords = geometry.get("coordinates") or []
    if len(coords) < 2:
        return None
    lon, lat = float(coords[0]), float(coords[1])
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


class KnowledgeGraph:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.state_dir / "graph.sqlite"
        self.ner_method = getattr(settings, "graph_ner", "auto")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents(
                    sha TEXT PRIMARY KEY, url TEXT, title TEXT, fetched_at TEXT,
                    published_at TEXT, source_id TEXT, metadata TEXT, active INTEGER DEFAULT 1);
                CREATE INDEX IF NOT EXISTS doc_url ON documents(url);
                CREATE TABLE IF NOT EXISTS entities(
                    id TEXT PRIMARY KEY, label TEXT, kind TEXT);
                CREATE TABLE IF NOT EXISTS mentions(
                    sha TEXT, entity TEXT, start INTEGER, end INTEGER, confidence REAL,
                    PRIMARY KEY(sha,entity,start));
                CREATE INDEX IF NOT EXISTS mention_entity ON mentions(entity,sha);
            """)
            self._migrate_edges(db)

    def _migrate_edges(self, db):
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='edges'"
        ).fetchone()
        if row is None:
            db.execute("""
                CREATE TABLE edges(
                    sha TEXT, subject TEXT, object TEXT, relation TEXT, start INTEGER, end INTEGER,
                    evidence TEXT, PRIMARY KEY(sha,subject,object,relation,start));
                """)
            db.execute("CREATE INDEX IF NOT EXISTS edge_subject ON edges(subject)")
            db.execute("CREATE INDEX IF NOT EXISTS edge_object ON edges(object)")
            db.execute("CREATE INDEX IF NOT EXISTS edge_relation ON edges(relation)")
            return
        cols = {r[1] for r in db.execute("PRAGMA table_info(edges)")}
        if "relation" in cols:
            return
        db.execute("ALTER TABLE edges RENAME TO edges_legacy")
        db.execute("""
            CREATE TABLE edges(
                sha TEXT, subject TEXT, object TEXT, relation TEXT, start INTEGER, end INTEGER,
                evidence TEXT, PRIMARY KEY(sha,subject,object,relation,start));
        """)
        db.execute("""
            INSERT INTO edges(sha,subject,object,relation,start,end,evidence)
            SELECT sha,subject,object,'co_mentioned',start,end,evidence FROM edges_legacy
        """)
        db.execute("DROP TABLE edges_legacy")
        db.execute("CREATE INDEX IF NOT EXISTS edge_subject ON edges(subject)")
        db.execute("CREATE INDEX IF NOT EXISTS edge_object ON edges(object)")
        db.execute("CREATE INDEX IF NOT EXISTS edge_relation ON edges(relation)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, doc: dict):
        text = doc.get("text", "")[:2_000_000]
        sha = doc["source_sha256"]
        resolved = resolve_ner_method(self.ner_method)
        entities = extract_entities(text, method=resolved)
        methods = sorted({e["method"] for e in entities}) or [resolved]
        metadata = {
            k: doc[k]
            for k in (
                "geometry",
                "media",
                "source_urls",
                "record_id",
                "snapshot_sha256",
                "measurements",
            )
            if k in doc
        }
        metadata.update(
            extractor="+".join(methods),
            ner_backend=resolved,
            analyzed_chars=len(text),
            truncated=len(doc.get("text", "")) > len(text),
        )
        with self.connect() as db:
            active = 1
            if doc.get("record_id"):
                newer = db.execute(
                    "SELECT 1 FROM documents WHERE url=? AND fetched_at>? LIMIT 1",
                    (doc["source_url"], doc.get("fetched_at", "")),
                ).fetchone()
                active = int(newer is None)
                if active:
                    db.execute("UPDATE documents SET active=0 WHERE url=?", (doc["source_url"],))
            db.execute(
                "INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?)",
                (
                    sha,
                    doc["source_url"],
                    doc.get("title", ""),
                    doc.get("fetched_at"),
                    doc.get("published_at"),
                    doc.get("source_id"),
                    json.dumps(metadata),
                    active,
                ),
            )
            db.execute("DELETE FROM mentions WHERE sha=?", (sha,))
            db.execute("DELETE FROM edges WHERE sha=?", (sha,))
            by_id = {}
            for e in entities:
                by_id[e["id"]] = e
                db.execute(
                    "INSERT OR IGNORE INTO entities VALUES(?,?,?)", (e["id"], e["label"], e["kind"])
                )
                db.execute(
                    "INSERT OR IGNORE INTO mentions VALUES(?,?,?,?,?)",
                    (sha, e["id"], e["start"], e["end"], e["confidence"]),
                )
            cursor = 0
            for sentence in SENTENCE_RE.finditer(text):
                while cursor < len(entities) and entities[cursor]["start"] < sentence.start():
                    cursor += 1
                local = []
                pos = cursor
                while pos < len(entities) and entities[pos]["start"] < sentence.end():
                    if entities[pos]["kind"] != "date":
                        local.append(entities[pos]["id"])
                    pos += 1
                sent = sentence.group()
                for a, b in itertools.combinations(sorted(set(local))[:20], 2):
                    relation = infer_relation(sent, by_id[a]["label"], by_id[b]["label"])
                    db.execute(
                        "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?,?,?)",
                        (sha, a, b, relation, sentence.start(), sentence.end(), sent[:2000]),
                    )

    def entities(self, query="", limit=50):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    """
                SELECT e.*, count(DISTINCT m.sha) AS documents FROM entities e
                JOIN mentions m ON m.entity=e.id JOIN documents d ON d.sha=m.sha
                WHERE d.active=1 AND instr(lower(e.label),lower(?))>0
                GROUP BY e.id ORDER BY documents DESC,e.label LIMIT ?
            """,
                    (query, limit),
                )
            ]

    def neighborhood(self, entity: str, limit=100):
        with self.connect() as db:
            edges = [
                dict(r) | {"inferred": True}
                for r in db.execute(
                    """
                SELECT x.*, a.label AS subject_label,b.label AS object_label,d.url,d.title
                FROM edges x JOIN entities a ON a.id=x.subject JOIN entities b ON b.id=x.object
                JOIN documents d ON d.sha=x.sha
                WHERE d.active=1 AND (x.subject=? OR x.object=?)
                ORDER BY d.fetched_at DESC LIMIT ?
            """,
                    (entity, entity, limit),
                )
            ]
            mentions = [
                dict(r)
                for r in db.execute(
                    """
                SELECT m.*,d.url,d.title FROM mentions m JOIN documents d ON m.sha=d.sha
                WHERE m.entity=? AND d.active=1 LIMIT ?
            """,
                    (entity, limit),
                )
            ]
            return {"entity": entity, "edges": edges, "mentions": mentions}

    def correlations(self, sha: str, limit=20):
        with self.connect() as db:
            rows = db.execute(
                """
                WITH target AS (SELECT DISTINCT entity FROM mentions WHERE sha=?),
                sizes AS (SELECT sha,count(DISTINCT entity) n FROM mentions GROUP BY sha)
                SELECT d.sha,d.url,d.title,count(DISTINCT m.entity) shared_entities,
                    group_concat(DISTINCT e.label) AS evidence_entities,
                    CAST(count(DISTINCT m.entity) AS REAL) /
                    ((SELECT count(*) FROM target)+sizes.n-count(DISTINCT m.entity)) AS score
                FROM target t JOIN mentions m ON m.entity=t.entity
                JOIN entities e ON e.id=t.entity JOIN documents d ON d.sha=m.sha
                JOIN sizes ON sizes.sha=d.sha
                WHERE m.sha<>? AND d.active=1 AND e.kind<>'date'
                GROUP BY d.sha ORDER BY score DESC,shared_entities DESC LIMIT ?
            """,
                (sha, sha, limit),
            ).fetchall()
            return {
                "sha256": sha,
                "method": "shared-entity Jaccard; candidate correlation, not causation",
                "results": [dict(r) for r in rows],
            }

    def spatial_correlations(self, sha: str, limit=20, radius_km=250.0):
        """Haversine proximity for documents that store Point geometries."""
        with self.connect() as db:
            row = db.execute(
                "SELECT sha,url,title,metadata FROM documents WHERE sha=? AND active=1", (sha,)
            ).fetchone()
            if row is None:
                return {
                    "sha256": sha,
                    "method": f"haversine ≤{radius_km}km; candidate proximity, not causation",
                    "results": [],
                }
            origin = point_coords(json.loads(row["metadata"]).get("geometry"))
            if origin is None:
                return {
                    "sha256": sha,
                    "method": f"haversine ≤{radius_km}km; candidate proximity, not causation",
                    "results": [],
                    "note": "document has no Point geometry",
                }
            lon1, lat1 = origin
            hits = []
            for other in db.execute(
                "SELECT sha,url,title,metadata FROM documents WHERE active=1 AND sha<>?", (sha,)
            ):
                meta = json.loads(other["metadata"] or "{}")
                coords = point_coords(meta.get("geometry"))
                if coords is None:
                    continue
                distance = haversine_km(lon1, lat1, coords[0], coords[1])
                if distance <= radius_km:
                    hits.append(
                        {
                            "sha": other["sha"],
                            "url": other["url"],
                            "title": other["title"],
                            "distance_km": round(distance, 3),
                            "score": round(1.0 / (1.0 + distance), 6),
                        }
                    )
            hits.sort(key=lambda x: x["distance_km"])
            return {
                "sha256": sha,
                "method": f"haversine ≤{radius_km}km; candidate proximity, not causation",
                "origin": {"lon": lon1, "lat": lat1},
                "results": hits[:limit],
            }

    def document(self, sha: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE sha=?", (sha,)).fetchone()
            if row is None:
                return None
            doc = dict(row)
            doc["metadata"] = json.loads(doc["metadata"])
            doc["entities"] = [
                dict(r)
                for r in db.execute(
                    "SELECT e.*, m.start, m.end, m.confidence FROM mentions m "
                    "JOIN entities e ON e.id=m.entity WHERE m.sha=? ORDER BY m.start LIMIT 500",
                    (sha,),
                )
            ]
            return doc

    def stats(self):
        with self.connect() as db:
            base = {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("documents", "entities", "mentions", "edges")
            }
            typed = db.execute(
                "SELECT count(*) FROM edges WHERE relation<>'co_mentioned'"
            ).fetchone()[0]
            base["typed_edges"] = typed
            base["ner_backend"] = resolve_ner_method(self.ner_method)
            return base
