"""
Data access layer for ShortUrl records.

All SQLAlchemy queries are isolated here. The service layer depends on this
class's interface only, so the DB implementation can change without touching
business logic or breaking unit tests (mock this class in unit tests).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from url_shortener.models.url import ShortUrl


class UrlRepository:
    """Async repository for ShortUrl persistence operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        code: str,
        original_url: str,
        expires_at: datetime | None = None,
    ) -> ShortUrl:
        """
        Persist a new ShortUrl record and return it.

        The caller is responsible for ensuring `code` is unique before calling
        this method (i.e. after code_exists() returns False).
        """
        record = ShortUrl(
            code=code,
            original_url=original_url,
            expires_at=expires_at,
        )
        self._session.add(record)
        await self._session.flush()  # Assign DB-generated id without committing
        await self._session.refresh(record)
        return record

    async def get_by_code(self, code: str) -> ShortUrl | None:
        """
        Fetch a ShortUrl by its short code.

        Returns None if the code does not exist (regardless of active/expired state).
        The caller (service) decides what to do with inactive/expired records.
        """
        result = await self._session.execute(
            select(ShortUrl).where(ShortUrl.code == code)
        )
        return result.scalar_one_or_none()

    async def code_exists(self, code: str) -> bool:
        """
        Check whether a short code already exists in the database.

        Used by the service during collision-aware code generation.
        Checks all codes regardless of active/inactive state to prevent
        re-use of previously deactivated codes.
        """
        result = await self._session.execute(
            select(ShortUrl.id).where(ShortUrl.code == code)
        )
        return result.scalar_one_or_none() is not None

    async def deactivate(self, code: str) -> bool:
        """
        Soft-delete a short URL by setting is_active=False.

        Returns:
            True if a row was updated (code existed and was active).
            False if the code was not found or was already inactive.
        """
        result = await self._session.execute(
            update(ShortUrl)
            .where(ShortUrl.code == code, ShortUrl.is_active.is_(True))
            .values(is_active=False)
            .returning(ShortUrl.id)
        )
        await self._session.flush()
        return result.scalar_one_or_none() is not None
