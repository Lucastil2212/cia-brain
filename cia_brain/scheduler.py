from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .jobs import JOB_REGISTRY, JobResult, run_job
from .log import configure_logging
from .settings import Settings, get_settings

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobSpec:
    name: str
    interval_seconds: int
    description: str
    enabled: bool = True
    last_run_at: str | None = None
    last_status: str | None = None
    last_detail: dict = field(default_factory=dict)
    next_run_at: float = 0.0
    run_count: int = 0


DESCRIPTIONS = {
    "recrawl_stale": "Re-queue stale downloads for conditional refetch",
    "parquet_compact": "Flush vectors and rewrite Parquet projections",
    "frontier_seed": "Ensure configured seed URLs are in the frontier",
    "health_snapshot": "Record lake and frontier counters",
    "graph_rebuild": "Replay normalized documents into the local knowledge graph",
}


def intervals_from_settings(settings: Settings) -> dict[str, int]:
    return {
        "recrawl_stale": settings.job_recrawl_interval_seconds,
        "parquet_compact": settings.job_parquet_compact_interval_seconds,
        "frontier_seed": settings.job_frontier_seed_interval_seconds,
        "health_snapshot": settings.job_health_interval_seconds,
        "graph_rebuild": settings.job_graph_rebuild_interval_seconds,
    }


def state_path(settings: Settings):
    return settings.state_dir / "scheduler.json"


def load_job_state(settings: Settings) -> dict:
    path = state_path(settings)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("jobs", {})
    except Exception:
        return {}


def write_job_result(settings: Settings, result: JobResult) -> None:
    path = state_path(settings)
    payload = {"updated_at": utcnow(), "jobs": load_job_state(settings)}
    prev = payload["jobs"].get(result.job, {})
    payload["jobs"][result.job] = {
        "last_run_at": result.ran_at,
        "last_status": "ok" if result.ok else "error",
        "last_detail": {"detail": result.detail, "error": result.error},
        "run_count": int(prev.get("run_count") or 0) + 1,
        "interval_seconds": intervals_from_settings(settings).get(result.job, 0),
        "enabled": intervals_from_settings(settings).get(result.job, 0) > 0,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def public_status(settings: Settings) -> dict:
    saved = load_job_state(settings)
    now = time.time()
    jobs = []
    for name, interval in intervals_from_settings(settings).items():
        data = saved.get(name, {})
        enabled = interval > 0
        next_run_at = None
        if enabled and data.get("last_run_at"):
            try:
                last = datetime.fromisoformat(data["last_run_at"]).timestamp()
                next_run_at = last + interval
            except Exception:
                next_run_at = now
        elif enabled:
            next_run_at = now
        jobs.append(
            {
                "name": name,
                "interval_seconds": interval,
                "description": DESCRIPTIONS.get(name, name),
                "enabled": enabled,
                "last_run_at": data.get("last_run_at"),
                "last_status": data.get("last_status"),
                "last_detail": data.get("last_detail") or {},
                "run_count": int(data.get("run_count") or 0),
                "next_run_at": next_run_at,
                "seconds_until_next": max(0, int(next_run_at - now)) if next_run_at else None,
            }
        )
    return {
        "ok": True,
        "now": utcnow(),
        "tick_seconds": settings.scheduler_tick_seconds,
        "jobs": jobs,
        "available_jobs": list(JOB_REGISTRY),
    }


class IntervalScheduler:
    """Lightweight Airflow-style interval runner for lake maintenance jobs.

    Continuous crawl/extract/index workers remain NATS-driven; this scheduler
    handles periodic DAG-like tasks (recrawl, compact, seed, health).
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.jobs = self._build_specs()
        self._hydrate_from_disk()

    def _build_specs(self) -> dict[str, JobSpec]:
        now = time.time()
        specs: dict[str, JobSpec] = {}
        for name, interval in intervals_from_settings(self.s).items():
            specs[name] = JobSpec(
                name=name,
                interval_seconds=interval,
                description=DESCRIPTIONS.get(name, name),
                enabled=interval > 0,
                next_run_at=now if interval > 0 else 0.0,
            )
        return specs

    def _hydrate_from_disk(self) -> None:
        saved = load_job_state(self.s)
        for name, data in saved.items():
            if name not in self.jobs:
                continue
            job = self.jobs[name]
            job.last_run_at = data.get("last_run_at")
            job.last_status = data.get("last_status")
            job.last_detail = data.get("last_detail") or {}
            job.run_count = int(data.get("run_count") or 0)
            if job.enabled and job.last_run_at:
                try:
                    last = datetime.fromisoformat(job.last_run_at).timestamp()
                    job.next_run_at = last + job.interval_seconds
                except Exception:
                    pass

    def status(self) -> dict:
        return public_status(self.s)

    def trigger(self, name: str) -> JobResult:
        if name not in JOB_REGISTRY:
            return JobResult(name, False, error=f"unknown job: {name}")
        return self._execute(name)

    def _execute(self, name: str) -> JobResult:
        with self._lock:
            result = run_job(name, self.s)
            write_job_result(self.s, result)
            job = self.jobs.get(name)
            if job:
                job.last_run_at = result.ran_at
                job.last_status = "ok" if result.ok else "error"
                job.last_detail = {"detail": result.detail, "error": result.error}
                job.run_count += 1
                if job.enabled:
                    job.next_run_at = time.time() + job.interval_seconds
            return result

    def run_forever(self) -> None:
        log.info(
            "scheduler started with jobs: %s",
            {n: j.interval_seconds for n, j in self.jobs.items() if j.enabled},
        )
        while not self._stop.is_set():
            self._hydrate_from_disk()
            due = []
            with self._lock:
                now = time.time()
                for job in self.jobs.values():
                    if job.enabled and job.next_run_at <= now:
                        due.append(job.name)
            for name in due:
                if self._stop.is_set():
                    break
                log.info("running scheduled job %s", name)
                result = self._execute(name)
                if result.ok:
                    log.info("job %s ok: %s", name, result.detail)
                else:
                    log.error("job %s failed: %s", name, result.error)
            self._stop.wait(self.s.scheduler_tick_seconds)
        log.info("scheduler stopped")

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    sched = IntervalScheduler(s)

    def _handle(sig, frame):  # noqa: ARG001
        log.info("received signal %s; shutting down scheduler", sig)
        sched.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    sched.run_forever()


if __name__ == "__main__":
    main()
