"""
Business logic for the URL shortener.

UrlService coordinates the repository and encodes the rules:
  - How short codes are generated and checked for uniqueness
  - When a URL is considered resolvable vs. not found
  - What 'deactivate' means (soft delete, no re-use)

The service has no knowledge of FastAPI, HTTP, or SQLAlchemy internals.
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone

import structlog

from url_shortener.models.url import ShortUrl
from url_shortener.repositories.url_repo import UrlRepository
from url_shortener.services.exceptions import CodeGenerationError, ShortCodeNotFoundError

logger = structlog.get_logger(__name__)

# ─── Short-code generation ────────────────────────────────────────────────────

_ALPHABET = string.ascii_lowercase + string.digits  # 36 URL-safe chars
_MAX_GENERATION_ATTEMPTS = 10


def generate_short_code(length: int = 8) -> str:
    """
    Generate a cryptographically random URL-safe short code.

    Uses `secrets.choice` (CSPRNG) over `random.choice` to prevent
    code enumeration attacks. The 36-char alphabet gives 36^8 ≈ 2.8 trillion
    possible codes — negligible collision rate at realistic scale.

    Args:
        length: Number of characters in the code.

    Returns:
        A lowercase alphanumeric string of the requested length.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


# ─── Service ──────────────────────────────────────────────────────────────────


class UrlService:
    """
    Orchestrates URL shortening and resolution.

    Depends on UrlRepository (injected); never imports SQLAlchemy directly.
    """

    def __init__(self, repository: UrlRepository) -> None:
        self._repo = repository

    async def shorten(
        self,
        original_url: str,
        code_length: int = 8,
        expires_in_seconds: int | None = None,
    ) -> ShortUrl:
        """
        Create a new short URL record.

        Generates a unique short code, retrying up to _MAX_GENERATION_ATTEMPTS
        times if a collision is detected in the DB.

        Args:
            original_url:       The destination URL to store.
            code_length:        Length of the generated code (from settings).
            expires_in_seconds: Optional TTL; if set, record expires after this many seconds.

        Returns:
            The persisted ShortUrl record.

        Raises:
            CodeGenerationError: All generation attempts produced duplicate codes.
        """
        expires_at: datetime | None = None
        if expires_in_seconds is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)

        for attempt in range(_MAX_GENERATION_ATTEMPTS):
            code = generate_short_code(code_length)
            if not await self._repo.code_exists(code):
                record = await self._repo.create(
                    code=code,
                    original_url=original_url,
                    expires_at=expires_at,
                )
                logger.info("url_shortened", code=code, attempt=attempt + 1)
                return record

        logger.error("code_generation_exhausted", attempts=_MAX_GENERATION_ATTEMPTS)
        raise CodeGenerationError(
            f"Could not generate a unique code after {_MAX_GENERATION_ATTEMPTS} attempts"
        )

    async def resolve(self, code: str) -> str:
        """
        Return the original URL for an active, non-expired short code.

        Args:
            code: The short code to look up.

        Returns:
            The original destination URL string.

        Raises:
            ShortCodeNotFoundError: Code not found, inactive, or expired.
        """
        record = await self._repo.get_by_code(code)
        if record is None or not record.is_resolvable:
            raise ShortCodeNotFoundError(code)
        logger.info("url_resolved", code=code)
        return record.original_url

    async def deactivate(self, code: str) -> None:
        """
        Soft-delete a short URL (sets is_active=False).

        Args:
            code: The short code to deactivate.

        Raises:
            ShortCodeNotFoundError: Code not found or already inactive.
        """
        success = await self._repo.deactivate(code)
        if not success:
            raise ShortCodeNotFoundError(code)
        logger.info("url_deactivated", code=code)
