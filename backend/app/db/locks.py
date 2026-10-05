"""PostgreSQL advisory locks."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def workspace_lock(session: AsyncSession, workspace_id: str, purpose: str) -> None:
    """Transaction-scoped advisory lock serializing writes of one kind (e.g. roadmap versions) per workspace."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"{purpose}:{workspace_id}"})
