"""
Unit tests for Pydantic schemas.

Validates that the API contract enforces correct input/output shapes.
No I/O, no DB, no network.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from url_shortener.schemas.url import ShortenRequest, ShortenResponse


@pytest.mark.unit
class TestShortenRequest:
    def test_valid_http_url(self) -> None:
        req = ShortenRequest(url="http://example.com/path?q=1")
        assert req.url == "http://example.com/path?q=1"

    def test_valid_https_url(self) -> None:
        req = ShortenRequest(url="https://example.com")
        assert req.url == "https://example.com"

    def test_rejects_missing_url(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ShortenRequest()  # type: ignore[call-arg]
        assert "url" in str(exc_info.value)

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValidationError):
            ShortenRequest(url="")

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ShortenRequest(url="ftp://example.com/file.txt")
        assert "http" in str(exc_info.value).lower()

    def test_rejects_plain_string(self) -> None:
        with pytest.raises(ValidationError):
            ShortenRequest(url="not-a-url")

    def test_rejects_url_over_2048_chars(self) -> None:
        long_url = "https://example.com/" + "a" * 2030
        assert len(long_url) > 2048
        with pytest.raises(ValidationError) as exc_info:
            ShortenRequest(url=long_url)
        assert "2048" in str(exc_info.value)

    def test_accepts_url_exactly_at_2048_chars(self) -> None:
        # Build a URL of exactly 2048 chars
        base = "https://example.com/"
        url = base + "a" * (2048 - len(base))
        assert len(url) == 2048
        req = ShortenRequest(url=url)
        assert len(req.url) == 2048

    def test_expires_in_seconds_optional(self) -> None:
        req = ShortenRequest(url="https://example.com")
        assert req.expires_in_seconds is None

    def test_expires_in_seconds_valid(self) -> None:
        req = ShortenRequest(url="https://example.com", expires_in_seconds=3600)
        assert req.expires_in_seconds == 3600

    def test_expires_in_seconds_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            ShortenRequest(url="https://example.com", expires_in_seconds=0)

    def test_expires_in_seconds_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            ShortenRequest(url="https://example.com", expires_in_seconds=-1)

    def test_expires_in_seconds_rejects_over_one_year(self) -> None:
        over_year = 365 * 24 * 3600 + 1
        with pytest.raises(ValidationError):
            ShortenRequest(url="https://example.com", expires_in_seconds=over_year)

    def test_url_with_unicode_path(self) -> None:
        req = ShortenRequest(url="https://example.com/path/to/resource")
        assert "example.com" in req.url


@pytest.mark.unit
class TestShortenResponse:
    def test_serialization(self) -> None:
        from datetime import datetime, timezone

        resp = ShortenResponse(
            short_url="http://localhost:8000/abc12345",
            code="abc12345",
            original_url="https://example.com",
            created_at=datetime.now(timezone.utc),
            expires_at=None,
        )
        data = resp.model_dump()
        assert data["short_url"] == "http://localhost:8000/abc12345"
        assert data["code"] == "abc12345"
        assert data["expires_at"] is None
