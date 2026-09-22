#!/bin/sh
set -eu
export NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
export DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/raw" "$DATA_DIR/manifests" "$DATA_DIR/normalized" \
  "$DATA_DIR/parquet" "$DATA_DIR/state" "$DATA_DIR/models" "$DATA_DIR/nats"
if [ -n "${DATABASE_URL:-}" ]; then
  python -m cia_brain.migrate
fi
exec python -m cia_brain.prod
