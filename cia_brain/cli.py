from __future__ import annotations

import argparse
import json
import sqlite3

from .indexer import SearchStore
from .jobs import JOB_REGISTRY, run_job
from .log import configure_logging
from .settings import get_settings


def main():
    p = argparse.ArgumentParser(prog="cia-brain")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild", help="rebuild SQLite FTS, vectors, and Parquet projections")
    sub.add_parser("crawl-stats", help="show crawler frontier statistics")
    sub.add_parser("jobs", help="list interval maintenance jobs")
    runp = sub.add_parser("run-job", help="run one maintenance job (Airflow / cron entrypoint)")
    runp.add_argument("job", choices=sorted(JOB_REGISTRY))
    sqlp = sub.add_parser("sql", help="run a local DuckDB query over Parquet projections")
    sqlp.add_argument("query")
    args = p.parse_args()
    s = get_settings()
    configure_logging(s.log_level)
    if args.cmd == "rebuild":
        SearchStore(s).rebuild()
    elif args.cmd == "crawl-stats":
        db = sqlite3.connect(s.state_dir / "crawl.sqlite")
        rows = db.execute("SELECT status,COUNT(*) FROM frontier GROUP BY status").fetchall()
        print(json.dumps(dict(rows), indent=2))
    elif args.cmd == "jobs":
        print(
            json.dumps(
                {
                    "jobs": list(JOB_REGISTRY),
                    "intervals_seconds": {
                        "recrawl_stale": s.job_recrawl_interval_seconds,
                        "parquet_compact": s.job_parquet_compact_interval_seconds,
                        "frontier_seed": s.job_frontier_seed_interval_seconds,
                        "health_snapshot": s.job_health_interval_seconds,
                    },
                },
                indent=2,
            )
        )
    elif args.cmd == "run-job":
        result = run_job(args.job, s)
        print(json.dumps({
            "job": result.job,
            "ok": result.ok,
            "detail": result.detail,
            "error": result.error,
            "ran_at": result.ran_at,
        }, indent=2))
        raise SystemExit(0 if result.ok else 1)
    elif args.cmd == "sql":
        from .analytics import query
        print(json.dumps(query(s, args.query), indent=2, default=str))


if __name__ == "__main__":
    main()
