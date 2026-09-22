# Deploying CIA Brain on Render

## Architecture

- **Render Postgres** — user accounts, API keys, rate-limit buckets
- **One web service** (`cia-brain`) — all-in-one process: NATS + crawler + feeds + extractor + indexer + scheduler + FastAPI/UI
- **Persistent disk** (`/data`) — immutable lake, search indexes, graph SQLite, NATS JetStream store

Separate Render workers are **not** used because disks cannot be shared between services.

## Blueprint

`render.yaml` is the source of truth. Create a Blueprint from this repo on Render (Pro).

## Manual dashboard steps

1. **Connect the GitHub repo** `Lucastil2212/cia-brain` (or your fork) to Render.
2. **New → Blueprint** → select the repo → confirm `render.yaml` → Apply.
3. When prompted for sync:false env vars, set:
   - `CRAWLER_CONTACT` — a real contact email/URL for the User-Agent (required by NWS etiquette).
4. Confirm plans (Pro):
   - Postgres `basic-1gb` (or upgrade to Pro Postgres if you prefer).
   - Web service `standard` (or higher) — needs RAM for FastEmbed + spaCy + ingest.
   - Disk **50 GB** (increase later if the lake grows; size can only go up).
5. Wait for the first deploy. Open the service URL `/healthz` — expect `"ok": true` and `"database": true`.
6. Open the site → **Account** → create your researcher account → **Mint key**.
7. Store the API key offline; use it as `X-API-Key` for scripts.
8. Optional: set `ALLOW_REGISTRATION=false` in the Render Environment after creating your admin account.
9. Optional: trigger `frontier_seed` / `graph_rebuild` from the **Stacks** tab once the service is healthy.
10. Custom domain: Render service → Settings → Custom Domains → add DNS as instructed.

## API example

```bash
export HOST=https://YOUR-SERVICE.onrender.com
export KEY=cia_your_key_here

curl -s "$HOST/healthz" | jq .

curl -s -X POST "$HOST/v1/search" \
  -H "content-type: application/json" \
  -H "X-API-Key: $KEY" \
  -d '{"query":"U-2","top_k":5}' | jq .
```

## Notes

- First crawl is slow (CIA robots delay ≈ 10s). Live feeds populate sooner.
- Deploys with a disk briefly interrupt the service (no zero-downtime).
- Ollama / local LLM agent is **not** part of the Render blueprint (no GPU profile). Search + graph work without it.
- Rotate `JWT_SECRET` only if you can accept invalidating existing sessions (Render generated it on first apply).
