"""Filesystem storage for local development.

For production use an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO) by
implementing StorageProvider with boto3/aioboto3; keys are already
path-like (``{workspace_id}/artifacts/{id}.pdf``) and map directly to object keys.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.core.errors import NotFoundError, ValidationFailed
from app.providers.storage.base import StorageProvider

_KEY_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,400}$")


class LocalStorage(StorageProvider):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY_RE.match(key) or ".." in key.split("/"):
            raise ValidationFailed("Invalid storage key")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValidationFailed("Invalid storage key")
        return path

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)  # atomic on the same filesystem

        await asyncio.to_thread(_write)
        return key

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise NotFoundError("Stored file not found")
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await asyncio.to_thread(path.unlink)
