from pathlib import Path

from cia_brain.jobs import JOB_REGISTRY, run_job
from cia_brain.scheduler import public_status, write_job_result
from cia_brain.settings import Settings


def test_job_registry_contains_interval_jobs():
    assert set(JOB_REGISTRY) >= {
        "recrawl_stale",
        "parquet_compact",
        "frontier_seed",
        "health_snapshot",
    }


def test_health_snapshot_and_status(tmp_path: Path):
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    result = run_job("health_snapshot", s)
    assert result.ok
    write_job_result(s, result)
    status = public_status(s)
    names = {j["name"] for j in status["jobs"]}
    assert "health_snapshot" in names
    health = next(j for j in status["jobs"] if j["name"] == "health_snapshot")
    assert health["run_count"] == 1
    assert health["last_status"] == "ok"


def test_frontier_seed_job(tmp_path: Path):
    s = Settings(
        data_dir=tmp_path,
        seed_urls="https://www.cia.gov/",
        allowed_hosts="www.cia.gov,cia.gov",
    )
    s.ensure_dirs()
    result = run_job("frontier_seed", s)
    assert result.ok
    assert result.detail.get("seeded", 0) >= 1
