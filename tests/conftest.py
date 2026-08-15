"""
Shared pytest fixtures for the Agentic SDLC test suite.

Fixture scopes:
  - session-scoped: test_engine  (one SQLite DB per pytest run)
  - function-scoped: db_session  (transaction rolled back after each test)
  - function-scoped: http_client (full ASGI stack wired to db_session)
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from orchestrator.core.models import (
    AmbiguityItem,
    Requirement,
    RequirementType,
    StageContext,
    Task,
    WorkflowState,
)
from url_shortener.api.deps import get_db
from url_shortener.config import Environment, Settings
from url_shortener.database import Base, build_session_factory
from url_shortener.main import create_app


# ─── Requirement fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def greenfield_requirement() -> Requirement:
    """Well-defined greenfield requirement for the URL shortener."""
    return Requirement(
        title="URL Shortener Service",
        raw_text=(
            "Build a URL shortener service with core APIs, analytics, "
            "and reliability features."
        ),
        requirement_type=RequirementType.GREENFIELD,
        normalized_text=(
            "Implement a REST API service that accepts a long URL and returns "
            "a short code. The service must redirect short codes to their original "
            "URLs, track click analytics per short code, enforce rate limits, "
            "and expose a /health endpoint."
        ),
        acceptance_criteria=[
            "POST /shorten accepts a URL and returns a short code",
            "GET /{code} redirects (HTTP 302) to the original URL",
            "GET /{code}/stats returns click count and first/last seen timestamps",
            "Unresolvable short code returns HTTP 404",
            "Rate limit exceeded returns HTTP 429",
            "GET /health returns {status: ok}",
        ],
        constraints=[
            "p99 redirect latency < 50 ms (cache hit)",
            "Short codes must be URL-safe alphanumeric (8 characters)",
            "No PII stored in analytics events",
        ],
    )


@pytest.fixture
def brownfield_requirement() -> Requirement:
    """Brownfield requirement to add custom-alias support to existing service."""
    return Requirement(
        title="Custom Alias Support",
        raw_text=(
            "Add support for custom short codes so users can choose their own alias "
            "instead of the auto-generated one."
        ),
        requirement_type=RequirementType.BROWNFIELD,
        normalized_text=(
            "Extend POST /shorten to accept an optional 'alias' field. "
            "If provided, use it as the short code instead of generating one. "
            "Aliases must be unique; conflict returns HTTP 409."
        ),
        acceptance_criteria=[
            "POST /shorten with alias='my-link' creates a short URL at /my-link",
            "Duplicate alias returns HTTP 409 with descriptive error",
            "Existing auto-generate path is unchanged",
        ],
    )


@pytest.fixture
def ambiguous_requirement() -> Requirement:
    """Ambiguous requirement that requires clarification before execution."""
    req = Requirement(
        title="Make It Faster",
        raw_text="make it faster",
        requirement_type=RequirementType.AMBIGUOUS,
    )
    req.ambiguities.extend([
        AmbiguityItem(
            field="target_metric",
            description="'Faster' could mean redirect latency, throughput, or startup time.",
        ),
        AmbiguityItem(
            field="baseline",
            description="No current performance baseline provided.",
        ),
        AmbiguityItem(
            field="scope",
            description="Unclear whether all endpoints or only redirect is in scope.",
        ),
    ])
    return req


# ─── Stage / workflow fixtures ─────────────────────────────────────────────────


@pytest.fixture
def empty_stage_context() -> StageContext:
    """Minimal stage context with no input/output data."""
    return StageContext(stage_name="requirements")


@pytest.fixture
def workflow_state(greenfield_requirement: Requirement) -> WorkflowState:
    """Fresh workflow state for the greenfield requirement."""
    return WorkflowState(requirement=greenfield_requirement)


# ─── Database fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
async def test_engine():
    """
    Session-scoped async SQLite engine.

    StaticPool ensures all sessions share the same in-memory database so
    data written in one session is visible to others in the same test run.
    Tables are created once and dropped at the end of the session.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncSession:
    """
    Function-scoped DB session.

    Wraps each test in a savepoint (nested transaction) that is rolled back
    after the test completes, keeping tests isolated without recreating tables.
    """
    async with test_engine.connect() as conn:
        await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        await conn.begin_nested()  # savepoint

        yield session

        await session.close()
        await conn.rollback()


@pytest.fixture
async def http_client(test_engine) -> AsyncClient:
    """
    Full ASGI test client wired to the in-memory SQLite engine.

    The `get_db` dependency is overridden so every request uses the same
    test engine, ensuring data written in a test is visible to subsequent
    requests within the same test function.
    """
    test_session_factory = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )

    test_settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        environment=Environment.testing,
        base_url="http://testserver",
        log_level="WARNING",
    )

    app = create_app(settings=test_settings)

    # Pre-inject the engine so the lifespan skips re-creation
    app.state.engine = test_engine
    app.state.session_factory = test_session_factory

    # Override get_db so the dependency returns our test session
    async def override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client
