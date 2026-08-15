"""
Core domain models for the Agentic SDLC orchestration layer.

This module defines the *state* types that track the progress of a workflow:
  Requirement → Tasks → StageContexts → WorkflowState

It is distinct from:
  results.py  — execution *outputs* (Artifact, Decision, Risk, Approval, ...)
  graph.py    — workflow *topology* (WorkflowDefinition, StageDependency)
  context.py  — cross-stage *context propagation* (ExecutionContext)

All four modules compose into a complete orchestration domain model.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from orchestrator.core.results import (
    Approval,
    Artifact,
    ArtifactType,
    Decision,
    ExecutionResult,
    Risk,
    ValidationResult,
)

if TYPE_CHECKING:
    # Imported for type annotations only — avoids coupling at runtime
    from orchestrator.core.context import ExecutionContext
    from orchestrator.core.graph import WorkflowDefinition


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ─── Requirement ──────────────────────────────────────────────────────────────


class RequirementType(str, enum.Enum):
    GREENFIELD = "greenfield"    # New system / feature built from scratch
    BROWNFIELD = "brownfield"    # Enhancement or refactor of existing code
    AMBIGUOUS = "ambiguous"      # Unclear intent; needs clarification before execution


class AmbiguityItem(BaseModel):
    """A single identified ambiguity within a raw requirement."""

    field: str = Field(description="Which aspect of the requirement is ambiguous")
    description: str = Field(description="Why this is ambiguous")
    resolved: bool = False
    resolution: str | None = None
    resolved_at: datetime | None = None


class Requirement(BaseModel):
    """
    Normalized representation of an incoming engineering requirement.

    The orchestrator transforms raw user text into this structured form
    before any execution begins (requirement understanding stage).
    """

    id: str = Field(default_factory=_uuid)
    title: str
    raw_text: str = Field(description="Original, unmodified requirement text")
    requirement_type: RequirementType
    normalized_text: str | None = Field(
        default=None,
        description="Clarified, unambiguous restatement produced by the requirements stage",
    )
    ambiguities: list[AmbiguityItem] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)

    @property
    def is_fully_resolved(self) -> bool:
        """True when every identified ambiguity has been resolved."""
        return all(a.resolved for a in self.ambiguities)


# ─── Task ─────────────────────────────────────────────────────────────────────


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class Task(BaseModel):
    """
    Atomic unit of work decomposed from a Requirement.

    Tasks form a dependency graph within a stage; the orchestrator
    respects the `depends_on` list when scheduling execution.
    """

    id: str = Field(default_factory=_uuid)
    title: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(
        default_factory=list,
        description="IDs of tasks that must complete before this one starts",
    )
    assigned_agent: str | None = Field(
        default=None,
        description="Name of the agent responsible for executing this task",
    )
    expected_artifact_types: list[ArtifactType] = Field(
        default_factory=list,
        description="Artifact types this task is expected to produce",
    )
    artifacts: dict[str, Any] = Field(
        default_factory=dict,
        description="Legacy untyped artifact bag (use execution_result.artifacts for new code)",
    )
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


# ─── Stage definition ─────────────────────────────────────────────────────────


class StageDefinition(BaseModel):
    """
    Immutable metadata describing a stage's configuration.

    StageDefinition is the *what* (name, capabilities, constraints).
    StageContext is the *runtime state* (status, results, artifacts).
    A WorkflowDefinition holds StageDefinitions as node attributes;
    the orchestrator creates one StageContext per StageDefinition per run.
    """

    stage_name: str
    description: str
    requires_approval: bool = Field(
        default=False,
        description="When True, a human must approve before the stage executes",
    )
    expected_artifact_types: list[ArtifactType] = Field(
        default_factory=list,
        description="Artifact types this stage is expected to produce (for exit-gate validation)",
    )
    timeout_seconds: int | None = Field(
        default=None,
        description="Maximum wall-clock time for this stage (None = no limit)",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


# ─── Gate result ──────────────────────────────────────────────────────────────


class GateResult(BaseModel):
    """
    Pass/fail outcome of evaluating an entry or exit gate on a stage.

    For richer detail (rule name, severity, evidence), see ValidationResult
    in results.py. GateResult is the high-level summary; ValidationResults
    are the per-rule details that compose it.
    """

    passed: bool
    gate_name: str
    reason: str | None = None
    evaluated_at: datetime = Field(default_factory=_now)


# ─── Stage context (runtime state) ────────────────────────────────────────────


class StageStatus(str, enum.Enum):
    PENDING = "pending"
    AWAITING_GATE = "awaiting_gate"          # Entry gate evaluation in progress
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"  # Blocked at human checkpoint
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"
    BLOCKED = "blocked"                      # Never ran; a dependency failed first


class StageContext(BaseModel):
    """
    All runtime state for a single SDLC stage execution.

    One StageContext per stage per workflow run. Persisted after every
    status transition so that rollback, resume, and audit replay
    all have a complete record to work from.

    The `execution_results` list records the typed output of each agent
    task run. `artifacts`, `decisions`, `risks`, and `validations` are
    the aggregated typed outputs accumulated from all execution results
    within this stage — they are the canonical source for the exit gate
    and for propagation into ExecutionContext.
    """

    stage_name: str
    status: StageStatus = StageStatus.PENDING
    attempt: int = Field(default=0, description="Current attempt number (0-indexed)")
    max_attempts: int = Field(default=3, description="Bounded retry limit")

    # ── Data I/O ──────────────────────────────────────────────────────────────
    input_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Snapshot of ExecutionContext passed into this stage",
    )
    output_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured key-value outputs to be merged back into ExecutionContext",
    )

    # ── Gate tracking ─────────────────────────────────────────────────────────
    entry_gate_results: list[GateResult] = Field(default_factory=list)
    exit_gate_results: list[GateResult] = Field(default_factory=list)

    # ── Task graph ────────────────────────────────────────────────────────────
    tasks: list[Task] = Field(default_factory=list)

    # ── Typed execution outputs (new in Step 3) ───────────────────────────────
    execution_results: list[ExecutionResult] = Field(
        default_factory=list,
        description="One per task/agent run. Source of truth for all other typed outputs.",
    )
    artifacts: list[Artifact] = Field(
        default_factory=list,
        description="Typed artifacts produced during this stage",
    )
    decisions: list[Decision] = Field(
        default_factory=list,
        description="Decisions made during this stage (with lineage)",
    )
    risks: list[Risk] = Field(
        default_factory=list,
        description="Risks identified during this stage",
    )
    validations: list[ValidationResult] = Field(
        default_factory=list,
        description="Per-rule validation results from gate evaluations",
    )

    # ── Timing and error ──────────────────────────────────────────────────────
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    rollback_performed: bool = False

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def has_retries_remaining(self) -> bool:
        return self.attempt < self.max_attempts

    @property
    def entry_passed(self) -> bool:
        return bool(self.entry_gate_results) and all(
            g.passed for g in self.entry_gate_results
        )

    @property
    def exit_passed(self) -> bool:
        return bool(self.exit_gate_results) and all(
            g.passed for g in self.exit_gate_results
        )

    @property
    def has_blocking_validation_failures(self) -> bool:
        """True if any validation result is blocking (ERROR/CRITICAL and failed)."""
        return any(v.is_blocking for v in self.validations)

    @property
    def ready_tasks(self) -> list[Task]:
        """Tasks whose dependencies are all complete and that are still pending."""
        completed_ids = {
            t.id for t in self.tasks if t.status == TaskStatus.COMPLETED
        }
        return [
            t for t in self.tasks
            if t.status == TaskStatus.PENDING
            and set(t.depends_on) <= completed_ids
        ]

    # ── Mutation helpers ──────────────────────────────────────────────────────

    def add_execution_result(self, result: ExecutionResult) -> None:
        """
        Record an agent execution result and aggregate its typed outputs.

        Artifacts, decisions, and risks from the result are flattened into
        the stage-level lists so gate evaluators don't need to walk nested
        structures.
        """
        self.execution_results.append(result)
        self.artifacts.extend(result.artifacts)
        self.decisions.extend(result.decisions)
        self.risks.extend(result.risks)
        self.validations.extend(result.validations)


# ─── Audit ────────────────────────────────────────────────────────────────────


class AuditEntry(BaseModel):
    """
    Immutable record of every orchestration event.

    Provides audit-grade traceability: every gate evaluation, retry,
    approval decision, safe-stop, and state transition is logged here.
    AuditEntry is append-only — never modified, never deleted.
    """

    timestamp: datetime = Field(default_factory=_now)
    event: str = Field(description="Machine-readable event name, e.g. 'stage_started'")
    stage: str | None = None
    actor: str = Field(default="orchestrator", description="Who triggered this event")
    details: dict[str, Any] = Field(default_factory=dict)


# ─── Workflow state ───────────────────────────────────────────────────────────


class WorkflowStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    STOPPED = "stopped"          # Operator-initiated safe stop


class WorkflowState(BaseModel):
    """
    Top-level runtime state object for a full SDLC lifecycle run.

    One WorkflowState per requirement. Persisted to disk/DB so that
    pause, resume, rollback, and audit replay all work across restarts.

    The optional `workflow_definition` field links runtime state to the
    DAG template that drives it. The optional `execution_context` field
    holds cross-stage artifacts, decisions, and risks.
    """

    id: str = Field(default_factory=_uuid)
    requirement: Requirement
    status: WorkflowStatus = WorkflowStatus.PENDING

    # ── Stage runtime state ───────────────────────────────────────────────────
    stages: dict[str, StageContext] = Field(
        default_factory=dict,
        description="stage_name → StageContext; preserves full execution history",
    )
    current_stage: str | None = None

    # ── Approvals ─────────────────────────────────────────────────────────────
    approvals: list[Approval] = Field(
        default_factory=list,
        description="All human approval records for this workflow run",
    )

    # ── Audit trail ───────────────────────────────────────────────────────────
    audit_trail: list[AuditEntry] = Field(default_factory=list)

    # ── Optional links to definition and context ──────────────────────────────
    # TYPE_CHECKING guard avoids circular imports at runtime while preserving
    # IDE type inference. Pydantic uses Any at runtime for these fields.
    workflow_definition: Any | None = Field(
        default=None,
        description="WorkflowDefinition (DAG template) driving this run",
    )
    execution_context: Any | None = Field(
        default=None,
        description="ExecutionContext accumulating cross-stage outputs",
    )

    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # ── Stage mutation helpers ────────────────────────────────────────────────

    def add_audit_entry(
        self,
        event: str,
        stage: str | None = None,
        actor: str = "orchestrator",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append an immutable audit record and bump updated_at."""
        self.audit_trail.append(
            AuditEntry(
                event=event,
                stage=stage,
                actor=actor,
                details=details or {},
            )
        )
        self.updated_at = _now()

    def set_stage(self, context: StageContext) -> None:
        """Upsert a stage context and update the current_stage pointer."""
        self.stages[context.stage_name] = context
        self.current_stage = context.stage_name
        self.updated_at = _now()

    def get_stage(self, stage_name: str) -> StageContext | None:
        return self.stages.get(stage_name)

    def add_approval(self, approval: Approval) -> None:
        """Record a new approval request or decision."""
        self.approvals.append(approval)
        self.updated_at = _now()

    # ── State queries ─────────────────────────────────────────────────────────

    @property
    def completed_stage_names(self) -> set[str]:
        """Names of stages that have completed successfully."""
        return {
            name
            for name, ctx in self.stages.items()
            if ctx.status == StageStatus.COMPLETED
        }

    @property
    def failed_stage_names(self) -> set[str]:
        """Names of stages that failed."""
        return {
            name
            for name, ctx in self.stages.items()
            if ctx.status == StageStatus.FAILED
        }

    @property
    def all_artifacts(self) -> list[Artifact]:
        """Flattened list of all artifacts produced across all stages."""
        result: list[Artifact] = []
        for ctx in self.stages.values():
            result.extend(ctx.artifacts)
        return result

    @property
    def all_decisions(self) -> list[Decision]:
        """Flattened list of all decisions made across all stages, in order."""
        result: list[Decision] = []
        for ctx in self.stages.values():
            result.extend(ctx.decisions)
        return result

    @property
    def all_risks(self) -> list[Risk]:
        """Flattened list of all risks identified across all stages."""
        result: list[Risk] = []
        for ctx in self.stages.values():
            result.extend(ctx.risks)
        return result

    @property
    def pending_approvals(self) -> list[Approval]:
        """Approval records that have not yet been resolved."""
        from orchestrator.core.results import ApprovalStatus
        return [a for a in self.approvals if a.status == ApprovalStatus.PENDING]
