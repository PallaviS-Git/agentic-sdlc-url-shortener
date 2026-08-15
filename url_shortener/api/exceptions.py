"""
FastAPI exception handlers.

Maps domain exceptions to HTTP responses. Registered on the app in main.py.
This is the only place in the codebase that knows both the domain exception
types AND the HTTP status codes they correspond to.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from url_shortener.services.exceptions import CodeGenerationError, ShortCodeNotFoundError


async def short_code_not_found_handler(
    request: Request, exc: ShortCodeNotFoundError
) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"error": "not_found", "detail": str(exc)},
    )


async def code_generation_error_handler(
    request: Request, exc: CodeGenerationError
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "code_generation_failed",
            "detail": "Service temporarily unable to generate a unique short code. Retry shortly.",
        },
    )
