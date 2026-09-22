# CIA Brain — public intelligence lake, local knowledge graph, and search

CIA Brain is a Docker Compose project that incrementally archives **publicly accessible, robots-allowed government collections** and polls public observation and media APIs, extracts searchable text, stores immutable originals in a content-addressed lake, builds lexical + semantic search projections, and optionally runs a fully local source-grounded research/education agent.

It is designed to plug into the Manticore Open Data Lake conventions:

- **raw originals:** immutable content-addressed files under `/data/raw`
- **normalized records:** gzipped JSON under `/data/normalized`
- **authoritative analytics projection:** Parquet + ZSTD under `/data/parquet`
- **lexical projection:** SQLite FTS5 / BM25
- **semantic projection:** FastEmbed + USearch HNSW
- **analytics:** DuckDB over Parquet (`cia-brain sql ...`)
- **event backbone:** NATS JetStream (continuous ETL)
- **interval jobs:** built-in scheduler + optional Apache Airflow DAGs
- **API + Search & Discovery UI:** FastAPI on port 8080
- **optional local agent:** Ollama multi-step reasoner

The search indexes are rebuildable projections. Original downloaded bytes + normalized records are preserved independently.

## Important crawling behavior

As of 2026-09-21, `https://www.cia.gov/robots.txt` advertises a 10-second crawl delay to general crawlers and disallows several paths/file patterns (including site-search paths, JavaScript, source maps, and JSON). This crawler:

1. fetches and parses `robots.txt` per allowed host;
2. refuses any URL rejected by `robots.txt`;
3. uses the greater of the site's crawl delay and `CRAWL_DELAY_SECONDS`;
4. uses CIA's advertised sitemap URLs plus recursive same-site discovery;
5. follows only explicitly enabled source hosts;
6. supports conditional re-fetching with `ETag` / `Last-Modified`;
7. hashes every downloaded object with SHA-256 and deduplicates bytes.

Because of that published 10-second delay, a truly exhaustive first crawl can take a long time. Do **not** lower the configured delay below the site's active policy or parallelize around it.

## Services

| Service | Role |
|---|---|
| `nats` | durable event stream between ingestion stages |
| `crawler` | sitemap + recursive, robots-aware downloader |
| `feeds` | polls live geo/telemetry/image/media/RSS/catalog APIs |
| `extractor` | HTML/PDF/Office/text extraction; optional OCR |
| `indexer` | chunking, FTS5, embeddings, USearch, Parquet compaction |
| `api` | hybrid search, discovery API, and web UI on port 8080 |
| `scheduler` | interval jobs (recrawl, compact, seed, health, optional graph_rebuild) |
| `ollama` | optional local model runtime (`ai` profile) |
| `agent` | source-grounded chat / reason / educational products on port 8090 |

## Start

```bash
cp .env.example .env
mkdir -p data
docker compose up -d --build nats indexer extractor crawler feeds api scheduler
```

Open the **Search & Discovery UI** at [http://localhost:8080/](http://localhost:8080/).

## Production on Render

See [`DEPLOY.md`](DEPLOY.md) and [`render.yaml`](render.yaml). Render runs managed Postgres for accounts/API keys plus one all-in-one web service (API, UI, NATS, ingest workers) with a persistent `/data` disk. Programmatic clients authenticate with `X-API-Key` or Bearer JWT and receive rate-limit headers.

Local accounts (optional):

```bash
docker compose up -d --build postgres nats api crawler feeds extractor indexer scheduler
docker compose --profile migrate run --rm migrate
```

Watch ingestion:

```bash
docker compose logs -f crawler extractor indexer scheduler
curl -s http://localhost:8080/v1/stats | python -m json.tool
```

Search:

```bash
curl -s -X POST http://localhost:8080/v1/search \
  -H 'content-type: application/json' \
  -d '{"query":"MKULTRA behavioral research", "top_k":10}' | python -m json.tool
```

Interactive API docs are at `http://localhost:8080/docs`.

## Vercel deployment

The repository includes a root `app.py` entrypoint for Vercel's zero-configuration FastAPI runtime and pins Python 3.12 in `pyproject.toml`.

On Vercel, `DATA_DIR` defaults to `/tmp/cia-brain`, which is writable but **ephemeral**. This lets the FastAPI application and web UI boot cleanly for a serverless deployment, including an empty search store before any index exists.

The full ingestion system still requires long-running services and durable storage: NATS, crawler, feeds, extractor, indexer, scheduler, and optionally Ollama/agent. Keep Docker Compose for that backend (or move those services to a persistent container host) and treat the Vercel deployment as the public FastAPI/UI surface until the durable backend is externalized.

No `vercel.json` is required for the root FastAPI entrypoint.

## Local AI agent

The default AI profile uses `qwen3:8b` through Ollama. It is separate from ingestion/search so the lake remains useful on machines that cannot run a multi-GB LLM.

```bash
docker compose --profile ai up -d --build
```

Ask a source-grounded question:

```bash
curl -s -X POST http://localhost:8090/v1/chat \
  -H 'content-type: application/json' \
  -d '{"question":"What do the archived documents say about the U-2 program?", "top_k":10}' \
  | python -m json.tool
```

Generate an educational artifact:

```bash
curl -s -X POST http://localhost:8090/v1/product \
  -H 'content-type: application/json' \
  -d '{"topic":"The Bay of Pigs and intelligence analysis", "product":"study-guide", "top_k":12}' \
  | python -m json.tool
```

Multi-step reason (plan → retrieve → synthesize), also available from the UI **Reason** tab via `/agent/v1/reason`:

```bash
curl -s -X POST http://localhost:8090/v1/reason \
  -H 'content-type: application/json' \
  -d '{"question":"What do archived sources say about Project AZORIAN?", "top_k":12}' \
  | python -m json.tool
```

Supported `product` values: `lesson`, `study-guide`, `quiz`, `timeline`, `brief`.

## Interval jobs & Airflow DAG

Continuous ingest is NATS-driven (`crawler` → `extractor` → `indexer`). A separate **scheduler** runs Airflow-style interval maintenance:

| Job | Default interval | Purpose |
|---|---|---|
| `frontier_seed` | 24h | ensure seed URLs stay in the frontier |
| `recrawl_stale` | 1h | re-queue downloads older than `RECRAWL_AFTER_HOURS` |
| `parquet_compact` | 6h | flush vectors + rewrite Parquet projections |
| `health_snapshot` | 5m | record lake / frontier counters |

Configure via `.env` (`JOB_*_INTERVAL_SECONDS`; `0` disables). Trigger from the UI **Pipeline** tab, API (`POST /v1/pipeline/jobs/trigger`), or CLI:

```bash
cia-brain jobs
cia-brain run-job health_snapshot
```

Optional Apache Airflow definitions live in [`dags/cia_brain_etl.py`](dags/cia_brain_etl.py) — symlink into your Airflow `dags/` folder. Airflow is **not** required for Compose; the built-in scheduler covers the same jobs locally.

### Stronger local model

If your machine has substantially more RAM/VRAM, change:

```dotenv
LLM_MODEL=qwen3.8:27b
```

Then restart the AI profile. The default `qwen3:8b` is intentionally more practical for a laptop-class machine.

### Newer embedding option

The core profile intentionally keeps the earlier Manticore ODL-compatible FastEmbed model:

```dotenv
EMBEDDING_BACKEND=fastembed
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

For a larger/current local embedding model through Ollama, run the AI profile, pull `qwen3-embedding:0.6b`, then set:

```dotenv
EMBEDDING_BACKEND=ollama
OLLAMA_EMBED_MODEL=qwen3-embedding:0.6b
```

After changing embedding models, rebuild all vectors:

```bash
docker compose run --rm --entrypoint python indexer -m cia_brain.cli rebuild
```

Never mix vectors generated by different embedding models in one index.

## OCR

Many historical scans have embedded OCR text already. If a PDF extracts almost no text, CIA Brain can optionally OCR it locally with Tesseract:

```dotenv
OCR_ENABLED=true
OCR_MAX_PAGES=0
```

`0` means all pages. OCR can consume significant CPU and storage I/O, so it is disabled by default.

## Data layout

```text
data/
  raw/          # immutable bytes, sha256-addressed
  manifests/    # URL/fetch metadata
  normalized/   # extracted JSON.gz records
  parquet/      # compact analytical projections
  state/
    crawl.sqlite
    search.sqlite
    graph.sqlite       # entities, mentions, evidence-bearing edges
    feeds.sqlite       # polling state and record revisions
    vectors.usearch
    vector-meta.json
  models/       # FastEmbed model cache
```

## Hybrid ranking

Search retrieves separate candidate sets from:

- SQLite FTS5/BM25 (lexical/exact terms)
- USearch cosine HNSW (semantic vectors)

It fuses them with weighted Reciprocal Rank Fusion (RRF):

```text
score(d) = w_lex/(k + rank_lex(d)) + w_sem/(k + rank_sem(d))
```

Weights and `k` are configurable in `.env`. This avoids pretending incomparable BM25 and cosine scores share the same scale.

## Provenance and educational use

Every result returns the government source URL(s), SHA-256 of the downloaded object, fetch timestamp, chunk number, and matching text. Byte-identical files discovered at multiple source URLs retain those aliases in the manifest and search metadata. The local agent is prompted to cite retrieved source markers and to distinguish **what a source states** from independently established historical fact. For publication-quality work, follow the returned source URL and inspect the original file.

## Operational controls

Key environment variables:

- `CRAWL_DELAY_SECONDS=10` — floor for host throttling
- `MAX_FILE_BYTES=0` — `0` means no per-file cap
- `MIN_FREE_DISK_GB=5` — pauses downloading before exhausting disk
- `RECRAWL_AFTER_HOURS=168` — freshness window before conditional refetch
- `MAX_DEPTH=40` — recursive link ceiling
- `OCR_ENABLED=false` — optional scanned-PDF OCR
- `CHUNK_CHARS=1800`, `CHUNK_OVERLAP=240`
- `VECTOR_PERSIST_EVERY=10` — batch USearch disk writes
- `JOB_RECRAWL_INTERVAL_SECONDS=3600` (and sibling `JOB_*` keys)

Crawler state is persistent and idempotent. Re-running containers resumes the frontier instead of starting over.

## Security boundaries

- only configured and catalog-enabled government archive hosts are accepted;
- redirects to external hosts are rejected;
- `robots.txt` defaults to deny when unavailable;
- content is stored as bytes and never executed;
- ZIP files are cataloged, not unpacked/executed;
- raw-file API validates SHA-256 paths and never accepts arbitrary filesystem paths;
- the agent receives only extracted text returned by search.

## Tests

```bash
docker compose build api
docker compose run --rm --entrypoint sh api -c "pip install -q 'pytest>=8.3,<9' && pytest -q"
```

## Production notes

For a single Linux host this deliberately avoids heavyweight external databases. If you later horizontally scale workers across multiple hosts, keep raw/Parquet on an object store and replace local SQLite/USearch projections with networked search/database services; the event contracts and immutable SHA-256 IDs let you do that without changing the archive format.

## Local analytics

Once Parquet projections exist, run ad-hoc analytical queries without a server database:

```bash
docker compose run --rm --entrypoint cia-brain api sql \
  "SELECT mime, count(*) AS n FROM documents GROUP BY mime ORDER BY n DESC"
```

The Compose file pins Ollama to `0.34.1` (current stable tag verified when this project was assembled) rather than `latest`.


## Government collections, live feeds, and knowledge graph

Additional archives are enabled by default through
`ARCHIVE_SOURCES=fbi,nsa,nara,state,odni,dia,crs`:
[FBI Vault](https://vault.fbi.gov/), [NSA declassification](https://www.nsa.gov/Helpful-Links/NSA-FOIA/Declassification-Transparency-Initiatives/),
[National Archives declassification](https://www.archives.gov/declassification),
[State Department historical documents](https://history.state.gov/historicaldocuments),
[ODNI FOIA](https://www.dni.gov/index.php/foia),
[DIA FOIA Electronic Reading Room](https://www.dia.mil/FOIA/FOIA-Electronic-Reading-Room/), and
[CRS reports](https://crsreports.congress.gov/).
These include FOIA releases and historical publications; public availability alone does not
establish that every item was previously classified. Collection links and linked attachments
are crawled subject to robots rules, throttling, and configured depth. The live
`nara_catalog` feed polls the public [NARA Catalog API](https://github.com/usnationalarchives/CatalogAPI)
for declassified description hits; it does not retrieve records that exist only in a reading room.
New collection hosts are discovered from their seeds rather than loading entire agency sitemaps.
Redirect destinations are checked before requests. A robots denial remains a denial.
Set `ARCHIVE_SOURCES=` to keep only the original configured hosts and seeds.

The `feeds` Compose service polls these fixed public endpoints independently of the archive
frontier. No API key is required. Set `CRAWLER_CONTACT` to a contact address for the User-Agent,
as requested by NWS. `LIVE_SOURCES=` disables polling; comma-separated IDs select feeds.

| ID | Source and documentation | Interval | Stored content |
|---|---|---|---|
| `usgs` | [USGS earthquakes](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php) | 60s | Past-hour global events, text, timestamps, point geometries |
| `nws` | [NWS active alerts](https://www.weather.gov/documentation/services-web-api) | 120s | Alert text, properties, timestamps, available polygons |
| `swpc` | [NOAA solar wind](https://www.swpc.noaa.gov/products/real-time-solar-wind) | 60s | One-hour propagated solar-wind measurements |
| `goes` | [NOAA GOES imagery](https://www.star.nesdis.noaa.gov/GOES/) | 600s | Actual GOES-19 CONUS GeoColor image bytes |
| `nasa` | [NASA media API](https://images.nasa.gov/docs/images.nasa.gov_api_docs.pdf) | 3600s | First 100 current-year search results: descriptions and image/audio/video asset references |
| `nara_catalog` | [NARA Catalog API](https://github.com/usnationalarchives/CatalogAPI) | 1800s | Declassified catalog descriptions (naId, title, text) |
| `state_rss` | [State Department press RSS](https://www.state.gov/rss-feed/) | 900s | Press-release titles and summaries |
| `fema_rss` | [FEMA news RSS](https://www.fema.gov/about/news-multimedia) | 900s | Emergency/management press items |
| `odni_rss` | [ODNI site RSS](https://www.dni.gov/) | 1800s | ODNI news/feed items when published |

“Live” means periodic polling, with upstream publication latency. NASA is a periodically
refreshed media catalog, not a real-time sensor feed or exhaustive archive. NASA audio/video
binaries are referenced, not downloaded or transcribed. GOES imagery is archived locally;
optional Tesseract OCR extracts visible text, not visual scene interpretation. No local
speech recognition or vision model is included. The USGS past-hour feed cannot backfill
outages longer than its window. Missing alerts are retained as historical observations;
“active” graph records mean the latest stored revision, not an assertion that an alert is
still in force.

Snapshots and individual structured records are content-addressed. Each normalized feed
record links to its original snapshot hash, upstream record ID, source ID, publication time,
geometry/measurements/media references, and fetch time. `source_url` for structured records
is the feed endpoint with a record-ID fragment; the upstream properties retain original
links where provided. Changed records enter the existing NATS extraction/search pipeline;
unchanged records are deduplicated. Conditional requests, bounded response sizes, persistent
backoff, and per-source errors are exposed in `GET /v1/sources` and the **Sources** tab.
Only one feeds worker should run against a given data directory.

The graph is a local SQLite WAL database at `data/state/graph.sqlite`, built during extraction
without external graph databases or hosted AI. Entity extraction is controlled by `GRAPH_NER`:

| Value | Behavior |
|---|---|
| `auto` (default) | Use local spaCy `en_core_web_sm` when installed; otherwise rules |
| `spacy` | Require spaCy; fall back to rules only if the model cannot load |
| `rules` | Deterministic offline gazetteer/heuristics only (`rules-v3`) |

Compose images install the optional `ner` extra and download `en_core_web_sm`. Agency aliases
always override overlapping model spans. spaCy labels map to `person`, `organization`,
`location`, `date`, `event`, and `group`. The rules path recognizes expanded agency aliases
(CIA, FBI, NSA, ODNI, DIA, CRS, FEMA, DHS, …), org-suffix phrases, person-like `First Last`
candidates, a small location gazetteer, ISO dates, and acronyms, then attaches pattern-based
relation labels when sentence cues match (`worked_with`, `released_by`, `reported_by`,
`located_in`, `transferred_to`); otherwise edges remain `co_mentioned`. Ambiguous leftover
tokens use `named_entity`. Case-folded labels are merged; this can conflate namesakes and miss
spelling variants. Character offsets and heuristic confidence values accompany mentions.
Confidence values are not calibrated probabilities.

Edges retain the supporting passage and source hash, and are explicitly marked inferred.
They do not assert employment, causality, or other facts beyond the lexical pattern cue.
Document correlations rank shared entity sets using Jaccard overlap; date-only overlap is
excluded from matches. Documents with Point geometries also support Haversine proximity
via `GET /v1/graph/correlations/{sha}?mode=spatial` (default radius `GRAPH_SPATIAL_RADIUS_KM`).
Graph analysis is bounded to the first two million text characters,
20 unique non-date entities per sentence, and 2,000 characters per evidence excerpt;
truncation and analyzed character counts are recorded. Revision replay is idempotent and
older feed revisions are excluded from current graph queries. Historical bytes remain stored.
Telemetry time-series correlation is still not inferred automatically.

Open **Knowledge graph** in the UI to search entities, inspect supporting passages, and find
related documents (entity overlap or nearby geo). API access:

```bash
curl 'http://localhost:8080/v1/sources'
curl 'http://localhost:8080/v1/graph/stats'
curl 'http://localhost:8080/v1/graph/entities?q=NASA'
curl 'http://localhost:8080/v1/graph/entities/ENTITY_ID'
curl 'http://localhost:8080/v1/graph/documents/DOCUMENT_SHA256'
curl 'http://localhost:8080/v1/graph/correlations/DOCUMENT_SHA256'
curl 'http://localhost:8080/v1/graph/correlations/DOCUMENT_SHA256?mode=spatial&radius_km=250'
curl 'http://localhost:8080/v1/graph/correlations/DOCUMENT_SHA256?mode=all'
```

To apply these changes to an existing installation and populate the graph from existing
normalized documents:

```bash
docker compose up -d --build nats crawler feeds extractor indexer api scheduler
docker compose run --rm --entrypoint cia-brain api run-job frontier_seed
docker compose run --rm --entrypoint cia-brain api run-job graph_rebuild
```

`GRAPH_ENABLED=false` disables automatic extraction and graph API access. `graph_rebuild`
is an explicit manual replay command by default (`JOB_GRAPH_REBUILD_INTERVAL_SECONDS=0`);
set a positive interval to schedule periodic rebuilds. Records without extractable text can
be archived but will not produce entity connections.
