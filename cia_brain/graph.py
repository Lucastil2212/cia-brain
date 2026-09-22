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
SENTENCE_RE = re.compile(r"[^\n.!?]+(?:[.!?](?!\s+[a-z]))?")

# Ordered patterns: first matching typed relation wins for a subject/object pair in a sentence.
RELATION_PATTERNS = (
    (re.compile(r"\b(?:worked with|collaborated with|partnered with|cooperated with)\b", re.I), "worked_with"),
    (re.compile(r"\b(?:released by|published by|issued by|declassified by)\b", re.I), "released_by"),
    (re.compile(r"\b(?:reported by|according to)\b", re.I), "reported_by"),
    (re.compile(r"\b(?:located in|based in|occurred in|near)\b", re.I), "located_in"),
    (re.compile(r"\b(?:transferred to|provided to|shared with)\b", re.I), "transferred_to"),
)


def entity_id(label: str, kind: str) -> str:
    return hashlib.sha256(f"{kind}:{label.casefold()}".encode()).hexdigest()[:24]


def extract_entities(text: str) -> list[dict]:
    """Deterministic offline candidates with exact character offsets; no model download."""
    candidates = []
    occupied = []
    # Longer alias labels first so "Central Intelligence Agency" wins over partials.
    aliases = sorted(ALIASES.items(), key=lambda kv: -len(kv[1][0]))
    for short, (label, kind) in aliases:
        for match in re.finditer(
            r"(?<!\w)(?:" + re.escape(label) + "|" + re.escape(short) + r")(?!\w)",
            text,
            re.IGNORECASE,
        ):
            if any(a <= match.start() < b for a, b in occupied):
                continue
            occupied.append(match.span())
            candidates.append((match, label, kind, 0.95))
    for match in ENTITY_RE.finditer(text):
        if any(match.start() < b and match.end() > a for a, b in occupied):
            continue
        label = match.group().strip()
        kind = "date" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", label) else "named_entity"
        candidates.append((match, label, kind, 0.55))
    return sorted(
        [
            {
                "id": entity_id(label, kind),
                "label": label,
                "kind": kind,
                "start": match.start(),
                "end": match.start() + len(match.group().rstrip()),
                "confidence": confidence,
                "method": "rules-v2",
            }
            for match, label, kind, confidence in candidates
        ],
        key=lambda x: x["start"],
    )


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
        self.path = settings.state_dir / "graph.sqlite"
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
        entities = extract_entities(text)
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
            extractor="rules-v2",
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
            return base
