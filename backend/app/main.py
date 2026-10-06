"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routers import jobs, materials, planning, research, workspaces
from app.container import Container
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger
from app.jobs.queue import QUOTA

log = get_logger(__name__)


def _setup_otel(app: FastAPI, settings: Settings) -> None:
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:
        log.warning("otel_not_installed", hint="uv sync --extra otel")
        return
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name}))
    exporter = (OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
                if settings.otel_exporter_otlp_endpoint else ConsoleSpanExporter())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or Container(settings)
        QUOTA.per_user, QUOTA.global_ = settings.daily_job_limit_per_user, settings.daily_job_limit_global
        worker_task = None
        if settings.run_worker_in_api and container is None:
            from app.worker.handlers import HANDLERS
            from app.worker.runner import Worker

            c = app.state.container
            await c.vectors.ensure_collection()
            await asyncio.to_thread(lambda: c.embeddings)  # load the model once, before taking jobs
            worker = Worker(c, HANDLERS)
            worker_task = asyncio.create_task(worker.run())
            log.info("worker_in_api_started")
        try:
            yield
        finally:
            if worker_task is not None:
                worker.stop()
                await asyncio.wait_for(worker_task, timeout=20)
            if container is None:
                await app.state.container.close()

    app = FastAPI(title="TeachLab API", version="0.1.0", lifespan=lifespan,
                  description="Research, teaching-preparation and assessment platform for teachers.")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_origin_regex=settings.cors_origin_regex, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["x-request-id"] = rid
        if not request.url.path.endswith("/events"):
            log.info("request", method=request.method, path=request.url.path, status=response.status_code,
                     ms=round((time.perf_counter() - start) * 1000, 1), request_id=rid)
        return response

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error=str(exc))
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error",
                                                                "message": "Internal server error",
                                                                "details": {}}})

    for r in (workspaces.router, research.router, planning.router, materials.router, jobs.router):
        app.include_router(r, prefix="/api")
    _setup_otel(app, settings)
    return app


app = create_app()
