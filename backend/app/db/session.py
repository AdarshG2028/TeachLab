"""Async SQLAlchemy engine/session management (asyncpg driver)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str, *, pool_size: int = 5, echo: bool = False) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, pool_size=pool_size, pool_pre_ping=True, echo=echo)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Unit-of-work: commits on success, rolls back on error."""
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
