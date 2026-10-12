"""Blob storage in PostgreSQL (``blobs`` table).

Lets the API and worker run as separate services without a shared disk or an object store. Suited to the
sizes this app stores (PDFs of tens of KB, uploads capped by MAX_UPLOAD_MB). For large volumes implement
StorageProvider on S3/R2 instead.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.errors import NotFoundError, ValidationFailed
from app.db.models import Blob
from app.db.session import Database
from app.providers.storage.base import StorageProvider
from app.providers.storage.local import _KEY_RE


class PostgresStorage(StorageProvider):
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _check(key: str) -> None:
        if not _KEY_RE.match(key) or ".." in key.split("/"):
            raise ValidationFailed("Invalid storage key")

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._check(key)
        async with self.db.session() as s:
            stmt = pg_insert(Blob).values(key=key, content_type=content_type, data=data, size=len(data))
            await s.execute(stmt.on_conflict_do_update(
                index_elements=["key"], set_={"data": stmt.excluded.data, "size": stmt.excluded.size,
                                              "content_type": stmt.excluded.content_type}))
        return key

    async def get(self, key: str) -> bytes:
        self._check(key)
        async with self.db.session() as s:
            data = (await s.execute(select(Blob.data).where(Blob.key == key))).scalar_one_or_none()
        if data is None:
            raise NotFoundError("Stored file not found")
        return bytes(data)

    async def exists(self, key: str) -> bool:
        self._check(key)
        async with self.db.session() as s:
            return (await s.execute(select(Blob.key).where(Blob.key == key))).scalar_one_or_none() is not None

    async def delete(self, key: str) -> None:
        self._check(key)
        async with self.db.session() as s:
            await s.execute(delete(Blob).where(Blob.key == key))
