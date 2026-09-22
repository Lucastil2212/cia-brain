"""
CIA Brain ETL — Apache Airflow DAG for lake maintenance intervals.

Continuous crawl → extract → index stays on NATS JetStream workers.
This DAG schedules the same maintenance jobs as `cia_brain.scheduler`:

  frontier_seed     daily
  recrawl_stale     hourly
  parquet_compact   every 6 hours
  health_snapshot   every 5 minutes

`graph_rebuild` remains a manual/CLI job by default (`JOB_GRAPH_REBUILD_INTERVAL_SECONDS=0`);
add an Airflow task if you want scheduled full replays.

Install: symlink/copy `dags/` into Airflow's DAG folder and ensure workers can
run `cia-brain run-job <name>` (package on PYTHONPATH or docker exec into stack).
Airflow is optional — Compose runs the built-in interval scheduler by default.
"""

from __future__ import annotations

from datetime import datetime, timedelta

try:
    from airflow import DAG
    from airflow.operators.bash import BashOperator
except ImportError:  # pragma: no cover - Airflow is an optional host dependency
    DAG = None  # type: ignore[assignment,misc]
    BashOperator = None  # type: ignore[assignment,misc]


def _task(job: str) -> "BashOperator":
    return BashOperator(
        task_id=job,
        bash_command=f"cia-brain run-job {job}",
    )


def build_dags():
    if DAG is None:
        return []

    common = {
        "owner": "cia-brain",
        "depends_on_past": False,
        "email_on_failure": False,
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    }
    start = datetime(2026, 1, 1)
    tags = ["cia-brain", "etl", "open-data-lake"]

    specs = [
        ("cia_brain_frontier_seed", timedelta(days=1), "frontier_seed"),
        ("cia_brain_recrawl_stale", timedelta(hours=1), "recrawl_stale"),
        ("cia_brain_parquet_compact", timedelta(hours=6), "parquet_compact"),
        ("cia_brain_health_snapshot", timedelta(minutes=5), "health_snapshot"),
    ]
    dags = []
    for dag_id, schedule, job in specs:
        with DAG(
            dag_id=dag_id,
            description=f"CIA Brain ETL job: {job}",
            default_args=common,
            start_date=start,
            schedule=schedule,
            catchup=False,
            max_active_runs=1,
            tags=tags,
        ) as dag:
            _task(job)
            dags.append(dag)

    # Convenience chain DAG for operators who prefer a single graph.
    with DAG(
        dag_id="cia_brain_etl",
        description="CIA Brain open-source lake maintenance ETL chain",
        default_args=common,
        start_date=start,
        schedule=timedelta(hours=1),
        catchup=False,
        max_active_runs=1,
        tags=tags,
    ) as chain:
        seed = _task("frontier_seed")
        recrawl = _task("recrawl_stale")
        compact = _task("parquet_compact")
        health = _task("health_snapshot")
        seed >> recrawl >> compact >> health
        dags.append(chain)
    return dags


_dags = build_dags()
globals().update({d.dag_id: d for d in _dags})
