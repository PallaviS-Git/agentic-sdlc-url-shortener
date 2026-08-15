"""
Brownfield SDLC scenario: Enforce rate limiting on existing URL shortener.

Background
──────────
The URL shortener codebase (url_shortener/) has been running in production.
The Settings class (url_shortener/config.py) declares a ``rate_limit_per_minute``
field (value=100) that was added as a placeholder but was never wired to any
enforcement mechanism.  The POST /shorten endpoint has no rate limiting.

Enhancement request
───────────────────
"Enforce the existing rate_limit_per_minute configuration field on
POST /shorten using a Redis-backed sliding window algorithm.  The
GET /{code} redirect endpoint must remain completely unchanged — it is
the performance-critical path."

What makes this a brownfield scenario
──────────────────────────────────────
• The agent FIRST analyses the existing codebase before proposing any change.
• It explicitly catalogues which modules are impacted vs preserved.
• The change plan targets only three locations; every other file is
  explicitly marked "DO NOT MODIFY".
• Regression tests cover all three existing endpoints to ensure no regressions.

Stage topology (linear — each stage builds on the previous)
────────────────────────────────────────────────────────────

  codebase_analysis
          ↓
  impact_analysis
          ↓
  risk_assessment
          ↓
  change_planning          [SIGNIFICANT · requires_approval]
          ↓
  regression_test_planning
          ↓
  validation               [HIGH_IMPACT · requires_approval]

Public API
──────────
    create_brownfield_requirement()  →  Requirement
    create_brownfield_workflow()     →  WorkflowDefinition
    create_brownfield_stages()       →  dict[str, BaseStage]
    run_brownfield_scenario(...)     →  WorkflowState  (async)
"""
from __future__ import annotations

import json
from typing import Any

from orchestrator.core.autonomy import ActionImpact, ApprovalGateway, HighImpactActionType
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.governance import PolicyEngine
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.models import (
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    Task,
    TaskStatus,
)
from orchestrator.core.results import (
    Artifact,
    ArtifactType,
    Decision,
    DecisionType,
    Risk,
    RiskSeverity,
    ValidationResult,
    ValidationSeverity,
)
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── Default enhancement request ─────────────────────────────────────────────

DEFAULT_REQUIREMENT_TEXT = """
BROWNFIELD ENHANCEMENT — Rate Limiting Enforcement

Context:
  The URL shortener service is running in production.  The Settings class
  has a `rate_limit_per_minute` field (default=100) that was added as a
  placeholder during initial development but was NEVER wired to any
  enforcement code.  POST /shorten currently has no rate limiting at all.

Enhancement:
  Enforce rate limiting on POST /shorten using a Redis sliding window
  algorithm.  Clients that exceed the limit must receive HTTP 429
  Too Many Requests with a Retry-After header.

Constraints:
  - GET /{code} redirect endpoint MUST remain unmodified (performance-critical).
  - DELETE /{code} MUST remain unmodified.
  - The existing `rate_limit_per_minute` config field must be reused —
    do NOT add a duplicate setting.
  - Redis is already provisioned and used for caching.
  - No changes to the database schema.
  - All existing unit and integration tests must continue to pass.

Acceptance criteria:
  - AC-001: POST /shorten returns 429 when limit exceeded, with Retry-After header.
  - AC-002: Rate limit counter resets correctly after the window expires.
  - AC-003: GET /{code} latency unaffected (no additional middleware on that path).
  - AC-004: All existing tests in tests/unit/ and tests/integration/ pass.
  - AC-005: New unit tests cover rate-limit enforcement logic in isolation.
""".strip()


# ─── Shared helpers (same pattern as greenfield) ──────────────────────────────


def _artifact(
    name: str,
    artifact_type: ArtifactType,
    stage: str,
    content: dict[str, Any] | str,
) -> Artifact:
    body = json.dumps(content, indent=2) if isinstance(content, dict) else content
    return Artifact(name=name, artifact_type=artifact_type,
                    produced_by_stage=stage, content=body)


def _decision(
    title: str,
    description: str,
    rationale: str,
    decision_type: DecisionType,
    stage: str,
    parent_id: str | None = None,
    downstream_impacts: list[str] | None = None,
) -> Decision:
    return Decision(
        decision_type=decision_type,
        title=title,
        description=description,
        rationale=rationale,
        stage=stage,
        parent_decision_id=parent_id,
        downstream_impacts=downstream_impacts or [],
    )


def _task(title: str, description: str, stage: str, agent: str) -> Task:
    return Task(
        title=title,
        description=description,
        status=TaskStatus.COMPLETED,
        stage=stage,
        assigned_agent=agent,
        rationale=f"Required by {stage} analysis step",
    )


def _val(
    rule: str,
    passed: bool,
    message: str,
    stage: str,
    severity: ValidationSeverity = ValidationSeverity.ERROR,
    evidence: dict[str, Any] | None = None,
) -> ValidationResult:
    return ValidationResult(rule_name=rule, passed=passed, severity=severity,
                            message=message, stage=stage, evidence=evidence or {})


def _risk(title: str, description: str, sev: str, stage: str, mitigation: str = "") -> Risk:
    r = Risk(title=title, description=description,
             severity=RiskSeverity(sev.lower()), stage=stage)
    return r


# ─── Stage 1: Codebase Analysis ───────────────────────────────────────────────


class CodebaseAnalysisStage(BaseStage):
    """
    STAGE 1 — Repository & Codebase Analysis

    The agent reads the existing URL shortener codebase and produces a
    structured snapshot: file inventory, API surface, data flows, existing
    tests, and configuration.

    This is the FIRST action in any brownfield engagement.  No change is
    proposed yet — this stage is purely observational.

    Produces
    ────────
    Artifact : codebase_snapshot.json — complete codebase catalogue
    Decisions: "Existing codebase confirmed as FastAPI + PostgreSQL + Redis"
    """

    stage_name = "codebase_analysis"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name="codebase_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Enumerate all source files", "Walk url_shortener/ directory tree", s, "codebase-reader-agent"),
            _task("Catalogue API endpoints", "Extract all FastAPI route definitions from api/", s, "api-analyser-agent"),
            _task("Catalogue data flows", "Trace request path from HTTP → service → DB for each endpoint", s, "api-analyser-agent"),
            _task("Inventory existing tests", "Enumerate test files and their coverage areas", s, "test-reader-agent"),
            _task("Read configuration schema", "Extract all Settings fields from config.py", s, "config-reader-agent"),
            _task("Identify infrastructure dependencies", "Map Redis, PostgreSQL usage across the codebase", s, "infra-reader-agent"),
        ])

        snapshot = {
            "analysis_type": "brownfield_codebase_analysis",
            "codebase_root": "url_shortener/",
            "module_inventory": {
                "url_shortener/main.py": {
                    "purpose": "FastAPI app factory, lifespan events (DB pool + Redis pool setup)",
                    "key_symbols": ["app", "lifespan"],
                    "dependencies": ["config.py", "database.py", "api/urls.py"],
                },
                "url_shortener/config.py": {
                    "purpose": "Pydantic BaseSettings; all config from env vars",
                    "key_symbols": ["Settings", "get_settings"],
                    "notable_fields": {
                        "rate_limit_per_minute": {
                            "value": 100,
                            "type": "int",
                            "status": "DECLARED_BUT_NOT_ENFORCED",
                            "comment": "Field exists in config but is never read by any middleware or dependency",
                        },
                        "redis_url": {"value": "redis://localhost:6379/0", "status": "ACTIVE"},
                        "short_code_length": {"value": 8, "status": "ACTIVE"},
                    },
                },
                "url_shortener/database.py": {
                    "purpose": "SQLAlchemy async engine + session factory setup",
                    "key_symbols": ["async_engine", "session_factory"],
                    "dependencies": ["config.py"],
                },
                "url_shortener/api/urls.py": {
                    "purpose": "FastAPI router — all URL shortener HTTP endpoints",
                    "endpoints": [
                        {
                            "method": "POST", "path": "/shorten",
                            "handler": "shorten_url",
                            "auth": "none (currently open to all)",
                            "rate_limited": False,
                            "dependencies": ["get_db", "get_url_service"],
                        },
                        {
                            "method": "GET", "path": "/{code}",
                            "handler": "redirect_url",
                            "auth": "none (public read)",
                            "rate_limited": False,
                            "performance_critical": True,
                        },
                        {
                            "method": "DELETE", "path": "/{code}",
                            "handler": "delete_url",
                            "auth": "none (currently open)",
                            "rate_limited": False,
                        },
                    ],
                },
                "url_shortener/api/deps.py": {
                    "purpose": "FastAPI dependency factories (get_db, get_url_service)",
                    "key_symbols": ["get_db", "get_url_service"],
                    "current_dependencies": ["UrlRepository", "UrlService"],
                    "rate_limit_dep": "MISSING",
                },
                "url_shortener/api/exceptions.py": {
                    "purpose": "Exception handlers registered on the FastAPI app",
                    "key_symbols": ["register_exception_handlers"],
                },
                "url_shortener/models/url.py": {
                    "purpose": "SQLAlchemy ORM model for ShortUrl",
                    "key_symbols": ["ShortUrl", "is_resolvable"],
                    "schema": ["id", "code", "original_url", "is_active", "created_at", "expires_at"],
                },
                "url_shortener/repositories/url_repo.py": {
                    "purpose": "Data access layer for ShortUrl; all DB queries here",
                    "key_symbols": ["UrlRepository"],
                    "methods": ["get_by_code", "create", "deactivate", "code_exists"],
                },
                "url_shortener/services/url_service.py": {
                    "purpose": "Business logic (shorten, resolve, deactivate); no HTTP/SQLAlchemy",
                    "key_symbols": ["UrlService", "generate_short_code"],
                    "calls_redis": False,
                },
                "url_shortener/services/exceptions.py": {
                    "purpose": "Domain exceptions (ShortCodeNotFoundError, CodeGenerationError)",
                },
                "url_shortener/schemas/url.py": {
                    "purpose": "Pydantic request/response schemas (ShortenRequest, ShortenResponse)",
                },
            },
            "data_flows": {
                "POST /shorten": [
                    "HTTP POST → shorten_url() → get_db() → get_url_service()",
                    "→ UrlService.shorten() → UrlRepository.code_exists() [DB]",
                    "→ UrlRepository.create() [DB] → ShortenResponse [HTTP 201]",
                    "NOTE: No Redis interaction on write path currently",
                ],
                "GET /{code}": [
                    "HTTP GET → redirect_url() → get_db() → get_url_service()",
                    "→ UrlService.resolve() → UrlRepository.get_by_code() [DB]",
                    "→ RedirectResponse HTTP 302",
                    "NOTE: Redis cache NOT implemented yet (config has redis_url but no cache logic in code)",
                ],
                "DELETE /{code}": [
                    "HTTP DELETE → delete_url() → get_db() → get_url_service()",
                    "→ UrlService.deactivate() → UrlRepository.deactivate() [DB]",
                    "→ HTTP 204",
                ],
            },
            "test_inventory": {
                "tests/unit/test_url_service.py": {
                    "purpose": "Unit tests for UrlService (mock DB, no HTTP)",
                    "test_count": "~15 tests",
                    "covers": ["shorten", "resolve", "deactivate", "generate_short_code"],
                    "rate_limit_tests": 0,
                },
                "tests/unit/test_models.py": {
                    "purpose": "Unit tests for ShortUrl model properties",
                    "test_count": "~5 tests",
                },
                "tests/unit/test_schemas.py": {
                    "purpose": "Unit tests for Pydantic schema validation",
                    "test_count": "~5 tests",
                },
                "tests/integration/test_urls_api.py": {
                    "purpose": "Integration tests against the FastAPI app",
                    "test_count": "~8 tests",
                    "covers": ["POST /shorten", "GET /{code}", "DELETE /{code}"],
                    "rate_limit_tests": 0,
                },
            },
            "infrastructure": {
                "redis": {
                    "configured": True,
                    "url_field": "Settings.redis_url",
                    "active_usage": "Not in current production code (redis_url configured but aioredis not imported anywhere)",
                    "planned_usage": "rate_limit_per_minute field suggests rate limiting was planned",
                },
                "postgresql": {
                    "configured": True,
                    "orm": "SQLAlchemy 2 async",
                    "active_usage": True,
                },
            },
            "gap_identified": {
                "field": "Settings.rate_limit_per_minute",
                "status": "DECLARED but NOT ENFORCED",
                "evidence": "Grep across codebase: rate_limit_per_minute appears only in config.py — never imported or used elsewhere",
            },
        }

        dec = _decision(
            title="Existing codebase confirmed: FastAPI + PostgreSQL + Redis-ready",
            description="URL shortener codebase catalogued. Redis infrastructure present but rate limiting not implemented.",
            rationale=(
                "config.py declares rate_limit_per_minute=100. "
                "No import of this field exists in api/, services/, or repositories/. "
                "Redis is configured (redis_url field) but aioredis is not imported anywhere. "
                "This is a clean enhancement opportunity: wire up existing config to real enforcement."
            ),
            decision_type=DecisionType.SCOPE,
            stage=s,
            downstream_impacts=["impact_analysis", "change_planning"],
        )
        ctx.decisions.append(dec)

        art = _artifact("codebase_snapshot.json", ArtifactType.DOCUMENTATION, s, snapshot)
        ctx.artifacts.append(art)

        ctx.output_data["codebase_snapshot"] = snapshot
        ctx.output_data["codebase_artifact_id"] = art.id
        ctx.output_data["gap_decision_id"] = dec.id
        ctx.output_data["endpoints_discovered"] = 3
        ctx.output_data["test_files_discovered"] = 4
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        if "gap_identified" not in ctx.output_data.get("codebase_snapshot", {}):
            return GateResult(gate_name="codebase_exit", passed=False,
                              reason="Gap analysis not completed")
        ctx.validations.extend([
            _val("codebase_fully_catalogued",
                 True,
                 f"Codebase catalogued: {len(ctx.output_data['codebase_snapshot']['module_inventory'])} "
                 "modules, 3 API endpoints, 4 test files",
                 s),
            _val("dormant_config_identified",
                 True,
                 "Identified dormant rate_limit_per_minute field in config.py — exists but never enforced",
                 s),
        ])
        return GateResult(gate_name="codebase_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 2: Impact Analysis ─────────────────────────────────────────────────


class ImpactAnalysisStage(BaseStage):
    """
    STAGE 2 — Impacted Modules, APIs, Data Flows & Tests

    Given the codebase snapshot, the agent maps the proposed rate-limiting
    enhancement to precisely which components are affected vs preserved.

    KEY PRINCIPLE: the impact map is as important for what it EXCLUDES as
    for what it includes.  'GET /{code} is NOT impacted' is an explicit,
    auditable decision — not an accidental omission.

    Produces
    ────────
    Artifact : impact_map.json — surgical change scope
    Decisions: scope boundary decision (what is out of scope)
    """

    stage_name = "impact_analysis"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "codebase_snapshot" not in ctx.input_data:
            return GateResult(gate_name="impact_entry", passed=False,
                              reason="codebase_snapshot missing — codebase_analysis must run first")
        return GateResult(gate_name="impact_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name
        gap_dec_id = ctx.input_data.get("gap_decision_id")

        ctx.tasks.extend([
            _task("Map enhancement to module impact", "For each impacted module: list exact lines/symbols affected", s, "impact-analyser-agent"),
            _task("Identify preserved modules", "Explicitly list modules that must NOT change", s, "impact-analyser-agent"),
            _task("Trace data flow changes", "Show before/after data flow for POST /shorten", s, "impact-analyser-agent"),
            _task("Identify existing tests at risk", "List tests that exercise impacted code paths", s, "impact-analyser-agent"),
            _task("Identify unaffected test coverage", "Confirm GET /{code} tests are safe from regression", s, "impact-analyser-agent"),
        ])

        impact_map = {
            "enhancement": "Enforce rate limiting on POST /shorten via Redis sliding window",
            "impacted_components": {
                "url_shortener/config.py": {
                    "impact_type": "ADDITION",
                    "change": "Add rate_limit_window_seconds: int = 60 field (sliding window duration)",
                    "existing_field_reused": "rate_limit_per_minute (existing, value=100)",
                    "lines_changed": "~2 lines added",
                    "risk": "LOW — additive only, no existing field removed or renamed",
                },
                "url_shortener/api/rate_limit.py": {
                    "impact_type": "NEW FILE",
                    "change": "Create Redis sliding window rate limiter as a FastAPI dependency",
                    "implementation": "ZADD + ZRANGEBYSCORE + ZCARD + EXPIRE in Redis",
                    "lines_changed": "~50 new lines",
                    "risk": "LOW — new file, isolated implementation",
                },
                "url_shortener/api/deps.py": {
                    "impact_type": "ADDITION",
                    "change": "Add check_rate_limit() factory function using rate_limit.py",
                    "lines_changed": "~15 lines added",
                    "risk": "LOW — additive, existing deps unchanged",
                },
                "url_shortener/api/urls.py": {
                    "impact_type": "MODIFICATION",
                    "affected_endpoints": {
                        "POST /shorten": {
                            "change": "Add Depends(check_rate_limit) to shorten_url() signature",
                            "lines_changed": "1-3 lines",
                            "breaking_change": False,
                        },
                    },
                    "unaffected_endpoints": {
                        "GET /{code}": "EXPLICITLY PRESERVED — no Depends() added",
                        "DELETE /{code}": "EXPLICITLY PRESERVED — no Depends() added",
                    },
                },
                "url_shortener/api/exceptions.py": {
                    "impact_type": "ADDITION",
                    "change": "Register 429 TooManyRequests handler with Retry-After header",
                    "lines_changed": "~10 lines added",
                    "risk": "LOW — new handler, does not touch existing handlers",
                },
            },
            "explicitly_preserved": {
                "url_shortener/services/url_service.py": {
                    "reason": "Rate limiting is a HTTP-layer concern; service layer must NOT know about it",
                    "action": "DO NOT MODIFY",
                },
                "url_shortener/models/url.py": {
                    "reason": "No schema change; rate limiting has no DB representation",
                    "action": "DO NOT MODIFY",
                },
                "url_shortener/repositories/url_repo.py": {
                    "reason": "Rate limiting does not require DB queries",
                    "action": "DO NOT MODIFY",
                },
                "url_shortener/schemas/url.py": {
                    "reason": "ShortenRequest and ShortenResponse are unchanged",
                    "action": "DO NOT MODIFY",
                },
                "url_shortener/database.py": {
                    "reason": "No DB schema changes",
                    "action": "DO NOT MODIFY",
                },
                "url_shortener/services/exceptions.py": {
                    "reason": "RateLimitExceededError goes in api/rate_limit.py, not domain exceptions",
                    "action": "DO NOT MODIFY",
                },
            },
            "api_impact": {
                "POST /shorten": {
                    "status": "MODIFIED — rate limit enforcement added",
                    "new_response_codes": [429],
                    "new_headers_on_429": ["Retry-After"],
                    "existing_responses_unchanged": [201, 422, 503],
                    "breaking_change": False,
                },
                "GET /{code}": {
                    "status": "PRESERVED — explicitly no change",
                    "rationale": "Performance-critical path; any middleware addition could affect p99 latency",
                },
                "DELETE /{code}": {
                    "status": "PRESERVED — explicitly no change",
                    "rationale": "Low-volume admin operation; rate limiting not required",
                },
            },
            "data_flow_delta": {
                "before": [
                    "HTTP POST /shorten → shorten_url() → DB",
                ],
                "after": [
                    "HTTP POST /shorten → check_rate_limit() [Redis SLIDING WINDOW]",
                    "  → if limit exceeded: return 429 (Retry-After header set)",
                    "  → if allowed: shorten_url() → DB → 201",
                ],
                "unchanged_flows": [
                    "GET /{code}: HTTP GET → redirect_url() → DB → 302  [NO CHANGE]",
                    "DELETE /{code}: HTTP DELETE → delete_url() → DB → 204  [NO CHANGE]",
                ],
            },
            "existing_tests_at_risk": {
                "tests/integration/test_urls_api.py::test_shorten_url": {
                    "risk": "LOW — still passes as long as rate limit not triggered in test",
                    "mitigation": "Reset Redis rate limit key before each test",
                },
            },
            "safe_existing_tests": [
                "tests/unit/test_url_service.py — no HTTP layer, unaffected",
                "tests/unit/test_models.py — no HTTP layer, unaffected",
                "tests/unit/test_schemas.py — no HTTP layer, unaffected",
                "tests/integration/test_urls_api.py::test_redirect_url — GET /{code} unaffected",
                "tests/integration/test_urls_api.py::test_delete_url — DELETE /{code} unaffected",
            ],
        }

        scope_dec = _decision(
            title="Rate limiting scope: POST /shorten ONLY",
            description="Explicit scope boundary: 3 files modified, 1 new file, 6 files preserved",
            rationale=(
                "Rate limiting belongs at the HTTP layer (FastAPI dependency), not the service layer. "
                "GET /{code} is explicitly excluded: it is performance-critical and rate limiting "
                "anonymous redirects would break legitimate browser/bot traffic. "
                "DELETE /{code} is excluded: low volume, admin usage, no abuse pattern observed. "
                "Service, model, and repository layers are untouched — rate limiting is orthogonal to business logic."
            ),
            decision_type=DecisionType.SCOPE,
            stage=s,
            parent_id=gap_dec_id,
            downstream_impacts=["change_planning", "regression_test_planning"],
        )
        ctx.decisions.append(scope_dec)

        art = _artifact("impact_map.json", ArtifactType.DOCUMENTATION, s, impact_map)
        ctx.artifacts.append(art)

        ctx.output_data["impact_map"] = impact_map
        ctx.output_data["impact_map_artifact_id"] = art.id
        ctx.output_data["scope_decision_id"] = scope_dec.id
        ctx.output_data["impacted_files"] = list(impact_map["impacted_components"].keys())
        ctx.output_data["preserved_files"] = list(impact_map["explicitly_preserved"].keys())
        ctx.output_data["api_responses_added"] = [429]
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        im = ctx.output_data.get("impact_map", {})
        if not im.get("explicitly_preserved"):
            return GateResult(gate_name="impact_exit", passed=False,
                              reason="Impact map must explicitly list preserved components")
        n_impacted = len(ctx.output_data.get("impacted_files", []))
        n_preserved = len(ctx.output_data.get("preserved_files", []))
        ctx.validations.extend([
            _val("impact_scope_defined",
                 True,
                 f"Impact map complete: {n_impacted} files impacted, {n_preserved} files explicitly preserved",
                 s),
            _val("get_redirect_preserved",
                 True,
                 "GET /{code} redirect endpoint explicitly excluded from impact scope",
                 s,
                 evidence={"preserved_reason": "performance-critical path"}),
            _val("service_layer_preserved",
                 True,
                 "url_service.py, url_repo.py, url model — all unchanged (rate limiting is HTTP-layer only)",
                 s),
        ])
        return GateResult(gate_name="impact_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 3: Risk Assessment ─────────────────────────────────────────────────


class RiskAssessmentStage(BaseStage):
    """
    STAGE 3 — Risk Assessment

    Evaluates the risks introduced by the rate-limiting change, including
    latency impact, Redis failure modes, and regression risks.

    Each risk has a mitigation plan — the change is not approved until
    all HIGH risks have mitigations.

    Produces
    ────────
    Artifact : risk_assessment.json
    """

    stage_name = "risk_assessment"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "impact_map" not in ctx.input_data:
            return GateResult(gate_name="risk_entry", passed=False,
                              reason="impact_map missing — impact_analysis must run first")
        return GateResult(gate_name="risk_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Analyse Redis failure modes", "What happens if Redis is unavailable during rate check?", s, "risk-analyst-agent"),
            _task("Analyse latency impact", "Estimate added latency from Redis round-trip on POST /shorten", s, "risk-analyst-agent"),
            _task("Analyse regression risk", "Review existing tests for breakage risk", s, "risk-analyst-agent"),
            _task("Analyse distributed correctness", "Is sliding window correct under concurrent requests?", s, "risk-analyst-agent"),
            _task("Propose mitigations", "Document mitigation plan for each identified risk", s, "risk-analyst-agent"),
        ])

        ctx.risks.extend([
            Risk(
                title="Redis unavailability blocks POST /shorten",
                description=(
                    "If the Redis instance is down, the rate-limit dependency "
                    "raises a connection error, causing POST /shorten to return 503. "
                    "Fail-closed (safe but disruptive) vs fail-open (permissive but safe)."
                ),
                severity=RiskSeverity.HIGH,
                stage=s,
            ),
            Risk(
                title="Additional Redis round-trip adds latency to POST /shorten",
                description=(
                    "Each call to check_rate_limit() adds ~0.5-2ms (Redis on LAN). "
                    "POST /shorten has a 200ms SLA; Redis call is within budget. "
                    "Under Redis overload, tail latency could spike."
                ),
                severity=RiskSeverity.MEDIUM,
                stage=s,
            ),
            Risk(
                title="Integration tests may trigger rate limit if not reset",
                description=(
                    "If integration tests call POST /shorten repeatedly in the same second, "
                    "the rate limit may be triggered mid-suite, causing spurious 429 failures."
                ),
                severity=RiskSeverity.LOW,
                stage=s,
            ),
            Risk(
                title="Redis key accumulation without TTL",
                description=(
                    "Sliding window uses ZADD; if EXPIRE is not set on the key, "
                    "rate-limit entries accumulate forever, slowly growing Redis memory usage."
                ),
                severity=RiskSeverity.MEDIUM,
                stage=s,
            ),
        ])

        assessment = {
            "risks": [
                {
                    "id": "RISK-001",
                    "title": "Redis unavailability blocks POST /shorten",
                    "severity": "HIGH",
                    "category": "infrastructure",
                    "probability": "low (Redis is HA in production)",
                    "impact": "high (POST /shorten returns 503 for all users)",
                    "mitigation": {
                        "strategy": "fail-open: if Redis raises ConnectionError, skip rate check and allow request",
                        "implementation": "try/except around Redis call; log warning on miss",
                        "rationale": "Availability > strict rate limiting; a brief open window during Redis outage is acceptable",
                        "test": "Unit test: mock Redis to raise ConnectionError → POST /shorten should return 201 (not 503)",
                    },
                },
                {
                    "id": "RISK-002",
                    "title": "Redis round-trip adds latency to POST /shorten",
                    "severity": "MEDIUM",
                    "category": "performance",
                    "probability": "certain (every request has the overhead)",
                    "impact": "medium (1-3ms added; 200ms SLA has budget)",
                    "mitigation": {
                        "strategy": "Use Redis pipeline to batch ZADD + ZRANGEBYSCORE + EXPIRE in 1 round-trip",
                        "implementation": "async with redis_client.pipeline() as pipe",
                        "test": "Load test POST /shorten after change; assert p99 < 200ms",
                    },
                },
                {
                    "id": "RISK-003",
                    "title": "Integration tests trigger rate limit",
                    "severity": "LOW",
                    "category": "test_regression",
                    "probability": "high without mitigation",
                    "impact": "low (CI failure only, not production issue)",
                    "mitigation": {
                        "strategy": "Add conftest.py fixture to flush rate-limit Redis key before each test",
                        "implementation": "await redis_client.delete(f'rate_limit:{client_ip}')",
                    },
                },
                {
                    "id": "RISK-004",
                    "title": "Redis key accumulation without TTL",
                    "severity": "MEDIUM",
                    "category": "operational",
                    "probability": "certain if not addressed",
                    "impact": "low (slow memory leak; Redis has eviction policy)",
                    "mitigation": {
                        "strategy": "Set EXPIRE on the rate-limit key to rate_limit_window_seconds * 2",
                        "implementation": "pipe.expire(key, window_seconds * 2)",
                    },
                },
            ],
            "overall_risk_rating": "LOW-MEDIUM",
            "blocking_risks": [],
            "requires_approval": True,
            "rationale": "No blocking risks. All HIGH risk mitigated. Change may proceed with implementation plan.",
        }

        risk_dec = _decision(
            title="All risks mitigated; change approved for planning",
            description="Risk assessment complete: no blocking risks, all HIGH risks have mitigation plans",
            rationale=(
                "RISK-001 (Redis unavailability): fail-open strategy prevents service disruption. "
                "RISK-002 (latency): Redis pipeline batches commands into single round-trip. "
                "RISK-003 (test regression): conftest.py fixture cleans up between tests. "
                "RISK-004 (key TTL): EXPIRE set at 2x window to auto-clean entries."
            ),
            decision_type=DecisionType.TRADE_OFF,
            stage=s,
            downstream_impacts=["change_planning"],
        )
        ctx.decisions.append(risk_dec)

        art = _artifact("risk_assessment.json", ArtifactType.REPORT, s, assessment)
        ctx.artifacts.append(art)

        ctx.output_data["risk_assessment"] = assessment
        ctx.output_data["risk_artifact_id"] = art.id
        ctx.output_data["risk_decision_id"] = risk_dec.id
        ctx.output_data["overall_risk_rating"] = assessment["overall_risk_rating"]
        ctx.output_data["blocking_risks"] = 0
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        if ctx.output_data.get("blocking_risks", 1) > 0:
            return GateResult(gate_name="risk_exit", passed=False,
                              reason=f"Blocking risks: {ctx.output_data['blocking_risks']}")
        ctx.validations.append(
            _val("no_blocking_risks",
                 True,
                 f"Risk assessment complete. Overall rating: {ctx.output_data['overall_risk_rating']}. "
                 "0 blocking risks. 4 risks identified with mitigation plans.",
                 s,
                 evidence={"risk_count": len(ctx.risks), "blocking": 0}))
        return GateResult(gate_name="risk_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 4: Change Planning ─────────────────────────────────────────────────


class ChangePlanningStage(BaseStage):
    """
    STAGE 4 — Proposed Change (Task Decomposition + Dependency Graph)

    Produces the precise, file-level change plan with exact additions to each
    affected file and the complete dependency graph of the change tasks.

    This stage requires human approval before regression testing begins —
    a reviewer must confirm the scope is correct and mitigations are acceptable.

    Produces
    ────────
    Artifact : change_plan.json — precise diff-level specification
    Decisions: implementation strategy (Redis pipeline, fail-open)
    """

    stage_name = "change_planning"
    requires_approval = True
    action_impact = ActionImpact.SIGNIFICANT
    policy_metadata = {"change_ticket_id": "CHG-2026-BF-001"}  # type: ignore[assignment]

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "risk_assessment" not in ctx.input_data:
            return GateResult(gate_name="change_planning_entry", passed=False,
                              reason="risk_assessment missing — risk_assessment stage must complete first")
        if ctx.input_data.get("blocking_risks", 1) > 0:
            return GateResult(gate_name="change_planning_entry", passed=False,
                              reason="Cannot plan change while blocking risks remain unmitigated")
        return GateResult(gate_name="change_planning_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Design Redis sliding window implementation", "Write check_rate_limit() dep using ZADD+ZCARD+EXPIRE pipeline", s, "senior-engineer-agent"),
            _task("Define 429 response schema", "Specify Retry-After header calculation and response body", s, "senior-engineer-agent"),
            _task("Plan config addition", "Add rate_limit_window_seconds to Settings (non-breaking)", s, "senior-engineer-agent"),
            _task("Plan dependency wiring", "Add check_rate_limit dep to POST /shorten only", s, "senior-engineer-agent"),
            _task("Design task dependency graph", "Order implementation tasks by dependency", s, "tech-lead-agent"),
        ])

        impl_strategy_dec = _decision(
            title="Redis pipeline + fail-open for sliding window rate limiting",
            description=(
                "Use Redis sorted-set sliding window (ZADD/ZRANGEBYSCORE/ZCARD) "
                "with pipelining. Fail-open on Redis errors."
            ),
            rationale=(
                "Sliding window provides smooth rate limiting (no burst at window boundary) "
                "versus fixed window counters. Redis pipeline batches 3 commands into 1 round-trip. "
                "Fail-open chosen over fail-closed because availability > strict rate limiting; "
                "a brief window during Redis outage is preferable to blocking all URL creation."
            ),
            decision_type=DecisionType.IMPLEMENTATION,
            stage=s,
            downstream_impacts=["regression_test_planning"],
        )
        ctx.decisions.append(impl_strategy_dec)

        change_plan = {
            "change_ticket": "CHG-2026-BF-001",
            "enhancement": "Rate limiting on POST /shorten — Redis sliding window",
            "implementation_tasks": [
                {
                    "id": "BF-IMPL-001",
                    "title": "Add rate_limit_window_seconds to config.py",
                    "file": "url_shortener/config.py",
                    "change_type": "ADDITION",
                    "details": "Add `rate_limit_window_seconds: int = Field(default=60, description='Sliding window duration in seconds')`",
                    "estimated_lines": 2,
                    "depends_on": [],
                    "breaking_change": False,
                },
                {
                    "id": "BF-IMPL-002",
                    "title": "Create url_shortener/api/rate_limit.py",
                    "file": "url_shortener/api/rate_limit.py",
                    "change_type": "NEW FILE",
                    "details": """
async def check_rate_limit(request: Request, redis = Depends(get_redis)) -> None:
    settings = request.app.state.settings
    ip = request.client.host or "unknown"
    key = f"rate_limit:{ip}"
    now_ms = int(time.time() * 1000)
    window_ms = settings.rate_limit_window_seconds * 1000
    limit = settings.rate_limit_per_minute

    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(key, 0, now_ms - window_ms)
            pipe.zadd(key, {str(now_ms): now_ms})
            pipe.zcard(key)
            pipe.expire(key, settings.rate_limit_window_seconds * 2)
            results = await pipe.execute()
        request_count = results[2]
        if request_count > limit:
            retry_after = settings.rate_limit_window_seconds
            raise RateLimitExceededError(retry_after=retry_after)
    except ConnectionError:
        # fail-open: Redis unavailable → allow request (log warning)
        logger.warning("rate_limit_redis_unavailable", ip=ip)
""",
                    "estimated_lines": 50,
                    "depends_on": ["BF-IMPL-001"],
                    "breaking_change": False,
                },
                {
                    "id": "BF-IMPL-003",
                    "title": "Add get_redis() dep and check_rate_limit() factory to deps.py",
                    "file": "url_shortener/api/deps.py",
                    "change_type": "ADDITION",
                    "details": "Add `get_redis()` dependency returning `request.app.state.redis_client` and `check_rate_limit = Depends(rate_limit.check_rate_limit)`",
                    "estimated_lines": 15,
                    "depends_on": ["BF-IMPL-002"],
                    "breaking_change": False,
                },
                {
                    "id": "BF-IMPL-004",
                    "title": "Wire check_rate_limit to POST /shorten in urls.py",
                    "file": "url_shortener/api/urls.py",
                    "change_type": "MODIFICATION",
                    "details": "Add `_: None = Depends(check_rate_limit)` to shorten_url() signature only. GET /{code} and DELETE /{code} signatures unchanged.",
                    "estimated_lines": 3,
                    "depends_on": ["BF-IMPL-003"],
                    "breaking_change": False,
                },
                {
                    "id": "BF-IMPL-005",
                    "title": "Add 429 exception handler to exceptions.py",
                    "file": "url_shortener/api/exceptions.py",
                    "change_type": "ADDITION",
                    "details": "Register RateLimitExceededError handler returning HTTP 429 with Retry-After header",
                    "estimated_lines": 10,
                    "depends_on": ["BF-IMPL-002"],
                    "breaking_change": False,
                },
                {
                    "id": "BF-IMPL-006",
                    "title": "Wire Redis client into app lifespan in main.py",
                    "file": "url_shortener/main.py",
                    "change_type": "MODIFICATION",
                    "details": "Add redis client startup/shutdown to lifespan context manager: `app.state.redis_client = await aioredis.from_url(settings.redis_url)`",
                    "estimated_lines": 5,
                    "depends_on": ["BF-IMPL-001"],
                    "breaking_change": False,
                },
            ],
            "do_not_modify": [
                "url_shortener/services/url_service.py",
                "url_shortener/models/url.py",
                "url_shortener/repositories/url_repo.py",
                "url_shortener/schemas/url.py",
                "url_shortener/database.py",
                "url_shortener/services/exceptions.py",
            ],
            "dependency_graph": {
                "BF-IMPL-001": [],
                "BF-IMPL-002": ["BF-IMPL-001"],
                "BF-IMPL-003": ["BF-IMPL-002"],
                "BF-IMPL-004": ["BF-IMPL-003"],
                "BF-IMPL-005": ["BF-IMPL-002"],
                "BF-IMPL-006": ["BF-IMPL-001"],
                "execution_order": "BF-IMPL-001 → BF-IMPL-002, BF-IMPL-006 → BF-IMPL-003, BF-IMPL-005 → BF-IMPL-004",
            },
            "total_files_modified": 4,
            "total_new_files": 1,
            "total_files_unchanged": 6,
            "rollback_plan": {
                "strategy": "Git revert of the change branch",
                "steps": [
                    "git revert <commit-sha>",
                    "Flush Redis rate-limit keys: redis-cli KEYS 'rate_limit:*' | xargs redis-cli DEL",
                ],
                "rto_estimate": "< 5 minutes",
                "data_loss": "None (Redis keys are ephemeral; no DB changes)",
            },
        }
        art = _artifact("change_plan.json", ArtifactType.DOCUMENTATION, s, change_plan)
        ctx.artifacts.append(art)

        ctx.output_data["change_plan"] = change_plan
        ctx.output_data["change_plan_artifact_id"] = art.id
        ctx.output_data["impl_task_count"] = len(change_plan["implementation_tasks"])
        ctx.output_data["preserved_file_count"] = len(change_plan["do_not_modify"])
        ctx.output_data["change_ready"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        cp = ctx.output_data.get("change_plan", {})
        if not cp.get("do_not_modify"):
            return GateResult(gate_name="change_exit", passed=False,
                              reason="Change plan must explicitly list do_not_modify files")
        if not cp.get("rollback_plan"):
            return GateResult(gate_name="change_exit", passed=False,
                              reason="Rollback plan required in change_plan")
        ctx.validations.extend([
            _val("change_plan_complete",
                 True,
                 f"Change plan: {ctx.output_data['impl_task_count']} impl tasks, "
                 f"{ctx.output_data['preserved_file_count']} files explicitly preserved, "
                 "rollback plan documented",
                 s),
            _val("get_redirect_not_in_plan",
                 True,
                 "GET /{code} does NOT appear in change_plan.implementation_tasks (confirmed preserved)",
                 s),
            _val("service_layer_not_in_plan",
                 True,
                 "url_service.py, url_repo.py in do_not_modify list",
                 s),
        ])
        return GateResult(gate_name="change_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 5: Regression Test Planning ───────────────────────────────────────


class RegressionTestPlanningStage(BaseStage):
    """
    STAGE 5 — Regression & New Test Planning

    Plans the full test suite update: new tests for rate limiting and
    confirmation that all existing tests remain valid.

    Produces
    ────────
    Artifact : regression_test_plan.json
    """

    stage_name = "regression_test_planning"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "change_plan" not in ctx.input_data:
            return GateResult(gate_name="regression_entry", passed=False,
                              reason="change_plan missing — change_planning must complete first")
        return GateResult(gate_name="regression_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Plan unit tests for rate_limit.py", "Test sliding window logic with mock Redis", s, "qa-agent"),
            _task("Plan integration tests for rate limiting", "Test POST /shorten 429 response with real Redis fixture", s, "qa-agent"),
            _task("Plan regression tests for preserved endpoints", "Confirm GET /{code} and DELETE /{code} unaffected", s, "qa-agent"),
            _task("Plan Redis failure test", "Verify fail-open behaviour when Redis is unavailable", s, "qa-agent"),
            _task("Plan conftest.py fixture", "Flush rate-limit keys before each test", s, "qa-agent"),
        ])

        test_plan = {
            "change_ticket": "CHG-2026-BF-001",
            "new_tests": {
                "tests/unit/test_rate_limit.py": {
                    "purpose": "Unit tests for check_rate_limit() dependency (mock Redis)",
                    "test_cases": [
                        "test_allows_request_under_limit",
                        "test_blocks_request_over_limit",
                        "test_window_resets_after_expiry",
                        "test_fail_open_on_redis_connection_error",
                        "test_retry_after_header_value_matches_window",
                        "test_different_ips_have_independent_limits",
                        "test_limit_not_applied_to_get_redirect",
                    ],
                    "coverage_target": "90%+ on rate_limit.py",
                },
                "tests/integration/test_rate_limit_api.py": {
                    "purpose": "Integration tests against FastAPI app with Redis",
                    "test_cases": [
                        "test_post_shorten_returns_429_when_limit_exceeded",
                        "test_post_shorten_429_includes_retry_after_header",
                        "test_post_shorten_succeeds_after_window_reset",
                        "test_get_redirect_never_returns_429",
                        "test_delete_url_never_returns_429",
                    ],
                    "requires": "redis-test fixture (flush before each test)",
                },
            },
            "existing_tests_status": {
                "tests/unit/test_url_service.py": {
                    "status": "SAFE — no HTTP layer involved, unaffected by middleware change",
                    "action": "Run unchanged; verify all still pass",
                },
                "tests/unit/test_models.py": {
                    "status": "SAFE — model unchanged",
                    "action": "Run unchanged",
                },
                "tests/unit/test_schemas.py": {
                    "status": "SAFE — schemas unchanged",
                    "action": "Run unchanged",
                },
                "tests/integration/test_urls_api.py": {
                    "status": "NEEDS FIXTURE — must flush rate-limit key before each test to avoid spurious 429",
                    "action": "Add conftest.py fixture; existing test assertions unchanged",
                    "test_cases": {
                        "test_shorten_url": "Add rate-limit reset fixture; assertion unchanged",
                        "test_redirect_url": "Unchanged — GET /{code} not rate-limited",
                        "test_delete_url": "Unchanged — DELETE /{code} not rate-limited",
                    },
                },
            },
            "conftest_additions": {
                "tests/integration/conftest.py": {
                    "new_fixture": "flush_rate_limit_keys",
                    "scope": "function",
                    "implementation": "await redis_client.delete('rate_limit:testclient')",
                    "applied_to": "all integration tests in tests/integration/",
                },
            },
            "coverage_goals": {
                "rate_limit.py": "≥90% line coverage",
                "overall_regression": "All 28+ existing tests must pass",
            },
        }

        art = _artifact("regression_test_plan.json", ArtifactType.TEST, s, test_plan)
        ctx.artifacts.append(art)

        ctx.output_data["regression_test_plan"] = test_plan
        ctx.output_data["regression_artifact_id"] = art.id
        ctx.output_data["new_test_count"] = sum(
            len(t["test_cases"]) for t in test_plan["new_tests"].values()
        )
        ctx.output_data["existing_tests_verified"] = True
        ctx.output_data["regression_ready"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        if not ctx.output_data.get("regression_ready"):
            return GateResult(gate_name="regression_exit", passed=False,
                              reason="regression_ready not set")
        ctx.validations.extend([
            _val("regression_plan_complete",
                 True,
                 f"Regression test plan: {ctx.output_data['new_test_count']} new test cases, "
                 "all existing tests confirmed safe or annotated with fixture requirement",
                 s),
            _val("existing_tests_preserved",
                 True,
                 "All 28+ existing tests planned to pass; no test removed or skipped",
                 s),
        ])
        return GateResult(gate_name="regression_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 6: Validation ─────────────────────────────────────────────────────


class BrownfieldValidationStage(BaseStage):
    """
    STAGE 6 — Change Validation & Final Approval

    Validates the full brownfield change plan:
    - All impacts from the impact_map are addressed in the change_plan.
    - No preserved files appear in the change_plan.
    - Regression plan covers all impacted code paths.
    - Change is self-contained and reversible.

    Requires human approval before the change plan is marked ready for implementation.

    Produces
    ────────
    Artifact : brownfield_validation_report.json
    """

    stage_name = "validation"
    requires_approval = True
    action_impact = ActionImpact.HIGH_IMPACT
    high_impact_action_type = HighImpactActionType.PRODUCTION_RELEASE
    policy_metadata = {  # type: ignore[assignment]
        "security_scan_passed": True,
        "change_ticket_id": "CHG-2026-BF-001",
        "rollback_plan_documented": True,
    }

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        required = ["regression_ready"]
        missing = [k for k in required if not ctx.input_data.get(k)]
        if missing:
            return GateResult(gate_name="validation_entry", passed=False,
                              reason=f"Missing upstream signals: {missing}")
        return GateResult(gate_name="validation_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Verify impact coverage", "All impacted modules in impact_map have tasks in change_plan", s, "validation-agent"),
            _task("Verify preservation adherence", "No preserved module appears in change_plan.implementation_tasks", s, "validation-agent"),
            _task("Verify rollback completeness", "Rollback plan covers all modified files", s, "validation-agent"),
            _task("Verify regression coverage", "New tests cover every new behaviour; existing tests unaffected", s, "validation-agent"),
            _task("Security review", "Check new Redis operations for injection risk; no secrets in rate-limit key", s, "security-agent"),
        ])

        validations = [
            _val("all_impacts_addressed",
                 True,
                 "All 4 impacted files from impact_map have corresponding tasks in change_plan",
                 s),
            _val("preserved_files_untouched",
                 True,
                 "url_service.py, url_repo.py, models/url.py, schemas/url.py — none appear in change_plan.implementation_tasks",
                 s),
            _val("get_redirect_unmodified",
                 True,
                 "GET /{code} endpoint verified absent from change plan; performance-critical path protected",
                 s,
                 evidence={"endpoint": "GET /{code}", "modified": False}),
            _val("rollback_documented",
                 True,
                 "Rollback plan: git revert + Redis key flush; RTO < 5 minutes; zero data loss",
                 s),
            _val("regression_coverage_adequate",
                 True,
                 "12 new test cases added; all 28+ existing tests preserved",
                 s,
                 evidence={"new_tests": 12, "existing_tests_status": "all pass"}),
            _val("security_review_passed",
                 True,
                 "Redis key uses IP address (not user input directly); no injection risk. "
                 "No secrets stored in rate-limit Redis key.",
                 s,
                 evidence={"key_pattern": "rate_limit:{client_ip}", "injection_risk": "none"}),
            _val("no_breaking_api_changes",
                 True,
                 "POST /shorten: 201/422/503 responses unchanged; 429 added (additive, not breaking). "
                 "GET /{code}: completely unchanged. DELETE /{code}: completely unchanged.",
                 s),
        ]
        ctx.validations.extend(validations)

        final_decision = _decision(
            title="Brownfield change approved — rate limiting ready for implementation",
            description=(
                "All 7 validation checks passed. Change plan is surgical: "
                "4 files modified, 1 new file, 6 files preserved. "
                "0 breaking API changes. Rollback < 5 minutes."
            ),
            rationale=(
                "The change correctly enforces the dormant rate_limit_per_minute config field. "
                "Impact scope is minimal and precisely documented. "
                "GET /{code} redirect path is untouched. "
                "All risks have mitigations. Rollback is trivial. "
                "Human approval obtained at change_planning and validation stages."
            ),
            decision_type=DecisionType.SCOPE,
            stage=s,
        )
        ctx.decisions.append(final_decision)

        report = {
            "change_ticket": "CHG-2026-BF-001",
            "validation_checks": len(validations),
            "passed": sum(1 for v in validations if v.passed),
            "failed": sum(1 for v in validations if not v.passed),
            "critical_failures": 0,
            "final_decision_id": final_decision.id,
            "change_summary": {
                "enhancement": "Rate limiting enforcement on POST /shorten",
                "files_modified": 4,
                "files_created": 1,
                "files_preserved": 6,
                "breaking_changes": 0,
                "new_response_codes": [429],
                "existing_test_impact": "All pass with conftest.py fixture addition",
                "new_tests": 12,
            },
            "implementation_ready": True,
            "reviewer_notes": "Change is minimal, reversible, and well-tested. Approved for implementation.",
        }
        art = _artifact("brownfield_validation_report.json", ArtifactType.REPORT, s, report)
        ctx.artifacts.append(art)

        ctx.output_data["validation_report"] = report
        ctx.output_data["validation_artifact_id"] = art.id
        ctx.output_data["implementation_ready"] = True
        ctx.output_data["critical_failures"] = 0
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        if ctx.output_data.get("critical_failures", 1) > 0:
            return GateResult(gate_name="validation_exit", passed=False,
                              reason=f"Critical failures: {ctx.output_data['critical_failures']}")
        if not ctx.output_data.get("implementation_ready"):
            return GateResult(gate_name="validation_exit", passed=False,
                              reason="implementation_ready not confirmed")
        ctx.validations.append(
            _val("final_sign_off",
                 True,
                 "Brownfield change plan validated and approved. "
                 "Rate limiting enhancement ready for implementation.",
                 s))
        return GateResult(gate_name="validation_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Workflow factory functions ───────────────────────────────────────────────


def create_brownfield_requirement(
    raw_text: str = DEFAULT_REQUIREMENT_TEXT,
) -> Requirement:
    """Build the Requirement for the brownfield rate-limiting enhancement."""
    return Requirement(
        title="Enforce Rate Limiting on POST /shorten — Brownfield Enhancement",
        raw_text=raw_text,
        requirement_type=RequirementType.BROWNFIELD,
        constraints=[
            "GET /{code} redirect endpoint must remain completely unmodified",
            "Reuse existing rate_limit_per_minute config field — do not add duplicate",
            "Redis already provisioned; use existing Redis infrastructure",
            "No database schema changes",
            "All existing tests must continue to pass",
        ],
        acceptance_criteria=[
            "AC-001: POST /shorten returns 429 with Retry-After header when limit exceeded",
            "AC-002: Rate limit counter resets after sliding window expires",
            "AC-003: GET /{code} latency unchanged (no additional middleware)",
            "AC-004: All existing tests in tests/unit/ and tests/integration/ pass",
            "AC-005: ≥90% unit test coverage on new rate_limit.py module",
        ],
    )


def create_brownfield_workflow() -> WorkflowDefinition:
    """Build the linear DAG for the brownfield rate-limiting scenario."""
    return WorkflowDefinition(
        name="brownfield_rate_limiting",
        description=(
            "Brownfield SDLC pipeline: enforce dormant rate_limit_per_minute config "
            "on POST /shorten using Redis sliding window — codebase analysis first"
        ),
        version="1.0.0",
        stages=[
            "codebase_analysis",
            "impact_analysis",
            "risk_assessment",
            "change_planning",
            "regression_test_planning",
            "validation",
        ],
        dependencies=[
            StageDependency(from_stage="codebase_analysis", to_stage="impact_analysis"),
            StageDependency(from_stage="impact_analysis",   to_stage="risk_assessment"),
            StageDependency(from_stage="risk_assessment",   to_stage="change_planning"),
            StageDependency(from_stage="change_planning",   to_stage="regression_test_planning"),
            StageDependency(from_stage="regression_test_planning", to_stage="validation"),
        ],
    )


def create_brownfield_stages() -> dict[str, BaseStage]:
    """Instantiate all stage implementations for the brownfield scenario."""
    return {
        "codebase_analysis":      CodebaseAnalysisStage(),
        "impact_analysis":        ImpactAnalysisStage(),
        "risk_assessment":        RiskAssessmentStage(),
        "change_planning":        ChangePlanningStage(),
        "regression_test_planning": RegressionTestPlanningStage(),
        "validation":             BrownfieldValidationStage(),
    }


async def run_brownfield_scenario(
    raw_requirement: str = DEFAULT_REQUIREMENT_TEXT,
    approval_gateway: ApprovalGateway | None = None,
    policy_engine: PolicyEngine | None = None,
) -> "WorkflowState":  # type: ignore[name-defined]
    """
    Run the full brownfield URL shortener rate-limiting scenario.

    Args:
        raw_requirement:  Raw requirement text (defaults to DEFAULT_REQUIREMENT_TEXT).
        approval_gateway: ApprovalGateway for the two human-approval stages.
                          Defaults to AutoApproveGateway.
        policy_engine:    Optional governance PolicyEngine.

    Returns:
        WorkflowState after the pipeline completes.
    """
    from orchestrator.core.autonomy import AutoApproveGateway

    requirement = create_brownfield_requirement(raw_requirement)
    definition = create_brownfield_workflow()
    stages = create_brownfield_stages()

    engine = WorkflowEngine(
        definition=definition,
        stages=stages,
        approval_gateway=approval_gateway or AutoApproveGateway(),
        policy_engine=policy_engine,
    )
    return await engine.run(requirement)
