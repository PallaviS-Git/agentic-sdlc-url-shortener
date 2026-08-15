"""
FastAPI dependencies.

Each dependency is a callable that FastAPI resolves via Depends().
Keeping them here prevents circular imports between routers.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from url_shortener.repositories.url_repo import UrlRepository
from url_shortener.services.url_service import UrlService


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """
    Yield an async database session scoped to a single request.

    Commits on success, rolls back on any exception. The session_factory
    is stored on app.state by the lifespan so it can be swapped for a test
    factory in integration tests.
    """
    async with request.app.state.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_url_service(session: AsyncSession) -> UrlService:
    """
    Build a UrlService wired to the request-scoped DB session.

    Thin factory — keeps the router free of repository/service construction.
    """
    return UrlService(UrlRepository(session))
