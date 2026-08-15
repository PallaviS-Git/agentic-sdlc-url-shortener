"""
Execution output domain types for the Agentic SDLC orchestration layer.

These types are the structured outputs that agents and stages produce.
They are distinct from execution *state* (StageContext, WorkflowState) —
state describes where execution is; results describe what it produced.

Import chain: this module imports only stdlib + pydantic.
Higher-level modules (models.py, context.py) import from here.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ─── Artifact ─────────────────────────────────────────────────────────────────


class ArtifactType(str, enum.Enum):
    """Classification of artifacts produced during SDLC execution."""

    CODE = "code"                   # Source code files
    SCHEMA = "schema"               # API/DB schemas (OpenAPI, SQL, etc.)
    TEST = "test"                   # Test files and coverage reports
    DOCUMENTATION = "documentation" # Docs, READMEs, architecture records
    CONFIG = "config"               # Configuration files
    REPORT = "report"               # Analysis/audit/summary reports
    MIGRATION = "migration"         # Database migration scripts


class ArtifactStatus(str, enum.Enum):
    DRAFT = "draft"           # Produced but not yet validated
    VALIDATED = "validated"   # Passed exit-gate validation
    REJECTED = "rejected"     # Failed validation; must be regenerated


class Artifact(BaseModel):
    """
    A typed, named output produced by an agent during a stage.

    Artifacts are the *deliverables* of SDLC execution: code files, API
    schemas, test suites, migration scripts, documentation. They are
    tracked from production through validation and collected in
    ExecutionContext so downstream stages can consume them.
    """

    id: str = Field(default_factory=_uuid)
    name: str = Field(description="Human-readable name, e.g. 'url_service.py'")
    artifact_type: ArtifactType
    produced_by_stage: str = Field(description="Stage that created this artifact")
    produced_by_agent: str | None = Field(
        default=None,
        description="Agent that created this artifact (None if produced by the stage itself)",
    )
    content: str | None = Field(
        default=None,
        description="Text content of the artifact (for in-memory artifacts)",
    )
    path: str | None = Field(
        default=None,
        description="File system path (for persisted artifacts)",
    )
    status: ArtifactStatus = ArtifactStatus.DRAFT
    created_at: datetime = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary metadata (e.g. language, line count, test coverage %)",
    )

    @property
    def is_available(self) -> bool:
        """True if the artifact has content or a resolvable path."""
        return bool(self.content or self.path)

    @property
    def is_validated(self) -> bool:
        return self.status == ArtifactStatus.VALIDATED


# ─── ExecutionResult ──────────────────────────────────────────────────────────


class ExecutionStatus(str, enum.Enum):
    """Outcome of a single task/agent execution attempt."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"  # Completed with warnings or incomplete output


class ExecutionResult(BaseModel):
    """
    Typed outcome of a single agent task execution.

    One ExecutionResult is recorded per task per attempt. The orchestrator
    uses these records for audit traceability and to decide whether to
    promote to exit-gate evaluation or trigger retry logic.
    """

    id: str = Field(default_factory=_uuid)
    task_id: str = Field(description="ID of the Task that was executed")
    agent_name: str = Field(description="Name of the agent that ran the task")
    status: ExecutionStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    output: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured key-value output from the agent",
    )
    started_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    error: str | None = None

    def mark_complete(self, status: ExecutionStatus, error: str | None = None) -> None:
        """Record completion timestamp and duration."""
        self.completed_at = _now()
        self.status = status
        self.error = error
        if self.completed_at and self.started_at:
            delta = self.completed_at - self.started_at
            self.duration_ms = int(delta.total_seconds() * 1000)

    @property
    def succeeded(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS

    @property
    def failed(self) -> bool:
        return self.status == ExecutionStatus.FAILURE


# ─── ValidationResult ─────────────────────────────────────────────────────────


class ValidationSeverity(str, enum.Enum):
    """Severity tier of a validation finding."""

    INFO = "info"         # Informational; does not block
    WARNING = "warning"   # Notable; does not block alone but accumulates
    ERROR = "error"       # Fails the gate if any ERROR rule fails
    CRITICAL = "critical" # Fails the gate immediately; may trigger rollback


class ValidationResult(BaseModel):
    """
    Structured outcome of evaluating a single validation rule.

    More expressive than GateResult: includes the rule name, severity,
    a human-readable message, and evidence (key-value data for audit).

    Multiple ValidationResults are aggregated to produce a GateResult.
    The gate fails if any result with severity >= ERROR is not passed.
    """

    rule_name: str = Field(description="Machine-readable rule identifier")
    passed: bool
    severity: ValidationSeverity = ValidationSeverity.ERROR
    message: str = Field(description="Human-readable finding description")
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Supporting data (e.g. coverage %, line counts, policy keys)",
    )
    validated_at: datetime = Field(default_factory=_now)

    @property
    def is_blocking(self) -> bool:
        """True if this result alone can block stage completion."""
        return not self.passed and self.severity in (
            ValidationSeverity.ERROR,
            ValidationSeverity.CRITICAL,
        )


# ─── Risk ─────────────────────────────────────────────────────────────────────


class RiskSeverity(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Risk(BaseModel):
    """
    An identified risk associated with a stage or its outputs.

    Risks surface from agent analysis and gate evaluation. They are
    collected in ExecutionContext and surfaced in the final engineering
    summary. Risks must either be mitigated or explicitly accepted.
    """

    id: str = Field(default_factory=_uuid)
    title: str
    description: str
    severity: RiskSeverity
    stage: str = Field(description="Stage that identified this risk")
    category: str = Field(
        default="general",
        description="Risk category (security, performance, maintainability, ...)",
    )
    mitigation: str | None = Field(
        default=None,
        description="Proposed mitigation strategy",
    )
    accepted: bool = Field(
        default=False,
        description="True if a human or policy explicitly accepted this risk",
    )
    identified_at: datetime = Field(default_factory=_now)


# ─── Decision ─────────────────────────────────────────────────────────────────


class DecisionType(str, enum.Enum):
    """Classification of decisions made during SDLC execution."""

    ARCHITECTURAL = "architectural"   # Design / technology choices
    IMPLEMENTATION = "implementation" # How something is built
    SECURITY = "security"             # Security-related choices
    TRADE_OFF = "trade_off"           # Explicit cost/benefit compromise
    SCOPE = "scope"                   # What is/isn't included


class Decision(BaseModel):
    """
    A recorded choice made during orchestration, with full lineage.

    Decisions are first-class domain objects because they are the primary
    evidence for engineering defensibility. The `parent_decision_id` field
    enables lineage tracing: "Decision D was made because of Decision B,
    which was made because of Decision A."

    Decision lineage is critical for understanding *why* the orchestrator
    took a specific path, especially when reviewing the final artifact.
    """

    id: str = Field(default_factory=_uuid)
    decision_type: DecisionType
    title: str
    description: str
    rationale: str = Field(
        description="The reasoning behind this choice. Must be non-empty.",
    )
    alternatives_considered: list[str] = Field(
        default_factory=list,
        description="Other options that were evaluated and rejected",
    )
    stage: str = Field(description="Stage in which this decision was made")
    made_by: str = Field(
        default="orchestrator",
        description="Agent name or 'human' indicating who made this decision",
    )
    made_at: datetime = Field(default_factory=_now)
    parent_decision_id: str | None = Field(
        default=None,
        description="ID of the upstream decision that led to this one (for lineage)",
    )
    downstream_impacts: list[str] = Field(
        default_factory=list,
        description="Stages or components whose behaviour this decision affects",
    )


# ─── Approval ─────────────────────────────────────────────────────────────────


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"       # Awaiting human decision
    APPROVED = "approved"     # Human explicitly approved
    REJECTED = "rejected"     # Human explicitly rejected
    TIMED_OUT = "timed_out"   # No decision before deadline → treated as rejection


class Approval(BaseModel):
    """
    A human approval checkpoint record.

    The orchestrator creates an Approval when a stage with
    `requires_approval=True` is about to execute. Execution is blocked
    until the approval status transitions from PENDING.

    Approval records are immutable audit evidence: even a TIMED_OUT
    record is retained so reviewers can see that the checkpoint fired.
    """

    id: str = Field(default_factory=_uuid)
    workflow_id: str
    stage_name: str
    requested_by: str = Field(
        default="orchestrator",
        description="Agent or system that triggered the approval request",
    )
    summary: str = Field(description="Plain-text description of what is being approved")
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Supporting information for the approver (artifacts, risks, decisions)",
    )
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver: str | None = Field(
        default=None,
        description="Identity of the human who made the decision",
    )
    notes: str | None = Field(
        default=None,
        description="Optional notes from the approver",
    )
    requested_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None

    @property
    def is_resolved(self) -> bool:
        return self.status != ApprovalStatus.PENDING

    @property
    def was_approved(self) -> bool:
        return self.status == ApprovalStatus.APPROVED
