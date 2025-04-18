"""Async engine, session factory and FastAPI dependency."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import get_settings
from src.db.base import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _is_memory_sqlite(url: str) -> bool:
    """In-memory SQLite gets a StaticPool, which takes no sizing arguments.

    File-backed SQLite gets an AsyncAdaptedQueuePool and accepts them, so the
    check has to be narrower than "is this SQLite". It was not, and a
    file-SQLite deployment silently ran on the 5 + 10 default pool: a load
    test at 5,000 concurrent users produced "QueuePool limit of size 5
    overflow 10 reached" rather than using the configured 10 + 20.
    """
    return url.startswith("sqlite") and (":memory:" in url or url.endswith("sqlite://"))


def get_engine() -> AsyncEngine:
    """Lazily build the engine so importing this module never opens a socket."""
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, object] = {"echo": settings.db_echo, "future": True}
        if not _is_memory_sqlite(settings.database_url):
            kwargs |= {
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_pre_ping": True,
                # Lambda freezes the container between invocations; a recycled
                # connection avoids handing out a socket NAT has already dropped.
                "pool_recycle": 900,
                # Fail fast instead of blocking a request for 30s behind an
                # exhausted pool — a queued request that eventually 500s is
                # worse than one that sheds load immediately.
                "pool_timeout": settings.db_pool_timeout,
            }
        _engine = create_async_engine(settings.database_url, **kwargs)
        logger.info(
            "Engine created (pool=%s, size=%s, overflow=%s)",
            type(_engine.pool).__name__,
            kwargs.get("pool_size", "n/a"),
            kwargs.get("max_overflow", "n/a"),
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Commits on success, rolls back on any exception."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Same contract as :func:`get_db` for code outside the request cycle."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables directly from metadata.

    Convenience for tests and first-run local dev. Alembic owns the schema in
    every deployed environment — see ``migrations/``.
    """
    # Imported for the side effect of registering mappers on Base.metadata.
    from src.models import cost  # noqa: F401

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema ensured")


async def dispose_engine() -> None:
    """Close the pool. Called on API shutdown and at the end of test sessions."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
