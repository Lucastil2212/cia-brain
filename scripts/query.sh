#!/bin/sh
set -eu
q=${1:?usage: query.sh 'search terms'}
curl -fsS -X POST http://localhost:8080/v1/search \
  -H 'content-type: application/json' \
  -d "$(python -c 'import json,sys; print(json.dumps({"query":sys.argv[1],"top_k":12}))' "$q")"
