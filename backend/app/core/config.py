"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = BACKEND_DIR.parent


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool = False

    # --- PostgreSQL (source of truth) ---
    database_url: str = "postgresql+asyncpg://teachlab:teachlab@localhost:5432/teachlab"
    test_database_url: str = "postgresql+asyncpg://teachlab:teachlab@localhost:5432/teachlab_test"
    db_pool_size: int = 5

    # --- Qdrant ---
    # Set QDRANT_URL for a server (local Docker or Qdrant Cloud). Leave empty to use
    # embedded local mode at QDRANT_PATH (":memory:" for an ephemeral store).
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_path: str = ":memory:"
    qdrant_collection_prefix: str = "chunks"

    # --- LLM (Groq, OpenAI-compatible) ---
    llm_provider: str = "groq"  # groq | fake (fake only allowed outside production)
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_model_smart: str = "openai/gpt-oss-120b"
    llm_model_fast: str = "openai/gpt-oss-20b"
    llm_context_tokens_smart: int = 32_000
    llm_context_tokens_fast: int = 32_000
    llm_max_output_tokens: int = 4_096
    llm_tokens_per_minute: int = 8_000
    llm_reasoning_effort: str | None = "low"  # sent only to reasoning models (gpt-oss); low keeps JSON within budget
    llm_max_retries: int = 4
    llm_timeout_seconds: float = 90.0
    llm_temperature: float = 0.3

    # --- Search (Tavily) ---
    search_provider: str = "tavily"  # tavily | exa | fixture (fixture only outside production)
    tavily_api_key: str | None = None
    exa_api_key: str | None = None
    search_max_results: int = 5
    search_cache_ttl_hours: int = 72
    search_fixture_dir: Path = BACKEND_DIR / "tests" / "fixtures" / "search"

    # --- Embeddings (local BGE) ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # fastembed = ONNX Runtime, ~200 MB RAM, no PyTorch (default);
    # sentence_transformers = PyTorch, ~520 MB RAM (needs: uv sync --extra torch)
    embedding_backend: str = "fastembed"
    embedding_threads: int = 2
    embedding_device: str = "cpu"  # sentence_transformers only
    embedding_batch_size: int = 32
    embedding_query_instruction: str = "Represent this sentence for searching relevant passages: "
    embedding_cache_dir: Path | None = None

    # --- Storage ---
    storage_backend: str = "local"  # local (filesystem) | postgres (blobs table; works across services)
    storage_dir: Path = BACKEND_DIR / "storage"

    # --- Public-deployment safety limits (0 disables) ---
    daily_job_limit_per_user: int = 0
    daily_job_limit_global: int = 0
    max_workspaces_per_user: int = 0

    # --- Auth ---
    # anonymous: no sign-in; each browser holds a random unguessable client id (safe for production)
    # local:     X-User-Id header, trusts the caller (development/test only)
    auth_provider: str = "local"
    default_user_id: str = "local-teacher"

    # --- API ---
    # JSON list or comma-separated, e.g. https://app.vercel.app,https://www.example.com
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])
    cors_origin_regex: str | None = None  # e.g. https://.*\.vercel\.app for preview deployments

    # --- Worker ---
    worker_concurrency: int = 2
    run_worker_in_api: bool = False  # run the job worker inside the API process (single-service deploys)
    worker_poll_seconds: float = 2.0
    worker_lease_seconds: int = 300
    job_default_max_attempts: int = 3

    # --- Research / ingestion limits ---
    max_upload_mb: int = 20
    fetch_max_mb: int = 15
    fetch_timeout_seconds: float = 20.0
    chunk_target_tokens: int = 350
    chunk_overlap_tokens: int = 50
    retrieval_top_k: int = 8
    retrieval_min_score: float = 0.55
    sufficiency_min_hits: int = 3

    # --- Conversation context ---
    context_recent_messages: int = 12
    summary_trigger_tokens: int = 3_000
    context_budget_ratio: float = 0.85  # safety margin over approximate token counts

    # --- Assessment ---
    code_verification_enabled: bool = False
    code_verification_timeout_seconds: float = 5.0
    duplicate_question_threshold: float = 0.92
    grounding_min_score: float = 0.45

    # --- Observability ---
    otel_enabled: bool = False
    otel_service_name: str = "teachlab-api"
    otel_exporter_otlp_endpoint: str | None = None

    @field_validator("database_url", "test_database_url", mode="before")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Accept provider URLs (postgres://, postgresql://, ?sslmode=require) and use the asyncpg driver."""
        if not isinstance(v, str):
            return v
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                v = "postgresql+asyncpg://" + v[len(prefix):]
        return v.replace("sslmode=", "ssl=")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [o.strip().rstrip("/") for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        if self.env == Environment.PRODUCTION:
            problems = []
            if self.auth_provider == "local":
                problems.append("AUTH_PROVIDER=local is not allowed in production")
            if self.llm_provider == "fake":
                problems.append("LLM_PROVIDER=fake is not allowed in production")
            if self.search_provider == "fixture":
                problems.append("SEARCH_PROVIDER=fixture is not allowed in production")
            if problems:
                raise ValueError("; ".join(problems))
        return self

    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    return Settings()
