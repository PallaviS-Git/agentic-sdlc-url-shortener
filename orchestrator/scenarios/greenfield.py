"""
Greenfield SDLC scenario: URL Shortener built from scratch.

This module implements the full agentic SDLC pipeline for a greenfield
engineering project.  Every stage runs through the real WorkflowEngine
with entry gates, governance, approvals, exit gates, and decision lineage.

Stage topology (partially parallel)
────────────────────────────────────

  requirements_analysis
          ↓
  architecture_design          [requires_approval=True, SIGNIFICANT]
          ↓
  task_decomposition
     ├── implementation_planning  ─┐
     ├── testing_planning         ─┤  (parallel fan-out)
     └── documentation_planning  ─┘
                  ↓  (synchronisation point)
              validation
                  ↓
        release_readiness        [requires_approval=True, HIGH_IMPACT, PRODUCTION_RELEASE]

Usage
─────
    from orchestrator.scenarios.greenfield import run_greenfield_scenario
    from orchestrator.core.autonomy import AutoApproveGateway

    state = await run_greenfield_scenario(
        approval_gateway=AutoApproveGateway()
    )
    assert state.status == WorkflowStatus.COMPLETED

Public API
──────────
    create_greenfield_requirement()  →  Requirement
    create_greenfield_workflow()     →  WorkflowDefinition
    create_greenfield_stages()       →  dict[str, BaseStage]
    run_greenfield_scenario(...)     →  WorkflowState   (async)
"""
from __future__ import annotations

import json
from typing import Any

from orchestrator.core.autonomy import ActionImpact, ApprovalGateway, HighImpactActionType
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.governance import PolicyEngine
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.models import (
    AmbiguityItem,
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


# ─── Default requirement text ─────────────────────────────────────────────────

DEFAULT_REQUIREMENT_TEXT = """
Build a production-quality URL shortener service from scratch.

The service must support:
- Creating short URLs from long URLs (with optional custom alias)
- Resolving short URLs to original long URLs (fast redirect)
- Basic click analytics (count, timestamp, user-agent)
- User authentication (JWT)
- URL expiry (optional TTL)
- URL deletion by owner

Non-functional requirements:
- Resolve short URL in <50ms p99
- Support 10,000 redirects/second at peak load
- 99.9% uptime SLA
- Short codes must be collision-free up to 1 billion URLs
- All write endpoints authenticated; read (redirect) endpoint public

Technology preference: Python, REST API, relational database.
""".strip()


# ─── Shared helpers ───────────────────────────────────────────────────────────


def _artifact(
    name: str,
    artifact_type: ArtifactType,
    stage: str,
    content: dict[str, Any] | str,
) -> Artifact:
    body = json.dumps(content, indent=2) if isinstance(content, dict) else content
    return Artifact(
        name=name,
        artifact_type=artifact_type,
        produced_by_stage=stage,
        content=body,
    )


def _decision(
    title: str,
    description: str,
    rationale: str,
    decision_type: DecisionType,
    stage: str,
    downstream_impacts: list[str] | None = None,
    parent_id: str | None = None,
) -> Decision:
    return Decision(
        decision_type=decision_type,
        title=title,
        description=description,
        rationale=rationale,
        stage=stage,
        downstream_impacts=downstream_impacts or [],
        parent_decision_id=parent_id,
    )


def _task(title: str, description: str, stage: str, agent: str) -> Task:
    return Task(
        title=title,
        description=description,
        status=TaskStatus.COMPLETED,
        stage=stage,
        assigned_agent=agent,
        rationale=f"Required by {stage} stage of the SDLC pipeline",
    )


def _validation(
    rule: str,
    passed: bool,
    message: str,
    stage: str,
    severity: ValidationSeverity = ValidationSeverity.ERROR,
    evidence: dict[str, Any] | None = None,
) -> ValidationResult:
    return ValidationResult(
        rule_name=rule,
        passed=passed,
        severity=severity,
        message=message,
        stage=stage,
        evidence=evidence or {},
    )


def _risk(title: str, description: str, severity: str, stage: str) -> Risk:
    return Risk(
        title=title,
        description=description,
        severity=RiskSeverity(severity.lower()),
        stage=stage,
    )


# ─── Stage 1: Requirements Analysis ──────────────────────────────────────────


class RequirementsAnalysisStage(BaseStage):
    """
    STAGE 1 — Requirement Understanding & Normalization

    Interprets the raw requirement text, identifies and resolves ambiguities,
    defines acceptance criteria, and produces a normalized engineering problem
    statement that all downstream stages consume.

    Produces
    ────────
    Artifact : normalized_requirement.json
    Decisions: scope decision (feature set for v1.0)
    Tasks    : parse, identify-ambiguities, define-criteria, scope-boundary
    """

    stage_name = "requirements_analysis"
    action_impact = ActionImpact.ROUTINE
    policy_metadata: dict = {}  # type: ignore[assignment]

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name="requirements_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        # ── Produce tasks ─────────────────────────────────────────────────────
        ctx.tasks.extend([
            _task("Parse raw requirement", "Extract structured FRs and NFRs from raw text", s, "requirements-parser-agent"),
            _task("Identify ambiguities", "Surface unclear terms and conflicting constraints", s, "requirements-analyst-agent"),
            _task("Resolve ambiguities", "Decide on concrete interpretations for each ambiguity", s, "requirements-analyst-agent"),
            _task("Define acceptance criteria", "Write measurable AC for each functional requirement", s, "requirements-analyst-agent"),
            _task("Define scope boundary", "Explicitly exclude features to prevent scope creep", s, "requirements-analyst-agent"),
        ])

        # ── Scope decision ────────────────────────────────────────────────────
        scope_dec = _decision(
            title="URL Shortener v1.0 Feature Scope",
            description="Confirmed in-scope features and explicit out-of-scope exclusions",
            rationale=(
                "Core value proposition is fast, reliable URL resolution. "
                "Analytics included as first-class feature to support business metrics. "
                "Advanced features (QR codes, custom domains, bulk import) deferred to "
                "v2.0 to keep initial scope deliverable within 2–3 days."
            ),
            decision_type=DecisionType.SCOPE,
            stage=s,
            downstream_impacts=["architecture_design", "task_decomposition"],
        )
        ctx.decisions.append(scope_dec)

        # ── Normalized requirement artifact ───────────────────────────────────
        normalized = {
            "title": "URL Shortener Service",
            "version": "1.0.0",
            "scope_decision_id": scope_dec.id,
            "functional_requirements": [
                {"id": "FR-001", "text": "Create short URL from long URL with optional custom alias"},
                {"id": "FR-002", "text": "Resolve short URL → 302 redirect to original long URL"},
                {"id": "FR-003", "text": "Track click analytics: count, timestamp, user-agent, IP"},
                {"id": "FR-004", "text": "List all URLs created by the authenticated user"},
                {"id": "FR-005", "text": "Set optional expiry (TTL) on short URLs"},
                {"id": "FR-006", "text": "Delete short URL (owner only)"},
            ],
            "non_functional_requirements": [
                {"id": "NFR-001", "text": "Resolve short URL in <50ms p99 (cache-warm path)"},
                {"id": "NFR-002", "text": "Support 10,000 redirects/second at peak"},
                {"id": "NFR-003", "text": "99.9% uptime SLA"},
                {"id": "NFR-004", "text": "Short codes collision-free up to 1 billion URLs"},
                {"id": "NFR-005", "text": "JWT auth for user sessions; API-key for programmatic access"},
            ],
            "acceptance_criteria": [
                "AC-001: POST /urls → 201 Created with short_url field within 200ms",
                "AC-002: GET /{code} → 302 redirect within 50ms (Redis cache warm)",
                "AC-003: Click event recorded asynchronously; no redirect latency impact",
                "AC-004: Unit test coverage ≥ 80% on service layer",
                "AC-005: Integration tests cover all 6 API endpoints",
                "AC-006: OpenAPI spec generated and validated",
            ],
            "ambiguities_resolved": [
                {"question": "Custom alias uniqueness?", "decision": "Reject duplicates with 409 Conflict"},
                {"question": "URL validation depth?", "decision": "Format validation required; reachability check optional"},
                {"question": "Analytics granularity?", "decision": "Per-click with timestamp, user-agent string, IP (hashed)"},
                {"question": "Auth mechanism?", "decision": "JWT (RS256) for users; HMAC API keys for programmatic access"},
                {"question": "Short code length?", "decision": "8 characters base62 → ~218 trillion combinations, far above 1B requirement"},
            ],
            "out_of_scope": [
                "QR code generation",
                "Custom domain support",
                "URL preview / screenshot capture",
                "Bulk URL import/export",
                "Real-time analytics dashboard",
            ],
        }
        art = _artifact("normalized_requirement.json", ArtifactType.DOCUMENTATION, s, normalized)
        ctx.artifacts.append(art)

        ctx.output_data["normalized_requirement"] = normalized
        ctx.output_data["requirement_artifact_id"] = art.id
        ctx.output_data["scope_decision_id"] = scope_dec.id
        ctx.output_data["fr_count"] = len(normalized["functional_requirements"])
        ctx.output_data["nfr_count"] = len(normalized["non_functional_requirements"])
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        nr = ctx.output_data.get("normalized_requirement", {})
        if not nr.get("functional_requirements"):
            return GateResult(gate_name="requirements_exit", passed=False,
                              reason="No functional requirements produced")
        if not ctx.decisions:
            return GateResult(gate_name="requirements_exit", passed=False,
                              reason="No scope decision recorded")
        ctx.validations.append(_validation(
            "requirement_normalized",
            True,
            f"Normalized requirement: {len(nr['functional_requirements'])} FRs, "
            f"{len(nr['non_functional_requirements'])} NFRs, "
            f"{len(nr['acceptance_criteria'])} ACs defined",
            s,
        ))
        ctx.validations.append(_validation(
            "ambiguities_resolved",
            True,
            f"{len(nr['ambiguities_resolved'])} ambiguities explicitly resolved",
            s,
        ))
        return GateResult(gate_name="requirements_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 2: Architecture Design ────────────────────────────────────────────


class ArchitectureDesignStage(BaseStage):
    """
    STAGE 2 — Architecture / Design

    Designs the system architecture from the normalized requirement.
    Produces Architecture Decision Records (ADRs), component diagram,
    and data model.

    Requires human approval before the architecture is locked in —
    downstream stages depend on these decisions, so errors here are costly.

    Produces
    ────────
    Artifact : architecture_design.json, data_model.json
    Decisions: 4 ADRs (framework, database, cache, auth)
    Risks    : vendor lock-in, cache invalidation complexity
    """

    stage_name = "architecture_design"
    requires_approval = True
    action_impact = ActionImpact.SIGNIFICANT
    policy_metadata = {"change_ticket_id": "CHG-2026-GF-001"}  # type: ignore[assignment]

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "normalized_requirement" not in ctx.input_data:
            return GateResult(
                gate_name="architecture_entry", passed=False,
                reason="normalized_requirement missing from upstream context",
            )
        return GateResult(gate_name="architecture_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Analyse NFRs for component constraints", "Map NFRs to architectural constraints", s, "architect-agent"),
            _task("Select technology stack", "Evaluate and choose framework, DB, cache, auth", s, "architect-agent"),
            _task("Design component diagram", "Document system components and their interactions", s, "architect-agent"),
            _task("Design data model", "Define entity relationships and indexes", s, "architect-agent"),
            _task("Write Architecture Decision Records", "Document rationale for each key decision", s, "architect-agent"),
            _task("Identify architectural risks", "Surface vendor lock-in and complexity risks", s, "architect-agent"),
        ])

        # ── Architecture Decision Records ─────────────────────────────────
        scope_dec_id = ctx.input_data.get("scope_decision_id")

        adr_framework = _decision(
            title="ADR-001: FastAPI as Web Framework",
            description="Use FastAPI (Python) with Uvicorn ASGI server",
            rationale=(
                "FastAPI provides async-native support critical for 10k req/s target. "
                "Built-in OpenAPI generation satisfies NFR for documented API. "
                "Type-safe with Pydantic v2. Strong ecosystem (SQLAlchemy, Redis). "
                "Alternatives considered: Django (sync-first, heavier), Flask (no async native)."
            ),
            decision_type=DecisionType.ARCHITECTURAL,
            stage=s,
            parent_id=scope_dec_id,
            downstream_impacts=["implementation_planning", "testing_planning"],
        )

        adr_db = _decision(
            title="ADR-002: PostgreSQL 15 as Primary Database",
            description="Use PostgreSQL 15 with SQLAlchemy 2 async ORM + Alembic migrations",
            rationale=(
                "PostgreSQL ACID guarantees prevent short-code collisions under concurrent inserts. "
                "Unique constraint on short_code column enforces collision-free guarantee. "
                "SQLAlchemy async supports our async framework choice. "
                "Alternatives considered: MySQL (weaker advisory locks), DynamoDB (adds AWS dependency)."
            ),
            decision_type=DecisionType.ARCHITECTURAL,
            stage=s,
            parent_id=adr_framework.id,
            downstream_impacts=["implementation_planning"],
        )

        adr_cache = _decision(
            title="ADR-003: Redis 7 for Redirect Cache",
            description="Cache short_code → long_url mappings in Redis with TTL",
            rationale=(
                "Redis GET is O(1) with sub-millisecond latency — critical for <50ms NFR. "
                "Cache warm path eliminates DB hit on every redirect. "
                "TTL in cache aligned with URL expiry. "
                "Trade-off: cache invalidation complexity on URL deletion/update (see risks)."
            ),
            decision_type=DecisionType.TRADE_OFF,
            stage=s,
            parent_id=adr_db.id,
            downstream_impacts=["implementation_planning", "testing_planning"],
        )

        adr_auth = _decision(
            title="ADR-004: JWT RS256 + API Key Authentication",
            description="User sessions via JWT RS256; programmatic access via HMAC-SHA256 API keys",
            rationale=(
                "JWT RS256: stateless, scalable, industry-standard. Public key distributed to "
                "services without shared secret. API keys for CLI/CI use cases where JWT refresh "
                "is impractical. Alternatives: sessions (stateful, doesn't scale horizontally), "
                "OAuth2 (overkill for v1.0)."
            ),
            decision_type=DecisionType.SECURITY,
            stage=s,
            parent_id=adr_framework.id,
            downstream_impacts=["implementation_planning", "testing_planning", "documentation_planning"],
        )

        ctx.decisions.extend([adr_framework, adr_db, adr_cache, adr_auth])

        # ── Architecture document ──────────────────────────────────────────
        arch_doc = {
            "adr_ids": [adr_framework.id, adr_db.id, adr_cache.id, adr_auth.id],
            "components": {
                "api_gateway": {
                    "technology": "FastAPI + Uvicorn",
                    "responsibility": "HTTP request handling, routing, auth middleware",
                    "ports": [8000],
                },
                "url_service": {
                    "technology": "Python service layer (async)",
                    "responsibility": "URL CRUD, short-code generation, analytics recording",
                },
                "cache": {
                    "technology": "Redis 7",
                    "responsibility": "Short-code → long-URL hot cache; redirect latency <5ms",
                    "ttl_policy": "Aligned with URL expiry; default 24h if no expiry set",
                },
                "database": {
                    "technology": "PostgreSQL 15",
                    "responsibility": "Persistent storage for URLs, analytics events, users",
                    "orm": "SQLAlchemy 2 async + Alembic",
                },
                "auth_service": {
                    "technology": "JWT RS256 + HMAC API keys",
                    "responsibility": "Token validation, key issuance, permission checks",
                },
            },
            "api_surface": {
                "POST /urls": "Create short URL",
                "GET /{code}": "Resolve and redirect (public)",
                "GET /urls": "List authenticated user's URLs",
                "DELETE /urls/{id}": "Delete URL (owner only)",
                "GET /urls/{id}/analytics": "Get click analytics for URL",
            },
            "deployment": {
                "containerisation": "Docker + docker-compose for local dev",
                "target": "Cloud-agnostic (Kubernetes-ready)",
            },
        }
        art_arch = _artifact("architecture_design.json", ArtifactType.DOCUMENTATION, s, arch_doc)

        # ── Data model ────────────────────────────────────────────────────
        data_model = {
            "entities": {
                "url": {
                    "columns": [
                        "id UUID PK",
                        "short_code VARCHAR(8) UNIQUE NOT NULL",
                        "long_url TEXT NOT NULL",
                        "owner_id UUID FK(user.id)",
                        "created_at TIMESTAMPTZ DEFAULT NOW()",
                        "expires_at TIMESTAMPTZ NULL",
                        "is_deleted BOOLEAN DEFAULT FALSE",
                    ],
                    "indexes": [
                        "UNIQUE(short_code)",
                        "INDEX(owner_id)",
                        "INDEX(expires_at) WHERE expires_at IS NOT NULL",
                    ],
                },
                "click_event": {
                    "columns": [
                        "id UUID PK",
                        "url_id UUID FK(url.id)",
                        "clicked_at TIMESTAMPTZ DEFAULT NOW()",
                        "user_agent TEXT",
                        "ip_hash VARCHAR(64)",
                        "country_code CHAR(2)",
                    ],
                    "indexes": ["INDEX(url_id, clicked_at)"],
                },
                "user": {
                    "columns": [
                        "id UUID PK",
                        "email VARCHAR(255) UNIQUE NOT NULL",
                        "hashed_password VARCHAR(128)",
                        "api_key_hash VARCHAR(64) NULL",
                        "created_at TIMESTAMPTZ DEFAULT NOW()",
                    ],
                },
            },
            "short_code_generation": {
                "algorithm": "base62(random_bytes(6))",
                "collision_handling": "DB unique constraint + retry on IntegrityError (max 3 attempts)",
                "keyspace": "62^8 ≈ 218 trillion unique codes",
            },
        }
        art_dm = _artifact("data_model.json", ArtifactType.SCHEMA, s, data_model)

        ctx.artifacts.extend([art_arch, art_dm])

        # ── Risks ─────────────────────────────────────────────────────────
        s = self.stage_name
        ctx.risks.extend([
            _risk(
                "Cache invalidation complexity",
                "Redis cache must be invalidated immediately when a URL is deleted or expires. "
                "Stale cache entries could serve deleted/expired URLs.",
                "high", s,
            ),
            _risk(
                "PostgreSQL as bottleneck at 10k req/s",
                "Redis cache miss path hits DB. Under heavy cache-miss load (e.g. after cache flush), "
                "PostgreSQL may become the bottleneck.",
                "medium", s,
            ),
        ])

        ctx.output_data["architecture_artifact_id"] = art_arch.id
        ctx.output_data["data_model_artifact_id"] = art_dm.id
        ctx.output_data["adr_ids"] = [d.id for d in [adr_framework, adr_db, adr_cache, adr_auth]]
        ctx.output_data["tech_stack"] = {
            "framework": "FastAPI", "db": "PostgreSQL 15",
            "cache": "Redis 7", "auth": "JWT RS256",
        }
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        s = self.stage_name
        if len(ctx.artifacts) < 2:
            return GateResult(gate_name="architecture_exit", passed=False,
                              reason="Expected architecture + data-model artifacts")
        if len(ctx.decisions) < 4:
            return GateResult(gate_name="architecture_exit", passed=False,
                              reason=f"Expected ≥4 ADRs, got {len(ctx.decisions)}")
        ctx.validations.append(_validation(
            "architecture_complete",
            True,
            f"Architecture design complete: {len(ctx.decisions)} ADRs, "
            f"{len(ctx.artifacts)} artifacts, {len(ctx.risks)} risks identified",
            s,
        ))
        ctx.validations.append(_validation(
            "data_model_defined",
            True,
            "Data model covers URL, click_event, and user entities with appropriate indexes",
            s,
        ))
        return GateResult(gate_name="architecture_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 3: Task Decomposition ─────────────────────────────────────────────


class TaskDecompositionStage(BaseStage):
    """
    STAGE 3 — Task Decomposition

    Breaks the architecture into a concrete set of implementation, testing,
    and documentation tasks with estimated effort and dependencies.

    Produces
    ────────
    Artifact : task_graph.json
    Decisions: decomposition strategy decision
    """

    stage_name = "task_decomposition"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "adr_ids" not in ctx.input_data:
            return GateResult(gate_name="decomposition_entry", passed=False,
                              reason="Architecture ADRs missing from context")
        return GateResult(gate_name="decomposition_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Map FRs to implementation tasks", "One implementation task per functional requirement", s, "tech-lead-agent"),
            _task("Identify shared infrastructure tasks", "Auth middleware, DB connection pool, error handlers", s, "tech-lead-agent"),
            _task("Map architecture risks to test tasks", "Each identified risk has a corresponding test task", s, "tech-lead-agent"),
            _task("Define task dependency graph", "Specify which tasks block others", s, "tech-lead-agent"),
        ])

        impl_tasks = [
            {"id": "IMPL-001", "title": "URL model + DB schema", "fr": "FR-001/002", "effort_h": 3, "depends_on": []},
            {"id": "IMPL-002", "title": "Short-code generator (base62)", "fr": "FR-001", "effort_h": 2, "depends_on": ["IMPL-001"]},
            {"id": "IMPL-003", "title": "POST /urls endpoint", "fr": "FR-001", "effort_h": 3, "depends_on": ["IMPL-001", "IMPL-002"]},
            {"id": "IMPL-004", "title": "GET /{code} redirect endpoint + Redis cache", "fr": "FR-002", "effort_h": 4, "depends_on": ["IMPL-001", "IMPL-002"]},
            {"id": "IMPL-005", "title": "Click analytics recording (async background task)", "fr": "FR-003", "effort_h": 3, "depends_on": ["IMPL-004"]},
            {"id": "IMPL-006", "title": "GET /urls list endpoint (paginated)", "fr": "FR-004", "effort_h": 2, "depends_on": ["IMPL-001"]},
            {"id": "IMPL-007", "title": "URL expiry enforcement (cron job + cache TTL)", "fr": "FR-005", "effort_h": 3, "depends_on": ["IMPL-004"]},
            {"id": "IMPL-008", "title": "DELETE /urls/{id} endpoint + cache invalidation", "fr": "FR-006", "effort_h": 2, "depends_on": ["IMPL-004"]},
            {"id": "IMPL-009", "title": "JWT auth middleware + API key auth", "fr": "NFR-005", "effort_h": 4, "depends_on": ["IMPL-001"]},
            {"id": "IMPL-010", "title": "Database migrations (Alembic)", "fr": "ALL", "effort_h": 1, "depends_on": ["IMPL-001"]},
        ]
        test_tasks = [
            {"id": "TEST-001", "title": "Unit tests: URL service layer", "covers": ["IMPL-001", "IMPL-002", "IMPL-003"], "effort_h": 4},
            {"id": "TEST-002", "title": "Unit tests: redirect + cache logic", "covers": ["IMPL-004", "IMPL-005"], "effort_h": 3},
            {"id": "TEST-003", "title": "Unit tests: auth middleware", "covers": ["IMPL-009"], "effort_h": 2},
            {"id": "TEST-004", "title": "Integration tests: all API endpoints (httpx)", "covers": ["ALL"], "effort_h": 5},
            {"id": "TEST-005", "title": "Load test: 10k req/s redirect path", "covers": ["NFR-002"], "effort_h": 3},
            {"id": "TEST-006", "title": "Cache invalidation regression tests", "covers": ["IMPL-007", "IMPL-008"], "effort_h": 2},
        ]
        doc_tasks = [
            {"id": "DOC-001", "title": "OpenAPI spec (auto-generated via FastAPI)", "effort_h": 1},
            {"id": "DOC-002", "title": "README with setup + usage guide", "effort_h": 2},
            {"id": "DOC-003", "title": "Architecture overview document", "effort_h": 2},
            {"id": "DOC-004", "title": "API authentication guide", "effort_h": 1},
        ]

        dec = _decision(
            title="IMPL-first + parallel TEST/DOC strategy",
            description="Decompose into 10 implementation tasks, 6 test tasks, 4 doc tasks executed in parallel after decomposition",
            rationale=(
                "Implementation tasks have natural dependencies (schema before endpoints). "
                "Test and documentation planning can proceed in parallel once the task graph "
                "is defined, reducing critical path by ~40%."
            ),
            decision_type=DecisionType.IMPLEMENTATION,
            stage=s,
            downstream_impacts=["implementation_planning", "testing_planning", "documentation_planning"],
        )
        ctx.decisions.append(dec)

        task_graph = {
            "decomposition_decision_id": dec.id,
            "implementation_tasks": impl_tasks,
            "test_tasks": test_tasks,
            "documentation_tasks": doc_tasks,
            "summary": {
                "total_tasks": len(impl_tasks) + len(test_tasks) + len(doc_tasks),
                "total_effort_hours": (
                    sum(t["effort_h"] for t in impl_tasks)
                    + sum(t["effort_h"] for t in test_tasks)
                    + sum(t["effort_h"] for t in doc_tasks)
                ),
                "parallel_paths": ["implementation_planning", "testing_planning", "documentation_planning"],
                "critical_path": "IMPL-001 → IMPL-002 → IMPL-004 → TEST-002",
            },
        }
        art = _artifact("task_graph.json", ArtifactType.DOCUMENTATION, s, task_graph)
        ctx.artifacts.append(art)

        ctx.output_data["task_graph"] = task_graph
        ctx.output_data["task_graph_artifact_id"] = art.id
        ctx.output_data["impl_task_count"] = len(impl_tasks)
        ctx.output_data["test_task_count"] = len(test_tasks)
        ctx.output_data["doc_task_count"] = len(doc_tasks)
        ctx.output_data["total_effort_hours"] = task_graph["summary"]["total_effort_hours"]
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        tg = ctx.output_data.get("task_graph", {})
        if not tg.get("implementation_tasks"):
            return GateResult(gate_name="decomposition_exit", passed=False,
                              reason="No implementation tasks defined")
        total = ctx.output_data.get("impl_task_count", 0)
        ctx.validations.append(_validation(
            "tasks_decomposed",
            True,
            f"Task decomposition complete: {total} impl tasks, "
            f"{ctx.output_data.get('test_task_count', 0)} test tasks, "
            f"{ctx.output_data.get('doc_task_count', 0)} doc tasks",
            self.stage_name,
        ))
        return GateResult(gate_name="decomposition_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 4: Implementation Planning ────────────────────────────────────────


class ImplementationPlanningStage(BaseStage):
    """
    STAGE 4 — Implementation Planning (parallel with testing + docs)

    Designs the code structure, API contract, service layer, and database
    access patterns.  Does NOT write code — produces the implementation plan
    that engineers and agents will execute.

    Produces
    ────────
    Artifact : implementation_plan.json
    Decisions: code organisation + error handling strategy
    """

    stage_name = "implementation_planning"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "task_graph" not in ctx.input_data:
            return GateResult(gate_name="impl_planning_entry", passed=False,
                              reason="task_graph missing from upstream context")
        return GateResult(gate_name="impl_planning_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Design package structure", "Define module layout and boundaries", s, "senior-engineer-agent"),
            _task("Design API contract (OpenAPI-first)", "Define request/response schemas for all endpoints", s, "senior-engineer-agent"),
            _task("Design service layer interfaces", "Define async service interfaces consumed by router", s, "senior-engineer-agent"),
            _task("Plan error handling strategy", "Map business errors to HTTP status codes", s, "senior-engineer-agent"),
            _task("Plan dependency injection", "Design FastAPI dependency tree", s, "senior-engineer-agent"),
        ])

        dec_structure = _decision(
            title="Layered architecture: router → service → repository",
            description="Separate concerns: HTTP layer, business logic, data access",
            rationale=(
                "Router layer handles request/response serialisation only. "
                "Service layer owns business rules (short-code generation, expiry logic). "
                "Repository layer owns all DB queries (testable with mock DB in unit tests). "
                "This separation allows unit-testing service logic without running a database."
            ),
            decision_type=DecisionType.IMPLEMENTATION,
            stage=s,
            downstream_impacts=["testing_planning"],
        )
        dec_errors = _decision(
            title="RFC 7807 Problem Details for error responses",
            description="Return structured JSON error bodies following RFC 7807",
            rationale=(
                "Standard format enables clients to parse errors reliably. "
                "FastAPI's HTTPException supports custom response models. "
                "Avoids ad-hoc error formats that break API clients on schema change."
            ),
            decision_type=DecisionType.IMPLEMENTATION,
            stage=s,
        )
        ctx.decisions.extend([dec_structure, dec_errors])

        impl_plan = {
            "package_layout": {
                "app/": {
                    "main.py": "FastAPI application factory, lifespan events",
                    "router/": {
                        "urls.py": "CRUD endpoints for URL resource",
                        "auth.py": "Login, token refresh, API key management",
                    },
                    "service/": {
                        "url_service.py": "URL CRUD + short-code generation",
                        "analytics_service.py": "Click event recording (async)",
                        "auth_service.py": "JWT issuance + verification, API key hashing",
                    },
                    "repository/": {
                        "url_repo.py": "SQLAlchemy async CRUD for URL + ClickEvent",
                        "user_repo.py": "User lookup by email / api_key_hash",
                    },
                    "models/": {
                        "db.py": "SQLAlchemy ORM models (URL, ClickEvent, User)",
                        "schemas.py": "Pydantic request/response schemas",
                    },
                    "core/": {
                        "config.py": "Settings from env vars (Pydantic BaseSettings)",
                        "database.py": "Async SQLAlchemy engine + session factory",
                        "redis.py": "Redis connection pool + helper functions",
                        "security.py": "JWT RS256 encode/decode, API key generation",
                    },
                },
            },
            "api_endpoints": [
                {"method": "POST", "path": "/urls", "auth": "required", "status_codes": [201, 409, 422]},
                {"method": "GET", "path": "/{code}", "auth": "none", "status_codes": [302, 404, 410]},
                {"method": "GET", "path": "/urls", "auth": "required", "status_codes": [200, 401]},
                {"method": "DELETE", "path": "/urls/{id}", "auth": "required", "status_codes": [204, 403, 404]},
                {"method": "GET", "path": "/urls/{id}/analytics", "auth": "required", "status_codes": [200, 403, 404]},
            ],
            "error_handling": {
                "strategy": "RFC 7807 Problem Details",
                "custom_exceptions": ["DuplicateShortCodeError", "URLNotFoundError", "URLExpiredError", "UnauthorisedError"],
            },
            "configuration": {
                "settings_class": "Settings(BaseSettings)",
                "required_env": ["DATABASE_URL", "REDIS_URL", "JWT_PRIVATE_KEY", "JWT_PUBLIC_KEY"],
            },
        }
        art = _artifact("implementation_plan.json", ArtifactType.DOCUMENTATION, s, impl_plan)
        ctx.artifacts.append(art)

        ctx.output_data["implementation_plan"] = impl_plan
        ctx.output_data["implementation_plan_artifact_id"] = art.id
        ctx.output_data["endpoint_count"] = len(impl_plan["api_endpoints"])
        ctx.output_data["implementation_ready"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        if not ctx.output_data.get("implementation_ready"):
            return GateResult(gate_name="impl_planning_exit", passed=False,
                              reason="implementation_ready flag not set")
        ctx.validations.append(_validation(
            "implementation_plan_complete",
            True,
            f"Implementation plan complete: {ctx.output_data['endpoint_count']} API endpoints designed, "
            "layered architecture defined, error-handling strategy selected",
            self.stage_name,
        ))
        return GateResult(gate_name="impl_planning_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 5: Testing Planning ────────────────────────────────────────────────


class TestingPlanningStage(BaseStage):
    """
    STAGE 5 — Testing Planning (parallel with implementation + docs)

    Defines the test strategy, coverage targets, and specific test cases
    for unit, integration, and non-functional requirements.

    Produces
    ────────
    Artifact : test_plan.json
    Decisions: test framework selection, coverage strategy
    """

    stage_name = "testing_planning"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "task_graph" not in ctx.input_data:
            return GateResult(gate_name="testing_planning_entry", passed=False,
                              reason="task_graph missing from upstream context")
        return GateResult(gate_name="testing_planning_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Design unit test strategy", "Define unit test cases for service layer", s, "qa-agent"),
            _task("Design integration test strategy", "Define end-to-end API tests using httpx", s, "qa-agent"),
            _task("Define coverage targets", "Set per-module coverage thresholds", s, "qa-agent"),
            _task("Plan load test", "Design load test for 10k req/s NFR", s, "qa-agent"),
            _task("Plan cache invalidation tests", "Design regression tests for cache consistency", s, "qa-agent"),
        ])

        dec_framework = _decision(
            title="pytest + pytest-asyncio + httpx for all test layers",
            description="Single test framework across unit, integration, and async tests",
            rationale=(
                "pytest-asyncio runs async test functions natively — required for FastAPI async routes. "
                "httpx.AsyncClient used with FastAPI TestClient for integration tests without running a server. "
                "Single framework reduces test infrastructure complexity."
            ),
            decision_type=DecisionType.IMPLEMENTATION,
            stage=s,
        )
        dec_coverage = _decision(
            title="80% line coverage minimum on service layer",
            description="Coverage target: ≥80% on app/service/, ≥70% overall",
            rationale=(
                "80% coverage on service layer catches most business logic bugs. "
                "Router and repository layers covered by integration tests, not unit tests. "
                "100% coverage target discouraged: leads to testing-for-coverage rather than testing-for-behaviour."
            ),
            decision_type=DecisionType.TRADE_OFF,
            stage=s,
        )
        ctx.decisions.extend([dec_framework, dec_coverage])

        test_plan = {
            "framework": "pytest + pytest-asyncio + httpx",
            "coverage_targets": {"service_layer": 80, "overall": 70, "router_layer": 60},
            "unit_tests": {
                "url_service": [
                    "test_create_url_returns_short_code",
                    "test_create_url_duplicate_alias_raises_409",
                    "test_create_url_custom_alias_accepted",
                    "test_get_url_by_code_returns_long_url",
                    "test_get_url_not_found_raises_404",
                    "test_get_url_expired_raises_410",
                    "test_delete_url_owner_succeeds",
                    "test_delete_url_non_owner_raises_403",
                    "test_short_code_generation_no_collision",
                ],
                "analytics_service": [
                    "test_record_click_stores_event",
                    "test_get_analytics_returns_count",
                ],
                "auth_service": [
                    "test_create_jwt_verifiable",
                    "test_expired_jwt_raises",
                    "test_api_key_hash_deterministic",
                ],
            },
            "integration_tests": [
                "test_post_url_creates_and_resolves",
                "test_redirect_returns_302",
                "test_redirect_expired_url_returns_410",
                "test_auth_required_on_create",
                "test_list_urls_paginated",
                "test_delete_url_removes_from_cache",
            ],
            "non_functional_tests": {
                "load_test": {
                    "tool": "locust",
                    "target_rps": 10000,
                    "scenario": "70% GET /{code}, 20% POST /urls, 10% GET /urls",
                    "pass_criteria": "p99 < 50ms on GET /{code} with Redis warm",
                },
                "collision_test": {
                    "scenario": "Generate 10M short codes and assert 0 duplicates",
                    "tool": "pytest parametrise + set()",
                },
            },
        }
        art = _artifact("test_plan.json", ArtifactType.TEST, s, test_plan)
        ctx.artifacts.append(art)

        ctx.output_data["test_plan"] = test_plan
        ctx.output_data["test_plan_artifact_id"] = art.id
        ctx.output_data["unit_test_count"] = sum(len(v) for v in test_plan["unit_tests"].values())
        ctx.output_data["integration_test_count"] = len(test_plan["integration_tests"])
        ctx.output_data["coverage_target"] = dec_coverage.id
        ctx.output_data["testing_ready"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        if not ctx.output_data.get("testing_ready"):
            return GateResult(gate_name="testing_planning_exit", passed=False,
                              reason="testing_ready flag not set")
        ctx.validations.append(_validation(
            "test_plan_complete",
            True,
            f"Test plan complete: {ctx.output_data['unit_test_count']} unit tests, "
            f"{ctx.output_data['integration_test_count']} integration tests, "
            "load test and collision test defined",
            self.stage_name,
        ))
        return GateResult(gate_name="testing_planning_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 6: Documentation Planning ─────────────────────────────────────────


class DocumentationPlanningStage(BaseStage):
    """
    STAGE 6 — Documentation Planning (parallel with implementation + testing)

    Defines the documentation deliverables: OpenAPI spec, README, architecture
    overview, and API authentication guide.

    Produces
    ────────
    Artifact : documentation_plan.json
    Decisions: auto-generated OpenAPI vs hand-written
    """

    stage_name = "documentation_planning"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if "task_graph" not in ctx.input_data:
            return GateResult(gate_name="doc_planning_entry", passed=False,
                              reason="task_graph missing from upstream context")
        return GateResult(gate_name="doc_planning_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Plan OpenAPI specification", "Define spec structure auto-generated by FastAPI", s, "tech-writer-agent"),
            _task("Plan README", "Define README sections and content outline", s, "tech-writer-agent"),
            _task("Plan architecture overview", "Outline system design document for engineers", s, "tech-writer-agent"),
            _task("Plan authentication guide", "Write auth flow and token usage guide", s, "tech-writer-agent"),
        ])

        dec_openapi = _decision(
            title="Auto-generated OpenAPI via FastAPI — not hand-written",
            description="Use FastAPI's native OpenAPI generation; enrich with response_model annotations",
            rationale=(
                "FastAPI generates OpenAPI 3.0.3 spec automatically from Pydantic schemas. "
                "Reduces manual documentation drift risk. "
                "Engineers add docstrings and response_model annotations as part of implementation; "
                "spec always reflects actual code state."
            ),
            decision_type=DecisionType.IMPLEMENTATION,
            stage=s,
        )
        ctx.decisions.append(dec_openapi)

        doc_plan = {
            "openapi_spec": {
                "generation": "FastAPI auto-generate at /openapi.json",
                "interactive_ui": "/docs (Swagger UI) and /redoc",
                "schema_source": "Pydantic v2 models in app/models/schemas.py",
                "enrichment": ["operation summaries via docstrings", "example values in Field()", "tag grouping"],
            },
            "readme_sections": [
                "Project overview and motivation",
                "Quick start (Docker compose)",
                "Environment variables reference",
                "API endpoint summary with curl examples",
                "Authentication guide (JWT + API key)",
                "Running tests and checking coverage",
                "Architecture overview link",
                "Contributing guidelines",
                "Limitations and known trade-offs",
            ],
            "architecture_overview": {
                "format": "Markdown with ASCII component diagram",
                "sections": ["Component overview", "Data flow (create URL, resolve URL)", "ADR index"],
            },
            "auth_guide": {
                "topics": ["Obtaining JWT token", "Using JWT in requests", "Generating API key", "Key rotation"],
            },
        }
        art = _artifact("documentation_plan.json", ArtifactType.DOCUMENTATION, s, doc_plan)
        ctx.artifacts.append(art)

        ctx.output_data["documentation_plan"] = doc_plan
        ctx.output_data["documentation_plan_artifact_id"] = art.id
        ctx.output_data["documentation_ready"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        if not ctx.output_data.get("documentation_ready"):
            return GateResult(gate_name="doc_planning_exit", passed=False,
                              reason="documentation_ready flag not set")
        ctx.validations.append(_validation(
            "documentation_plan_complete",
            True,
            "Documentation plan complete: OpenAPI auto-generation strategy defined, "
            "README outline and architecture overview planned",
            self.stage_name,
        ))
        return GateResult(gate_name="doc_planning_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 7: Validation ─────────────────────────────────────────────────────


class ValidationStage(BaseStage):
    """
    STAGE 7 — Validation (synchronisation point)

    Validates that all plans collectively cover all requirements, that no
    acceptance criteria are unaddressed, that identified risks have
    mitigation plans, and that a security review has been performed.

    Entry gate enforces that all three planning stages have completed.

    Produces
    ────────
    Artifact : validation_report.json
    Validations: ≥7 validation checks across coverage, security, completeness
    Risks: residual risks documented
    """

    stage_name = "validation"
    action_impact = ActionImpact.ROUTINE

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        required = ["implementation_ready", "testing_ready", "documentation_ready"]
        missing = [k for k in required if not ctx.input_data.get(k)]
        if missing:
            return GateResult(
                gate_name="validation_entry", passed=False,
                reason=f"Planning stages not complete: {missing}",
            )
        return GateResult(gate_name="validation_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Verify requirement coverage", "Confirm all 6 FRs have implementation + test tasks", s, "qa-lead-agent"),
            _task("Verify NFR coverage", "Confirm all 5 NFRs have acceptance criteria and tests", s, "qa-lead-agent"),
            _task("Security review", "Review auth design, input validation, SQL injection prevention", s, "security-agent"),
            _task("Risk mitigation review", "Confirm each identified risk has a mitigation plan", s, "risk-agent"),
            _task("Compliance check", "Verify GDPR implications of analytics data collection", s, "compliance-agent"),
            _task("Compile validation report", "Aggregate all validation results", s, "qa-lead-agent"),
        ])

        validation_results = [
            _validation("all_fr_covered",         True, "All 6 functional requirements have implementation tasks",  s),
            _validation("all_nfr_covered",         True, "All 5 NFRs have corresponding test or design coverage",    s),
            _validation("acceptance_criteria_met", True, "All 6 acceptance criteria traceable to plan artifacts",    s),
            _validation("security_review_passed",  True, "Auth design uses RS256 JWT (industry standard). SQL injection prevented by ORM parameterised queries. Input validated by Pydantic.", s, evidence={"auth_mechanism": "JWT RS256", "sql_injection": "ORM only", "input_validation": "Pydantic v2"}),
            _validation("risk_mitigations_defined",True, "2 identified risks have documented mitigation strategies", s, evidence={"risks_reviewed": 2}),
            _validation("test_coverage_planned",   True, f"Coverage target ≥80% service layer defined in test plan",  s),
            _validation("gdpr_compliance_checked", True, "Analytics records IP as one-way hash (SHA-256). No PII stored in analytics.", s, evidence={"ip_storage": "SHA-256 hash", "pii": "none"}),
        ]
        ctx.validations.extend(validation_results)

        report = {
            "validation_date": "automated by orchestrator",
            "total_checks": len(validation_results),
            "passed": sum(1 for v in validation_results if v.passed),
            "failed": sum(1 for v in validation_results if not v.passed),
            "critical_failures": 0,
            "security_scan_result": "PASSED",
            "gdpr_compliant": True,
            "residual_risks": [
                {
                    "risk": "Cache invalidation complexity",
                    "mitigation": "Cache invalidation on every write; cache TTL aligned with URL expiry; integration test for delete-then-resolve",
                    "residual_likelihood": "low",
                },
                {
                    "risk": "PostgreSQL bottleneck under cache-miss storm",
                    "mitigation": "Connection pool sizing; circuit breaker on DB; read replica for analytics queries",
                    "residual_likelihood": "low",
                },
            ],
            "sign_off_criteria": {
                "all_fr_covered":          True,
                "all_nfr_covered":         True,
                "security_review_passed":  True,
                "test_plan_approved":      True,
                "documentation_planned":   True,
            },
        }
        art = _artifact("validation_report.json", ArtifactType.REPORT, s, report)
        ctx.artifacts.append(art)

        ctx.output_data["validation_report"] = report
        ctx.output_data["validation_report_artifact_id"] = art.id
        ctx.output_data["validation_passed"] = True
        ctx.output_data["security_scan_passed"] = True
        ctx.output_data["total_validations"] = len(validation_results)
        ctx.output_data["critical_failures"] = 0
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        if not ctx.output_data.get("validation_passed"):
            return GateResult(gate_name="validation_exit", passed=False,
                              reason="validation_passed flag not set")
        if ctx.output_data.get("critical_failures", 1) > 0:
            return GateResult(gate_name="validation_exit", passed=False,
                              reason=f"Critical failures: {ctx.output_data['critical_failures']}")
        return GateResult(gate_name="validation_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Stage 8: Release Readiness ───────────────────────────────────────────────


class ReleaseReadinessStage(BaseStage):
    """
    STAGE 8 — Release Readiness (final gate)

    Compiles the release checklist, verifies all gates passed, and produces
    the final sign-off artifact.

    This is a HIGH_IMPACT PRODUCTION_RELEASE action — requires human approval
    before the checklist is marked complete.  Policy guardrails enforce that
    a security scan has passed and a rollback plan is documented.

    Produces
    ────────
    Artifact : release_checklist.json
    Decisions: release approval decision
    """

    stage_name = "release_readiness"
    requires_approval = True
    action_impact = ActionImpact.HIGH_IMPACT
    high_impact_action_type = HighImpactActionType.PRODUCTION_RELEASE
    policy_metadata = {  # type: ignore[assignment]
        "security_scan_passed": True,
        "change_ticket_id": "CHG-2026-GF-001",
        "rollback_plan_documented": True,
    }

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        if not ctx.input_data.get("validation_passed"):
            return GateResult(gate_name="release_entry", passed=False,
                              reason="validation_passed not confirmed by upstream validation stage")
        if ctx.input_data.get("critical_failures", 1) > 0:
            return GateResult(gate_name="release_entry", passed=False,
                              reason="Critical validation failures present — cannot proceed to release")
        return GateResult(gate_name="release_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        s = self.stage_name

        ctx.tasks.extend([
            _task("Verify all validation gates passed", "Cross-check validation_report critical_failures == 0", s, "release-manager-agent"),
            _task("Confirm rollback plan documented", "Verify ADR-003 cache invalidation + DB migration rollback documented", s, "release-manager-agent"),
            _task("Compile release checklist", "Aggregate all sign-off criteria into release artifact", s, "release-manager-agent"),
            _task("Confirm environment readiness", "Verify Docker compose and env vars documented", s, "release-manager-agent"),
        ])

        release_dec = _decision(
            title="URL Shortener v1.0 — APPROVED FOR RELEASE",
            description=(
                "All SDLC gates passed. Normalized requirements, architecture, task decomposition, "
                "implementation plan, test plan, documentation plan, and validation report are complete."
            ),
            rationale=(
                "8-stage SDLC pipeline completed with 0 critical failures. "
                "Security scan passed. All 6 FRs covered. All 5 NFRs addressed. "
                "Rollback plan documented in ADR-003 and data migration strategy. "
                "Human approval obtained at architecture_design and release_readiness stages."
            ),
            decision_type=DecisionType.SCOPE,
            stage=s,
        )
        ctx.decisions.append(release_dec)

        checklist = {
            "version": "1.0.0",
            "release_decision_id": release_dec.id,
            "checklist": {
                "requirements_normalized":   {"status": "PASSED", "artifact": "normalized_requirement.json"},
                "architecture_approved":     {"status": "PASSED", "artifact": "architecture_design.json", "human_approved": True},
                "tasks_decomposed":          {"status": "PASSED", "artifact": "task_graph.json"},
                "implementation_planned":    {"status": "PASSED", "artifact": "implementation_plan.json"},
                "tests_planned":             {"status": "PASSED", "artifact": "test_plan.json"},
                "documentation_planned":     {"status": "PASSED", "artifact": "documentation_plan.json"},
                "validation_passed":         {"status": "PASSED", "artifact": "validation_report.json"},
                "security_scan_passed":      {"status": "PASSED"},
                "gdpr_compliance_verified":  {"status": "PASSED"},
                "rollback_plan_documented":  {"status": "PASSED"},
                "release_approved_by_human": {"status": "PENDING → APPROVED via approval gateway"},
            },
            "release_metadata": {
                "pipeline_stages": 8,
                "total_artifacts": "≥9",
                "total_decisions": "≥8",
                "total_tasks": "≥35",
                "total_validation_rules": "≥9",
                "human_approvals_required": 2,
            },
            "rollback_plan": {
                "cache": "FLUSHDB on Redis; no data loss (Redis is cache only)",
                "database": "Alembic downgrade to previous migration",
                "code": "Git revert + re-deploy previous container image",
                "rto_estimate": "< 15 minutes",
            },
            "go_live_prerequisites": [
                "Infrastructure provisioned (PostgreSQL, Redis, Kubernetes namespace)",
                "Environment variables set (DATABASE_URL, REDIS_URL, JWT_PRIVATE_KEY)",
                "Alembic migrations applied",
                "Health check endpoint /health returns 200",
                "Load test passing at 10k req/s",
            ],
        }
        art = _artifact("release_checklist.json", ArtifactType.REPORT, s, checklist)
        ctx.artifacts.append(art)

        ctx.validations.append(_validation(
            "release_checklist_complete",
            True,
            "All release checklist items PASSED. Workflow is release-ready.",
            s,
            evidence={"critical_failures": 0, "human_approvals": 2},
        ))

        ctx.output_data["release_checklist"] = checklist
        ctx.output_data["release_checklist_artifact_id"] = art.id
        ctx.output_data["release_decision_id"] = release_dec.id
        ctx.output_data["go_live_ready"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        if not ctx.output_data.get("go_live_ready"):
            return GateResult(gate_name="release_exit", passed=False,
                              reason="go_live_ready not confirmed")
        ctx.validations.append(_validation(
            "final_sign_off",
            True,
            "Release readiness confirmed. URL Shortener v1.0 pipeline complete.",
            self.stage_name,
        ))
        return GateResult(gate_name="release_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


# ─── Workflow factory functions ───────────────────────────────────────────────


def create_greenfield_requirement(raw_text: str = DEFAULT_REQUIREMENT_TEXT) -> Requirement:
    """Build the Requirement object for the greenfield URL shortener scenario."""
    return Requirement(
        title="URL Shortener Service — Greenfield",
        raw_text=raw_text,
        requirement_type=RequirementType.GREENFIELD,
        constraints=[
            "Python technology stack preferred",
            "Must integrate with existing PostgreSQL + Redis infrastructure",
            "Delivery target: 2-3 engineering days",
        ],
        acceptance_criteria=[
            "POST /urls creates short URL within 200ms",
            "GET /{code} redirects within 50ms (cache warm)",
            "Unit test coverage ≥ 80% on service layer",
            "OpenAPI spec generated and accurate",
        ],
    )


def create_greenfield_workflow() -> WorkflowDefinition:
    """Build the WorkflowDefinition DAG for the greenfield URL shortener scenario."""
    return WorkflowDefinition(
        name="greenfield_url_shortener",
        description=(
            "Full SDLC pipeline for URL Shortener from scratch: "
            "requirements → architecture → decomposition → "
            "[impl|test|docs] → validation → release"
        ),
        version="1.0.0",
        stages=[
            "requirements_analysis",
            "architecture_design",
            "task_decomposition",
            "implementation_planning",
            "testing_planning",
            "documentation_planning",
            "validation",
            "release_readiness",
        ],
        dependencies=[
            StageDependency(from_stage="requirements_analysis", to_stage="architecture_design"),
            StageDependency(from_stage="architecture_design",   to_stage="task_decomposition"),
            # Parallel fan-out
            StageDependency(from_stage="task_decomposition",    to_stage="implementation_planning"),
            StageDependency(from_stage="task_decomposition",    to_stage="testing_planning"),
            StageDependency(from_stage="task_decomposition",    to_stage="documentation_planning"),
            # Synchronisation point
            StageDependency(from_stage="implementation_planning", to_stage="validation"),
            StageDependency(from_stage="testing_planning",        to_stage="validation"),
            StageDependency(from_stage="documentation_planning",  to_stage="validation"),
            # Final gate
            StageDependency(from_stage="validation", to_stage="release_readiness"),
        ],
    )


def create_greenfield_stages() -> dict[str, BaseStage]:
    """Instantiate all SDLC stage implementations for the greenfield scenario."""
    return {
        "requirements_analysis":  RequirementsAnalysisStage(),
        "architecture_design":    ArchitectureDesignStage(),
        "task_decomposition":     TaskDecompositionStage(),
        "implementation_planning": ImplementationPlanningStage(),
        "testing_planning":       TestingPlanningStage(),
        "documentation_planning": DocumentationPlanningStage(),
        "validation":             ValidationStage(),
        "release_readiness":      ReleaseReadinessStage(),
    }


async def run_greenfield_scenario(
    raw_requirement: str = DEFAULT_REQUIREMENT_TEXT,
    approval_gateway: ApprovalGateway | None = None,
    policy_engine: PolicyEngine | None = None,
    final_approval_required: bool = False,
) -> "WorkflowState":  # type: ignore[name-defined]
    """
    Run the full greenfield URL Shortener SDLC scenario.

    Args:
        raw_requirement:        Raw requirement text (defaults to DEFAULT_REQUIREMENT_TEXT).
        approval_gateway:       ApprovalGateway implementation for human-approval stages.
                                Defaults to AutoApproveGateway (suitable for CI / testing).
        policy_engine:          Optional PolicyEngine to enforce governance guardrails.
        final_approval_required: Whether to require a final human QC review at the end.

    Returns:
        WorkflowState after the pipeline completes. Check state.status for the outcome.

    Example::

        from orchestrator.core.autonomy import AutoApproveGateway
        state = await run_greenfield_scenario(approval_gateway=AutoApproveGateway())
        assert state.status == WorkflowStatus.COMPLETED
    """
    from orchestrator.core.autonomy import AutoApproveGateway

    requirement = create_greenfield_requirement(raw_requirement)
    definition = create_greenfield_workflow()
    stages = create_greenfield_stages()

    engine = WorkflowEngine(
        definition=definition,
        stages=stages,
        approval_gateway=approval_gateway or AutoApproveGateway(),
        policy_engine=policy_engine,
        final_approval_required=final_approval_required,
    )

    return await engine.run(requirement)
