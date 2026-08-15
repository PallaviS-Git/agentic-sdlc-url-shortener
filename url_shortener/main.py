from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.observability.logging import configure_logging
from url_shortener.api.exceptions import (
    code_generation_error_handler,
    short_code_not_found_handler,
)
from url_shortener.api.urls import router as url_router
from url_shortener.config import Settings, get_settings
from url_shortener.database import build_engine, build_session_factory
from url_shortener.services.exceptions import CodeGenerationError, ShortCodeNotFoundError

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage application-level resources.

    DB setup: if `app.state.session_factory` is already populated (e.g. by a
    test fixture that injects a pre-created engine), skip engine creation so
    tests remain in full control of the database connection.
    """
    settings: Settings = app.state.settings
    configure_logging(
        level=settings.log_level,
        environment=settings.environment.value,
    )

    engine_owned = False
    if not getattr(app.state, "session_factory", None):
        engine = build_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = build_session_factory(engine)
        engine_owned = True

    logger.info(
        "application_startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
    )
    yield

    if engine_owned and hasattr(app.state, "engine"):
        await app.state.engine.dispose()

    logger.info("application_shutdown", app=settings.app_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    App factory.

    Accepts an optional Settings instance so tests can inject custom config
    (e.g. a SQLite database URL) without touching environment variables.
    """
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Agentic SDLC demonstration system. "
            "The URL shortener is the production artifact produced by the orchestrator."
        ),
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    # ── Domain exception → HTTP response mappings ─────────────────────────────
    app.add_exception_handler(ShortCodeNotFoundError, short_code_not_found_handler)
    app.add_exception_handler(CodeGenerationError, code_generation_error_handler)

    # ── Ops endpoints ─────────────────────────────────────────────────────────
    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    # ── Feature routers ───────────────────────────────────────────────────────
    app.include_router(url_router)

    return app


# Module-level app instance used by uvicorn and the Dockerfile CMD
app = create_app()
