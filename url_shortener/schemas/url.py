"""
Pydantic v2 schemas that define the public API contract.

Kept separate from ORM models deliberately: the persistence shape and the
API shape can evolve independently without coupling migrations to API changes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, HttpUrl, field_validator


# ─── Request ──────────────────────────────────────────────────────────────────


class ShortenRequest(BaseModel):
    """Payload for POST /shorten."""

    url: Annotated[str, Field(description="The destination URL to shorten (http/https only, ≤ 2048 chars)")]
    expires_in_seconds: Annotated[
        int | None,
        Field(
            default=None,
            gt=0,
            le=365 * 24 * 3600,
            description="Optional TTL in seconds (1 second – 1 year). Omit for no expiry.",
        ),
    ] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if len(v) > 2048:
            raise ValueError("URL exceeds maximum length of 2048 characters")
        # Explicit scheme guard — Pydantic v2's HttpUrl is lenient about schemes
        lower = v.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            raise ValueError("Invalid URL. Must use http or https scheme.")
        try:
            HttpUrl(v)
        except Exception:
            raise ValueError("Invalid URL. Must be a valid http or https URL.")
        return v


# ─── Response ─────────────────────────────────────────────────────────────────


class ShortenResponse(BaseModel):
    """Response body for a successfully created short URL."""

    short_url: str = Field(description="The fully-qualified shortened URL")
    code: str = Field(description="The short code component")
    original_url: str = Field(description="The original destination URL")
    created_at: datetime
    expires_at: datetime | None = None


# ─── Error ────────────────────────────────────────────────────────────────────


class ErrorDetail(BaseModel):
    """Uniform error response body."""

    error: str = Field(description="Machine-readable error type")
    detail: str = Field(description="Human-readable explanation")
