# Airflow DAGs for CIA Brain

Optional. The Compose `scheduler` service already runs these jobs on intervals.

To use Apache Airflow instead (or in addition):

1. Point Airflow `dags_folder` at this directory (or symlink the files).
2. Ensure workers can execute `cia-brain run-job <name>` with access to `/data`
   (same volume layout as Compose) and package import `cia_brain`.
3. Disable overlapping jobs in Compose by setting the matching
   `JOB_*_INTERVAL_SECONDS=0` if Airflow should be the sole scheduler.

DAG IDs:

- `cia_brain_frontier_seed` (daily)
- `cia_brain_recrawl_stale` (hourly)
- `cia_brain_parquet_compact` (every 6h)
- `cia_brain_health_snapshot` (every 5m)
- `cia_brain_etl` (hourly chain of all four)
