# Deployment

TeachLab has four deployable parts:

* the **API** (FastAPI, stateless);
* one or more **workers**, which use the same image with a different command;
* the **frontend** (Next.js);
* managed **data services**: PostgreSQL, Neo4j and Qdrant, plus object storage for PDFs and uploads.

## Resource requirements (measured)

Measured on a 4-core, 12 GB Windows laptop with Python 3.13, torch 2.14 (CPU) and `BAAI/bge-small-en-v1.5`. Numbers will differ on other hardware.

| Process | RSS | Notes |
|---|---|---|
| API (`uvicorn app.main:app`) | **~120 MB** | Never loads the embedding model or calls the LLM. |
| Worker, idle with BGE loaded | **~600 MB** | The model is loaded once per worker process. |
| Worker while embedding a batch | **~740 MB** | Batch size `EMBEDDING_BATCH_SIZE=32`. |
| Neo4j (docker-compose) | 1–1.5 GB | Heap capped at 1 GB in the compose file. |

* **Query embedding latency:** about 40 ms on CPU.
* **Indexing throughput:** about 6 chunks/s for ~250-token chunks on a busy 4-core CPU. Expect roughly 15–40 chunks/s on a dedicated modern 4-vCPU host. A typical research turn ingests 2–6 sources of about 5–30 chunks each, so indexing takes seconds; the LLM extraction calls dominate.
* **Cold start:** importing torch and loading the model took 13–70 s on this machine. Bake the model into the image (the Dockerfile already does this) and set `HF_HUB_OFFLINE=1` to skip Hugging Face network checks.
* **No GPU is required.** Set `EMBEDDING_DEVICE=cuda` only if one is available.

**Memory-constrained hosts (≤1 GB).** Run the API and the worker as separate services; the API fits in 256–512 MB. Give the worker at least 1 GB and use `WORKER_CONCURRENCY=1`. If even that's too much, implement an `EmbeddingProvider` with fastembed/ONNX using the same BGE model, which needs roughly 250 MB and no torch. Dimensions and normalization stay the same, so vectors remain compatible, and `POST /vectors/reindex` rebuilds the index if you switch models anyway. The Qdrant collection name encodes the model and dimension, so a model change creates a fresh collection rather than mixing vectors.

## Containers

```bash
docker build -t teachlab-backend backend/
docker build -t teachlab-frontend --build-arg NEXT_PUBLIC_API_URL=https://api.example.com frontend/

# migrations (one-off, e.g. a release command)
docker run --env-file .env teachlab-backend alembic upgrade head
# API
docker run --env-file .env -p 8000:8000 teachlab-backend
# worker (scale horizontally; jobs are claimed with SKIP LOCKED)
docker run --env-file .env teachlab-backend python -m app.worker
```

## Suggested free/low-cost topology

| Component | Option | Notes |
|---|---|---|
| Frontend | Vercel / Netlify / any Node host | Set `NEXT_PUBLIC_API_URL`. The browser calls the API directly, so SSE isn't buffered by a proxy. |
| API | Render / Railway / Fly.io web service (512 MB) | Add the frontend origin to `CORS_ORIGINS`, e.g. `CORS_ORIGINS=["https://app.example.com"]`. |
| Worker | Render / Railway / Fly.io background worker (≥1 GB) | Same image, `python -m app.worker`. |
| PostgreSQL | Neon / Supabase / Railway | `DATABASE_URL=postgresql+asyncpg://…`. Run `alembic upgrade head` on release. |
| Neo4j | Neo4j AuraDB Free | `NEO4J_URI=neo4j+s://<id>.databases.neo4j.io`. Constraints are created on worker start. |
| Qdrant | Qdrant Cloud free cluster | `QDRANT_URL=https://<cluster>.cloud.qdrant.io:6333`, `QDRANT_API_KEY`. |
| File storage | Local disk only for a single instance | For multiple instances, implement `StorageProvider` for S3, R2 or MinIO (see below). |

**Object storage.** `app/providers/storage/local.py` uses path-like keys (`{workspace_id}/artifacts/{kind}-{hash}.pdf`, `{workspace_id}/uploads/...`, `{workspace_id}/raw/...`). An S3-compatible provider maps these keys one-to-one to object keys. The API and worker must share the same storage. On platforms with ephemeral disks, use object storage or a shared volume.

## Production configuration checklist

* `ENV=production`. Startup refuses `AUTH_PROVIDER=local`, `LLM_PROVIDER=fake` and `SEARCH_PROVIDER=fixture`.
* **Authentication.** Implement and select a real `AuthProvider`, for example Clerk JWT verification via JWKS. Local header auth is development-only.
* `LOG_JSON=true`. Optionally set `OTEL_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT`.
* `LLM_TOKENS_PER_MINUTE` should match your Groq plan, and the model IDs should match Groq's current model list.
* `CODE_VERIFICATION_ENABLED=false`, unless workers run in a locked-down container with no network, a read-only filesystem and CPU/memory limits.
* Put a reverse proxy or platform TLS in front of the API. Keep request bodies within `MAX_UPLOAD_MB`.
* Back up PostgreSQL. It is the source of truth, and the graph and vectors can be rebuilt from it with `POST /graph/rebuild` and `POST /vectors/reindex`.

## Free-tier quotas and bottlenecks

Verify current limits; free tiers change.

* **Groq.** The free tier has per-model requests-per-minute, tokens-per-minute and daily limits.
  * Extraction is the heaviest consumer: one call per batch of about 2,500 tokens of source text.
  * The client throttles to `LLM_TOKENS_PER_MINUTE` and honors `retry-after`; jobs retry with backoff.
  * Use the fast model for extraction and summaries, and the larger model for answers, roadmaps, notes and questions.
  * Expect large research sessions to be rate-limited. They then complete more slowly rather than failing.
* **Exa** (default search). Free credits at sign-up; each search with page text uses credits, and results are cached per workspace for `SEARCH_CACHE_TTL_HOURS`.
* **Tavily.** The free plan has a monthly credit allowance. Searches use `search_depth=basic` with `include_raw_content`, which usually avoids separate extract calls. Results are cached per workspace for `SEARCH_CACHE_TTL_HOURS`, and the agent skips searching when local knowledge is sufficient.
* **Neo4j AuraDB Free.** Node and relationship caps (on the order of 200k nodes / 400k relationships at the time of writing). Free instances pause after inactivity and may be deleted after a long idle period; keep PostgreSQL as the system of record and rebuild the graph when needed. Each claim adds about 1 Claim node plus one Evidence node per supporting chunk.
* **Qdrant Cloud free.** About 1 GB of storage. At 384 dimensions with payloads, that's on the order of hundreds of thousands of chunks.
* **CPU embeddings.** These are the main worker CPU cost. Scale out by adding workers, since each claims jobs independently.
