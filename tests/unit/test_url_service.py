"""
Unit tests for UrlService and generate_short_code.

All DB interaction is mocked via pytest-mock. No database, no network.
"""
from __future__ import annotations

import string
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from url_shortener.models.url import ShortUrl
from url_shortener.services.exceptions import CodeGenerationError, ShortCodeNotFoundError
from url_shortener.services.url_service import UrlService, generate_short_code, _ALPHABET


# ─── generate_short_code ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestGenerateShortCode:
    def test_default_length_is_8(self) -> None:
        code = generate_short_code()
        assert len(code) == 8

    def test_custom_length(self) -> None:
        for length in [4, 8, 12, 16]:
            code = generate_short_code(length)
            assert len(code) == length

    def test_only_allowed_characters(self) -> None:
        for _ in range(100):
            code = generate_short_code()
            assert all(c in _ALPHABET for c in code), f"Unexpected char in {code!r}"

    def test_lowercase_only(self) -> None:
        for _ in range(50):
            code = generate_short_code()
            assert code == code.lower()

    def test_codes_are_different(self) -> None:
        codes = {generate_short_code() for _ in range(200)}
        # With 36^8 ≈ 2.8T possible codes, all 200 should be unique
        assert len(codes) == 200

    def test_url_safe_characters_only(self) -> None:
        """Short codes must be safe to embed in a URL path without encoding."""
        unsafe = set(' !"#$%&\'()*+,/:;<=>?@[\\]^`{|}~')
        for _ in range(100):
            code = generate_short_code()
            assert not set(code) & unsafe


# ─── UrlService fixtures ──────────────────────────────────────────────────────


def _make_short_url(
    code: str = "abc12345",
    original_url: str = "https://example.com",
    is_active: bool = True,
    expires_at: datetime | None = None,
) -> ShortUrl:
    """Build an unsaved ShortUrl instance for use in assertions."""
    record = ShortUrl(
        code=code,
        original_url=original_url,
        is_active=is_active,
        expires_at=expires_at,
    )
    record.created_at = datetime.now(timezone.utc)
    return record


def _make_service(
    code_exists: bool = False,
    existing_record: ShortUrl | None = None,
    deactivate_success: bool = True,
) -> tuple[UrlService, MagicMock]:
    """Build a UrlService with a fully-mocked repository."""
    repo = MagicMock()
    repo.code_exists = AsyncMock(return_value=code_exists)
    repo.get_by_code = AsyncMock(return_value=existing_record)
    repo.create = AsyncMock(
        return_value=_make_short_url() if existing_record is None else existing_record
    )
    repo.deactivate = AsyncMock(return_value=deactivate_success)
    return UrlService(repo), repo


# ─── UrlService.shorten ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestUrlServiceShorten:
    async def test_shorten_creates_record(self) -> None:
        service, repo = _make_service(code_exists=False)
        result = await service.shorten("https://example.com")
        repo.create.assert_called_once()
        assert result.original_url == "https://example.com"

    async def test_shorten_uses_configured_code_length(self) -> None:
        service, repo = _make_service()
        await service.shorten("https://example.com", code_length=12)
        call_kwargs = repo.create.call_args
        code = call_kwargs.kwargs["code"]
        assert len(code) == 12

    async def test_shorten_retries_on_collision(self) -> None:
        """If code_exists returns True once then False, create is called once."""
        repo = MagicMock()
        repo.code_exists = AsyncMock(side_effect=[True, False])
        repo.create = AsyncMock(return_value=_make_short_url())
        service = UrlService(repo)

        await service.shorten("https://example.com")
        assert repo.code_exists.call_count == 2
        assert repo.create.call_count == 1

    async def test_shorten_raises_after_max_retries(self) -> None:
        """If every generated code collides, raise CodeGenerationError."""
        repo = MagicMock()
        repo.code_exists = AsyncMock(return_value=True)  # always collides
        service = UrlService(repo)

        with pytest.raises(CodeGenerationError):
            await service.shorten("https://example.com")

    async def test_shorten_sets_expires_at_when_ttl_provided(self) -> None:
        service, repo = _make_service()
        await service.shorten("https://example.com", expires_in_seconds=3600)
        call_kwargs = repo.create.call_args.kwargs
        assert call_kwargs["expires_at"] is not None
        expected = datetime.now(timezone.utc) + timedelta(seconds=3600)
        delta = abs((call_kwargs["expires_at"] - expected).total_seconds())
        assert delta < 2

    async def test_shorten_no_expires_at_when_no_ttl(self) -> None:
        service, repo = _make_service()
        await service.shorten("https://example.com", expires_in_seconds=None)
        call_kwargs = repo.create.call_args.kwargs
        assert call_kwargs["expires_at"] is None

    async def test_shorten_does_not_commit(self) -> None:
        """Service never commits; that is the dependency's responsibility."""
        service, repo = _make_service()
        await service.shorten("https://example.com")
        assert not hasattr(repo, "commit") or not repo.commit.called


# ─── UrlService.resolve ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestUrlServiceResolve:
    async def test_resolve_returns_original_url(self) -> None:
        record = _make_short_url(original_url="https://target.example.com")
        service, _ = _make_service(existing_record=record)
        result = await service.resolve("abc12345")
        assert result == "https://target.example.com"

    async def test_resolve_raises_when_code_not_found(self) -> None:
        service, repo = _make_service()
        repo.get_by_code = AsyncMock(return_value=None)
        with pytest.raises(ShortCodeNotFoundError) as exc_info:
            await service.resolve("missing")
        assert exc_info.value.code == "missing"

    async def test_resolve_raises_when_inactive(self) -> None:
        record = _make_short_url(is_active=False)
        service, _ = _make_service(existing_record=record)
        with pytest.raises(ShortCodeNotFoundError):
            await service.resolve("abc12345")

    async def test_resolve_raises_when_expired(self) -> None:
        record = _make_short_url(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        service, _ = _make_service(existing_record=record)
        with pytest.raises(ShortCodeNotFoundError):
            await service.resolve("abc12345")

    async def test_resolve_succeeds_when_not_yet_expired(self) -> None:
        record = _make_short_url(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        service, _ = _make_service(existing_record=record)
        result = await service.resolve("abc12345")
        assert result == record.original_url


# ─── UrlService.deactivate ────────────────────────────────────────────────────


@pytest.mark.unit
class TestUrlServiceDeactivate:
    async def test_deactivate_succeeds(self) -> None:
        service, repo = _make_service(deactivate_success=True)
        await service.deactivate("abc12345")  # should not raise
        repo.deactivate.assert_called_once_with("abc12345")

    async def test_deactivate_raises_when_not_found(self) -> None:
        service, _ = _make_service(deactivate_success=False)
        with pytest.raises(ShortCodeNotFoundError):
            await service.deactivate("nonexistent")
