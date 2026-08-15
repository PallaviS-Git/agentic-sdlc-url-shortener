"""
URL shortener API router.

Endpoints:
  POST   /shorten       — create a short URL
  GET    /{code}        — resolve and redirect to original URL
  DELETE /{code}        — soft-delete a short URL
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from url_shortener.api.deps import get_db, get_url_service
from url_shortener.config import Settings
from url_shortener.schemas.url import ShortenRequest, ShortenResponse
from url_shortener.services.url_service import UrlService

router = APIRouter(tags=["urls"])


@router.post(
    "/shorten",
    response_model=ShortenResponse,
    status_code=201,
    summary="Create a short URL",
    responses={
        422: {"description": "Invalid input (bad URL format, URL too long)"},
        503: {"description": "Unable to generate unique code (transient)"},
    },
)
async def shorten_url(
    body: ShortenRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> ShortenResponse:
    """
    Accept a long URL and return a short code that redirects to it.

    - `url`: Required. Must be a valid http or https URL, ≤ 2048 characters.
    - `expires_in_seconds`: Optional TTL (1 second – 1 year). Omit for no expiry.
    """
    settings: Settings = request.app.state.settings
    service: UrlService = get_url_service(session)

    record = await service.shorten(
        original_url=body.url,
        code_length=settings.short_code_length,
        expires_in_seconds=body.expires_in_seconds,
    )

    short_url = f"{settings.base_url.rstrip('/')}/{record.code}"
    return ShortenResponse(
        short_url=short_url,
        code=record.code,
        original_url=record.original_url,
        created_at=record.created_at,
        expires_at=record.expires_at,
    )


@router.get(
    "/{code}",
    status_code=302,
    summary="Resolve and redirect a short URL",
    response_class=RedirectResponse,
    responses={
        302: {"description": "Redirect to the original URL"},
        404: {"description": "Short code not found, inactive, or expired"},
    },
)
async def redirect_url(
    code: str,
    session: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """
    Redirect the caller to the original URL associated with `code`.

    Returns HTTP 302 on success, HTTP 404 if the code is unknown/inactive/expired.
    """
    service: UrlService = get_url_service(session)
    original_url = await service.resolve(code)
    return RedirectResponse(url=original_url, status_code=302)


@router.delete(
    "/{code}",
    status_code=204,
    summary="Deactivate a short URL",
    response_class=Response,
    responses={
        204: {"description": "Successfully deactivated"},
        404: {"description": "Short code not found or already inactive"},
    },
)
async def delete_url(
    code: str,
    session: AsyncSession = Depends(get_db),
) -> Response:
    """
    Soft-delete a short URL. The code will no longer resolve after this call.

    The record is retained in the database for audit purposes; the code
    cannot be re-used after deactivation.
    """
    service: UrlService = get_url_service(session)
    await service.deactivate(code)
    return Response(status_code=204)
