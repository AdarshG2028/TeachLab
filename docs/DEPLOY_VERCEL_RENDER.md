# Deploy: Vercel (frontend) + Render (API + worker)

**Works, on Render's $7/month "Starter" plan (512 MB).** The frontend is plain Next.js and fits Vercel's free
tier. The backend runs the embedding model on ONNX (no PyTorch), so the whole service fits in 512 MB: measured
idle 312 MiB and peak 464 MiB under a hard 512 MB limit while doing a roadmap, a lesson and a full 5-PDF teaching
pack, with no out-of-memory kill. Headroom is only about 50 MB, so if Render shows out-of-memory restarts, move to
the 2 GB "Standard" plan (~$25/month) or a 1 GB host (see Variants). Render's free plan is not suitable: it sleeps
when idle, which stops background jobs. Everything else can stay on free tiers.

```
Browser ──▶ Vercel (Next.js UI)
   │
   └──────▶ Render (one Docker service: API + job worker in one process, 512 MB)
                 ├──▶ Postgres  (Neon or Supabase free tier; also stores generated PDFs)
                 ├──▶ Qdrant Cloud (free cluster)
                 ├──▶ Groq (LLM) and Exa (web search)
```

## How "no sign-in" works (read this before going public)
There are no accounts. Each browser creates a random private key (a UUID) and sends it with every request;
workspaces belong to whoever holds the key, and other keys cannot see them or guess them. Consequences:

- Clearing browser data without saving the key loses access. The home page has an **Access key** box to copy the
  key and to restore a key from another browser.
- Anyone with the key can open the workspaces, so treat it like a password.
- Anyone on the internet can use the app, and your Groq/Exa keys pay for it. The blueprint therefore sets
  **limits** (`DAILY_JOB_LIMIT_PER_USER=40`, `DAILY_JOB_LIMIT_GLOBAL=400`, `MAX_WORKSPACES_PER_USER=10`).
  A determined person can still create many keys to get many allowances; the global cap bounds the total.
  If the site will be shared widely, add a real login or an access code in front of it.

## 1. Data services (free tiers)
1. **Postgres**: create a database at [Neon](https://neon.tech) or [Supabase](https://supabase.com) and copy the
   connection string (`postgres://...?sslmode=require` is accepted as is).
2. **Qdrant**: create a free cluster at [cloud.qdrant.io](https://cloud.qdrant.io); note the URL
   (`https://<id>.<region>.cloud.qdrant.io:6333`) and API key.
3. **Groq** key (console.groq.com/keys) and **Exa** key (dashboard.exa.ai/api-keys).

## 2. Render (API + worker)
1. Push this repository to GitHub.
2. Render dashboard > **New > Blueprint** > select the repo. It reads `render.yaml`.
3. Fill the secrets it asks for: `DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, `GROQ_API_KEY`, `EXA_API_KEY`
   and `CORS_ORIGINS` (set it after step 3, to your Vercel URL).
4. Deploy. The container runs database migrations, then starts the API and the worker. The first build takes
   several minutes (it downloads CPU-only PyTorch and bakes the embedding model into the image).
5. Check `https://<service>.onrender.com/api/health`: it should report `"status":"ok"`.

## 3. Vercel (frontend)
1. Vercel > **Add New Project** > import the same repo.
2. Set **Root Directory** to `frontend`. Framework: Next.js (auto-detected); package manager: pnpm (auto-detected).
3. Environment variable: `NEXT_PUBLIC_API_URL` = `https://<service>.onrender.com` (no trailing slash).
4. Deploy, then put the Vercel URL into Render's `CORS_ORIGINS` and redeploy the API.
   For preview deployments you can also set `CORS_ORIGIN_REGEX=https://.*\.vercel\.app`.

## Variants
- **More headroom:** AWS (App Runner or ECS Fargate with 1 GB), Fly.io or any 1 GB VPS run the same Docker image
  comfortably; set `PORT` if the platform does not (the container listens on `$PORT`, default 8000).
- **Heavier embeddings:** `EMBEDDING_BACKEND=sentence_transformers` (build with `uv sync --extra torch`) uses
  PyTorch: ~520 MB for the model alone, needing a 2 GB instance. Vectors are identical to the default.
- **Separate worker:** set `RUN_WORKER_IN_API=false` on the API and add a Render *background worker* with the same
  image and start command `python -m app.worker`. Keep `STORAGE_BACKEND=postgres` so both services share files.
- **Other hosts:** the Docker image runs anywhere (Fly.io, Railway, a VPS); it listens on `$PORT` (default 8000).

## What was and was not verified
Verified locally: migrations, anonymous identity and isolation, usage limits, Postgres file storage, URL and CORS
parsing, and the production-settings guard (`AUTH_PROVIDER=anonymous` is allowed, `local` is refused).
Not verified: an actual Render or Vercel deployment (I have no access to your accounts), and behaviour behind
Render's proxy under real traffic.
