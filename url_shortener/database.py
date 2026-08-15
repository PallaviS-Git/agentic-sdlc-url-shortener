"""
Database engine and session factory.

All SQLAlchemy async infrastructure lives here.
Consumers (deps.py, tests) import `Base`, `build_engine`, and `build_session_factory`.
The `Base` class is the single declarative base shared by every ORM model.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def build_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """
    Create an async SQLAlchemy engine.

    Args:
        database_url: Full async connection URL (e.g. postgresql+asyncpg://...).
        echo: If True, log all SQL statements. Use only for debugging.
    """
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,   # Verify connection liveness before checkout
        pool_recycle=3600,    # Recycle stale connections every hour
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """
    Create a session factory bound to the given engine.

    expire_on_commit=False: objects remain accessible after commit without
    triggering an implicit SELECT, which is important in async code where
    lazy loading is not available.
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
