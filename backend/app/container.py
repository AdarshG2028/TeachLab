"""Dependency container: builds providers and services from Settings.

Providers are created lazily so the API process never loads the embedding
model (only the worker does). Tests replace providers by assigning the
private attributes before first use (e.g. ``container._llm = FakeLLM()``).
"""

from __future__ import annotations

from typing import Any

from app.auth.base import AuthProvider
from app.core.config import Settings
from app.core.errors import ConfigurationError
from app.db.session import Database
from app.providers.embeddings.base import EmbeddingProvider
from app.providers.llm.base import LLMProvider
from app.providers.pdf.base import PDFProvider
from app.providers.search.base import SearchProvider
from app.providers.storage.base import StorageProvider
from app.providers.storage.local import LocalStorage
from app.providers.vector.base import VectorStore


class Container:
    def __init__(self, settings: Settings, *, db: Database | None = None) -> None:
        self.settings = settings
        self.db = db or Database(settings.database_url, pool_size=settings.db_pool_size)
        self.auth: AuthProvider = self._build_auth()
        self.storage: StorageProvider = LocalStorage(settings.storage_dir)
        self._llm: LLMProvider | None = None
        self._search: SearchProvider | None = None
        self._embeddings: EmbeddingProvider | None = None
        self._vectors: VectorStore | None = None
        self._pdf: PDFProvider | None = None
        self._services: dict[str, Any] = {}

    def _build_auth(self) -> AuthProvider:
        if self.settings.auth_provider == "local":
            from app.auth.local import LocalAuthProvider

            return LocalAuthProvider(self.settings.default_user_id)
        raise ConfigurationError(f"Unknown AUTH_PROVIDER {self.settings.auth_provider}")

    # ------------------------------------------------------------------ providers

    @property
    def llm(self) -> LLMProvider:
        if self._llm is None:
            if self.settings.llm_provider == "groq":
                from app.providers.llm.groq import GroqLLMProvider

                self._llm = GroqLLMProvider(self.settings)
            elif self.settings.llm_provider == "fake":
                from app.providers.llm.fake import FakeLLMProvider

                self._llm = FakeLLMProvider()
            else:
                raise ConfigurationError(f"Unknown LLM_PROVIDER {self.settings.llm_provider}")
        return self._llm

    @property
    def search(self) -> SearchProvider:
        if self._search is None:
            if self.settings.search_provider == "tavily":
                from app.providers.search.tavily import TavilySearchProvider

                self._search = TavilySearchProvider(self.settings)
            elif self.settings.search_provider == "exa":
                from app.providers.search.exa import ExaSearchProvider

                self._search = ExaSearchProvider(self.settings)
            elif self.settings.search_provider == "fixture":
                from app.providers.search.fixture import FixtureSearchProvider

                self._search = FixtureSearchProvider(self.settings.search_fixture_dir)
            else:
                raise ConfigurationError(f"Unknown SEARCH_PROVIDER {self.settings.search_provider}")
        return self._search

    @property
    def embeddings(self) -> EmbeddingProvider:
        if self._embeddings is None:
            from app.providers.embeddings.bge import get_bge

            self._embeddings = get_bge(self.settings)
        return self._embeddings

    @property
    def vectors(self) -> VectorStore:
        if self._vectors is None:
            from app.providers.vector.qdrant import QdrantVectorStore, collection_name

            s = self.settings
            self._vectors = QdrantVectorStore.from_settings(
                url=s.qdrant_url or None, api_key=s.qdrant_api_key, path=s.qdrant_path,
                collection=collection_name(s.qdrant_collection_prefix, s.embedding_model, s.embedding_dim),
                dim=s.embedding_dim)
        return self._vectors

    @property
    def pdf(self) -> PDFProvider:
        if self._pdf is None:
            from app.providers.pdf.reportlab_provider import ReportLabPDFProvider

            self._pdf = ReportLabPDFProvider()
        return self._pdf

    # ------------------------------------------------------------------ services

    def _svc(self, name: str, factory) -> Any:
        if name not in self._services:
            self._services[name] = factory()
        return self._services[name]

    @property
    def ingestion(self):
        from app.services.research.ingestion import IngestionService

        return self._svc("ingestion", lambda: IngestionService(self))

    @property
    def retrieval(self):
        from app.services.research.retrieval import RetrievalService

        return self._svc("retrieval", lambda: RetrievalService(self))

    @property
    def research(self):
        from app.services.research.agent import ResearchAgent

        return self._svc("research", lambda: ResearchAgent(self))

    @property
    def tutor(self):
        from app.services.tutor.guide import GuidedTutor

        return self._svc("tutor", lambda: GuidedTutor(self))

    @property
    def roadmap(self):
        from app.services.roadmap.service import RoadmapService

        return self._svc("roadmap", lambda: RoadmapService(self))

    @property
    def coverage(self):
        from app.services.roadmap.coverage import CoverageService

        return self._svc("coverage", lambda: CoverageService(self))

    @property
    def notes(self):
        from app.services.content.notes import NotesService

        return self._svc("notes", lambda: NotesService(self))

    @property
    def assessment(self):
        from app.services.assessment.service import AssessmentService

        return self._svc("assessment", lambda: AssessmentService(self))

    @property
    def exports(self):
        from app.services.export.service import ExportService

        return self._svc("exports", lambda: ExportService(self))

    async def close(self) -> None:
        if self._vectors is not None and hasattr(self._vectors, "close"):
            await self._vectors.close()
        await self.db.dispose()
