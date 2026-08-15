"""
ORM model for a shortened URL record.

One row per short code. Deletion is soft (is_active=False) to preserve
audit history and prevent short-code re-use.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from url_shortener.database import Base


class ShortUrl(Base):
    __tablename__ = "short_urls"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    code: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        index=True,
        comment="URL-safe alphanumeric short identifier",
    )
    original_url: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="The full destination URL",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="False means soft-deleted; code is no longer resolvable",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="UTC expiry time; NULL means never expires",
    )

    # ── Computed properties ────────────────────────────────────────────────────

    @property
    def is_expired(self) -> bool:
        """True if the record has passed its expiry time."""
        if self.expires_at is None:
            return False
        now = datetime.now(timezone.utc)
        exp = self.expires_at
        # SQLite returns naive datetimes; treat them as UTC
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now > exp

    @property
    def is_resolvable(self) -> bool:
        """True only if the record is active and not expired."""
        return self.is_active and not self.is_expired

    def __repr__(self) -> str:
        return f"ShortUrl(code={self.code!r}, active={self.is_active}, expired={self.is_expired})"
