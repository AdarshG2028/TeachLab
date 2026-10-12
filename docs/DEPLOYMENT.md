# Deployment

TeachLab has four deployable parts:

* the **API** (FastAPI, stateless);
* one or more **workers**, which use the same image with a different command;
* the **frontend** (Next.js);
* managed **data services**: PostgreSQL and Qdrant, plus object storage for PDFs and uploads.

## Resource requirements (measured)

Measured with Python 3.13 and `BAAI/bge-small-en-v1.5` on ONNX Runtime (fastembed). Numbers will differ on other hardware.

| Setup | Memory | Notes |
|---|---|---|
| **Docker image, API + worker in one process (default)** | **312 MiB idle, 464 MiB peak** | Peak measured under a hard 512 MB container limit during a roadmap + lesson + 5-PDF teaching pack, with `WORKER_CONCURRENCY=1`, `MALLOC_ARENA_MAX=2`; no OOM kill. Image size 1.13 GB. |
| Same, PyTorch backend (`EMBEDDING_BACKEND=sentence_transformers`) | ~520 MB for the model alone (≈740 MB peak) | Needs a 2 GB instance; image 3.2 GB. Vectors are identical to the ONNX backend (cosine 1.0). |

* **Why ONNX:** `fastembed` runs the same BGE model on ONNX Runtime with the CPU memory arena disabled and 2 threads. Without the arena setting it used 706 MB, so that option matters.
* **Indexing throughput:** comparable to PyTorch on CPU (a few chunks/s on a busy 4-core laptop; faster on a dedicated host). A research turn embeds seconds of text; LLM calls dominate.
* **Cold start:** about 5-10 s to load the model; it is baked into the image, so nothing is downloaded at startup.
* **No GPU is required.**

**Memory-constrained hosts.** The default image already fits 512 MB with little headroom (see above). To get more, run a single job at a time (`WORKER_CONCURRENCY=1`), keep `MALLOC_ARENA_MAX=2`, or use a 1 GB instance. The Qdrant collection name encodes the model and dimension, so a model change creates a fresh collection rather than mixing vectors.

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
* Back up PostgreSQL. It is the source of truth, and the vector index can be rebuilt from it with `POST /vectors/reindex`.

## Free-tier quotas and bottlenecks

Verify current limits; free tiers change.

* **Groq.** The free tier has per-model requests-per-minute, tokens-per-minute and daily limits.
  * Extraction is the heaviest consumer: one call per batch of about 2,500 tokens of source text.
  * The client throttles to `LLM_TOKENS_PER_MINUTE` and honors `retry-after`; jobs retry with backoff.
  * Use the fast model for extraction and summaries, and the larger model for answers, roadmaps, notes and questions.
  * Expect large research sessions to be rate-limited. They then complete more slowly rather than failing.
* **Exa** (default search). Free credits at sign-up; each search with page text uses credits, and results are cached per workspace for `SEARCH_CACHE_TTL_HOURS`.
* **Tavily.** The free plan has a monthly credit allowance. Searches use `search_depth=basic` with `include_raw_content`, which usually avoids separate extract calls. Results are cached per workspace for `SEARCH_CACHE_TTL_HOURS`, and the agent skips searching when local knowledge is sufficient.
* **Qdrant Cloud free.** About 1 GB of storage. At 384 dimensions with payloads, that's on the order of hundreds of thousands of chunks.
* **CPU embeddings.** These are the main worker CPU cost. Scale out by adding workers, since each claims jobs independently.
