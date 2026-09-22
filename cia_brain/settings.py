from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    nats_url: str = "nats://nats:4222"
    log_level: str = "INFO"

    crawler_user_agent: str = "ManticoreEducationalArchiver/1.0"
    crawler_contact: str = ""
    allowed_hosts: str = "www.cia.gov,cia.gov"
    seed_urls: str = "https://www.cia.gov/,https://www.cia.gov/readingroom/"
    archive_sources: str = "fbi,nsa,nara,state,odni,dia,crs"
    live_sources: str = "usgs,nws,swpc,goes,nasa,nara_catalog,state_rss,fema_rss,odni_rss"
    feed_max_bytes: int = 20_000_000
    graph_enabled: bool = True
    graph_spatial_radius_km: float = 250.0
    job_graph_rebuild_interval_seconds: int = 0

    crawl_delay_seconds: float = 10.0
    request_timeout_seconds: float = 90.0
    max_retries: int = 5
    max_depth: int = 40
    max_file_bytes: int = 0
    recrawl_after_hours: int = 168
    min_free_disk_gb: float = 5.0

    ocr_enabled: bool = False
    ocr_max_pages: int = 0
    chunk_chars: int = 1800
    chunk_overlap: int = 240

    embedding_backend: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    ollama_embed_model: str = "qwen3-embedding:0.6b"
    ollama_url: str = "http://ollama:11434"
    hybrid_lexical_weight: float = 1.0
    hybrid_semantic_weight: float = 1.0
    rrf_k: int = 60
    default_top_k: int = 12

    llm_model: str = "qwen3:8b"
    search_api_url: str = "http://api:8080"
    agent_api_url: str = "http://agent:8090"
    agent_top_k: int = 10

    # Indexer performance
    vector_persist_every: int = 10
    parquet_compact_every: int = 25
    embed_batch_size: int = 64

    # Interval jobs (seconds). 0 disables a job in the built-in scheduler.
    job_recrawl_interval_seconds: int = 3600
    job_parquet_compact_interval_seconds: int = 21600
    job_frontier_seed_interval_seconds: int = 86400
    job_health_interval_seconds: int = 300
    scheduler_tick_seconds: float = 5.0

    @property
    def allowed_host_set(self) -> set[str]:
        from .sources import archive_hosts
        return {x.strip().lower() for x in self.allowed_hosts.split(",") if x.strip()} | archive_hosts(self.archive_sources)

    @property
    def seed_url_list(self) -> list[str]:
        from .sources import selected_sources
        return [x.strip() for x in self.seed_urls.split(",") if x.strip()] + [
            s.url for s in selected_sources(self.archive_sources, True)
        ]

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def manifests_dir(self) -> Path:
        return self.data_dir / "manifests"

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "normalized"

    @property
    def parquet_dir(self) -> Path:
        return self.data_dir / "parquet"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def ensure_dirs(self) -> None:
        for p in (
            self.raw_dir,
            self.manifests_dir,
            self.normalized_dir,
            self.parquet_dir,
            self.state_dir,
            self.data_dir / "models",
        ):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
