"""
Orchestration observability — structured logs, execution trace, and reliability metrics.

Design principle
────────────────
"Do not introduce unnecessary observability infrastructure if the prototype
can demonstrate the behavior with a clean abstraction."

No external frameworks (structlog, OpenTelemetry, Prometheus, etc.) are used.
Everything is computed from the rich state already captured in WorkflowState,
StageContext, AuditEntry, and WorkflowLineage.

All output types are Pydantic models, so they are JSON-serializable out of the
box — pipe them to any log aggregator, metrics backend, or SIEM via
``report.as_dict()``.

Public API
──────────
  build_structured_logs(state)       → list[StructuredLogRecord]
  build_execution_trace(state)       → ExecutionTrace
  compute_workflow_metrics(state)    → WorkflowMetrics
  compute_reliability_metrics(states)→ ReliabilityMetrics
  build_observability_report(state)  → WorkflowObservabilityReport

Trace reconstruction
────────────────────
The execution trace reconstructs the full provenance chain::

    Requirement → Decision → Task → Agent → Artifact → Validation → Approval → Result

Each step carries:
  • Unique ID (workflow_id, stage_id, task_id, artifact_id, …)
  • Timestamp
  • Links to predecessor step IDs (so the chain is navigable)
  • Structured details (JSON-serializable dict)

Import chain: orchestrator.core.{lineage, models, results} + stdlib.
"""
from __future__ import annotations

import enum
import statistics
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from orchestrator.core.lineage import build_lineage
from orchestrator.core.models import (
    StageStatus,
    WorkflowState,
    WorkflowStatus,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ─── Log levels ───────────────────────────────────────────────────────────────

_ERROR_EVENTS: frozenset[str] = frozenset(
    {
        "stage_execution_failed",
        "exit_gate_failed",
        "exit_gate_exception",
        "entry_gate_failed",
        "entry_gate_exception",
        "policy_blocked",
        "approval_rejected",
        "safe_stop_triggered",
        "rollback_failed",
        "stage_failed_all_attempts",
        "critical_action_blocked",
        "workflow_failed",
        "workflow_safe_stopped",
        "approval_timed_out",
        "final_qc_rejected",
    }
)

_WARN_EVENTS: frozenset[str] = frozenset(
    {
        "stage_retrying",
        "exit_gate_retrying",
        "stage_skipped_fallback",
        "stage_fallback_applied",
        "rollback_started",
        "rollback_completed",
        "rollback_failed",
        "approval_missing_gateway",
    }
)


def _log_level(event: str) -> str:
    if event in _ERROR_EVENTS:
        return "ERROR"
    if event in _WARN_EVENTS:
        return "WARN"
    return "INFO"


# ─── Structured log record ────────────────────────────────────────────────────


class StructuredLogRecord(BaseModel):
    """
    Single JSON-serializable log line enriched with all correlation IDs.

    Engineers feed these into any log aggregator (ELK, Splunk, CloudWatch, …)
    and filter on ``workflow_id`` to reconstruct the full execution history.
    """

    timestamp: datetime
    level: str = Field(description="INFO | WARN | ERROR")
    event: str = Field(description="Machine-readable event name (from AuditEntry.event)")
    workflow_id: str = Field(description="Correlation ID — links every log line to one run")
    stage_name: str | None = None
    stage_id: str | None = Field(
        default=None,
        description="Unique stage execution ID from StageContext.stage_id",
    )
    actor: str = "orchestrator"
    details: dict[str, Any] = Field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a flat, JSON-serializable representation."""
        return self.model_dump(mode="json")


# ─── Execution trace ──────────────────────────────────────────────────────────


class TraceStepKind(str, enum.Enum):
    """The kind of entity represented by a single step in the execution trace."""

    REQUIREMENT = "requirement"
    DECISION = "decision"
    TASK = "task"
    AGENT = "agent"
    ARTIFACT = "artifact"
    VALIDATION = "validation"
    APPROVAL = "approval"
    RESULT = "result"


class TraceStep(BaseModel):
    """
    One node in the execution trace graph.

    ``links`` contains the IDs of predecessor steps so the full
    Requirement → … → Result chain is navigable without additional queries.
    """

    kind: TraceStepKind
    id: str
    name: str
    timestamp: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    links: list[str] = Field(
        default_factory=list,
        description="IDs of predecessor steps (requirement, decision, task, …)",
    )


class ExecutionTrace(BaseModel):
    """
    Full provenance chain for one workflow run.

    Steps are grouped by kind for easy inspection.  Call ``all_steps()``
    to get a flat list ordered from root (Requirement) to leaf (Result).
    """

    workflow_id: str
    workflow_status: str
    created_at: datetime
    completed_at: datetime | None = None

    # Ordered groups — use all_steps() for the full flat chain.
    requirement: TraceStep | None = None
    decisions: list[TraceStep] = Field(default_factory=list)
    tasks: list[TraceStep] = Field(default_factory=list)
    agents: list[TraceStep] = Field(default_factory=list)
    artifacts: list[TraceStep] = Field(default_factory=list)
    validations: list[TraceStep] = Field(default_factory=list)
    approvals: list[TraceStep] = Field(default_factory=list)
    result: TraceStep | None = None

    def all_steps(self) -> list[TraceStep]:
        """
        Return every step as a flat list, grouped by kind, from
        REQUIREMENT → DECISION → TASK → AGENT → ARTIFACT → VALIDATION
        → APPROVAL → RESULT.
        """
        steps: list[TraceStep] = []
        if self.requirement:
            steps.append(self.requirement)
        steps.extend(self.decisions)
        steps.extend(self.tasks)
        steps.extend(self.agents)
        steps.extend(self.artifacts)
        steps.extend(self.validations)
        steps.extend(self.approvals)
        if self.result:
            steps.append(self.result)
        return steps

    def step_ids_by_kind(self, kind: TraceStepKind) -> list[str]:
        """Return all IDs for steps of a given kind."""
        return [s.id for s in self.all_steps() if s.kind == kind]


# ─── Metrics ──────────────────────────────────────────────────────────────────


class StageMetrics(BaseModel):
    """Timing and outcome data for a single stage execution."""

    stage_name: str
    stage_id: str | None = None
    status: str
    latency_seconds: float | None = Field(
        default=None,
        description="Wall-clock time from stage start to completion (None if not completed)",
    )
    attempt_count: int = Field(
        default=1,
        description="Total execution attempts (1 = no retries, N = N-1 retries)",
    )
    retried: bool = False
    rolled_back: bool = False
    fallback_used: bool = False


class WorkflowMetrics(BaseModel):
    """
    Reliability metrics for a single workflow run.

    All timing values are in seconds.  Use ``compute_workflow_metrics()``
    to build this from a ``WorkflowState``.
    """

    workflow_id: str
    status: str
    succeeded: bool
    total_latency_seconds: float | None = Field(
        default=None,
        description=(
            "End-to-end wall-clock time from WorkflowState.created_at to "
            "WorkflowState.completed_at. None when the workflow did not finish."
        ),
    )
    stage_metrics: list[StageMetrics] = Field(default_factory=list)
    total_retries: int = 0
    total_rollbacks: int = 0
    total_policy_evaluations: int = 0
    total_approvals: int = 0
    mttr_seconds: float | None = Field(
        default=None,
        description=(
            "Mean time-to-recovery for stages that retried and eventually "
            "succeeded. Measured from the first failure to stage completion. "
            "None when no retried stages recovered in this run."
        ),
    )


class ReliabilityMetrics(BaseModel):
    """
    Cross-run reliability metrics computed from multiple workflow executions.

    Pass a list of ``WorkflowState`` objects to ``compute_reliability_metrics()``
    to produce this summary — one per deployment cycle, sprint, or time window.
    """

    total_runs: int
    successful_runs: int
    failed_runs: int
    success_rate: float = Field(description="0.0 – 1.0")
    failure_rate: float = Field(description="0.0 – 1.0")

    total_retries: int
    retry_frequency: float = Field(description="Mean retries per workflow run")
    total_rollbacks: int
    rollback_frequency: float = Field(description="Mean rollbacks per workflow run")

    mean_e2e_latency_seconds: float | None = Field(
        default=None,
        description="Mean end-to-end latency across all runs that recorded completed_at",
    )
    mean_stage_latency_seconds: float | None = Field(
        default=None,
        description="Mean per-stage execution latency across all runs and stages",
    )
    mttr_seconds: float | None = Field(
        default=None,
        description=(
            "Mean time-to-recovery across all retried+recovered stages "
            "in all runs. None when no retried stages recovered."
        ),
    )


# ─── Observability report ─────────────────────────────────────────────────────


class WorkflowObservabilityReport(BaseModel):
    """
    Single artifact that captures all observability data for one workflow run.

    Contains:
      • ``execution_trace``  — full Requirement → Result provenance chain
      • ``metrics``          — latency, retries, rollbacks, MTTR for this run
      • ``structured_logs``  — every audit event as a log-aggregator-friendly record

    Call ``as_dict()`` to get a JSON-serializable representation suitable
    for storage, shipping to a SIEM, or serving via an API endpoint.
    """

    workflow_id: str
    report_generated_at: datetime = Field(default_factory=_now)
    execution_trace: ExecutionTrace
    metrics: WorkflowMetrics
    structured_logs: list[StructuredLogRecord]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (all datetimes as ISO-8601 strings)."""
        return self.model_dump(mode="json")

    def failure_trace(self) -> list[StructuredLogRecord]:
        """Return only ERROR-level log records — the failure trace."""
        return [r for r in self.structured_logs if r.level == "ERROR"]

    def decision_trace(self) -> list[TraceStep]:
        """Return all Decision steps from the execution trace."""
        return self.execution_trace.decisions

    def approval_trace(self) -> list[TraceStep]:
        """Return all Approval steps from the execution trace."""
        return self.execution_trace.approvals

    def policy_trace(self) -> list[StructuredLogRecord]:
        """Return all policy-evaluation log records."""
        return [
            r for r in self.structured_logs if r.event in {"policy_evaluated", "policy_blocked"}
        ]

    def artifact_trace(self) -> list[TraceStep]:
        """Return all Artifact steps from the execution trace."""
        return self.execution_trace.artifacts


# ─── Builder functions ────────────────────────────────────────────────────────


def build_structured_logs(state: WorkflowState) -> list[StructuredLogRecord]:
    """
    Convert every AuditEntry in WorkflowState into a StructuredLogRecord.

    Each record is enriched with:
      • ``workflow_id``   — correlation ID for the full run
      • ``stage_id``      — unique execution ID from StageContext.stage_id
      • ``level``         — INFO / WARN / ERROR based on the event name
    """
    records: list[StructuredLogRecord] = []
    for entry in state.audit_trail:
        stage_id: str | None = None
        if entry.stage and entry.stage in state.stages:
            stage_id = state.stages[entry.stage].stage_id

        records.append(
            StructuredLogRecord(
                timestamp=entry.timestamp,
                level=_log_level(entry.event),
                event=entry.event,
                workflow_id=state.id,
                stage_name=entry.stage,
                stage_id=stage_id,
                actor=entry.actor,
                details=entry.details,
            )
        )
    return records


def build_execution_trace(state: WorkflowState) -> ExecutionTrace:
    """
    Build the full Requirement → Decision → Task → Agent → Artifact
    → Validation → Approval → Result provenance chain from WorkflowState.

    Uses WorkflowLineage internally; does not require any additional
    database lookups.
    """
    lineage = build_lineage(state)
    req = state.requirement

    # ── Requirement ────────────────────────────────────────────────────────
    req_step = TraceStep(
        kind=TraceStepKind.REQUIREMENT,
        id=req.id,
        name=req.title,
        timestamp=state.created_at,
        details={
            "type": req.requirement_type.value,
            "text": req.raw_text[:300],
        },
        links=[],
    )

    # ── Decisions ──────────────────────────────────────────────────────────
    decision_steps: list[TraceStep] = []
    for dec in lineage.decisions:
        decision_steps.append(
            TraceStep(
                kind=TraceStepKind.DECISION,
                id=dec.id,
                name=(dec.rationale[:80] if dec.rationale else dec.decision_type.value),
                timestamp=dec.made_at,
                details={
                    "decision_type": dec.decision_type.value,
                    "rationale": dec.rationale,
                    "stage": dec.stage if hasattr(dec, "stage") else None,
                },
                links=[req.id],
            )
        )

    # ── Tasks ──────────────────────────────────────────────────────────────
    task_steps: list[TraceStep] = []
    for task in lineage.tasks:
        predecessor = task.created_by_decision_id or req.id
        task_steps.append(
            TraceStep(
                kind=TraceStepKind.TASK,
                id=task.id,
                name=task.title,
                timestamp=task.started_at,
                details={
                    "stage": task.stage,
                    "rationale": task.rationale,
                    "assigned_agent": task.assigned_agent,
                    "agent_execution_id": task.agent_execution_id,
                    "status": task.status.value,
                },
                links=[predecessor],
            )
        )

    # ── Agents (one step per unique agent × stage combination) ─────────────
    agent_steps: list[TraceStep] = []
    seen_agents: set[tuple[str, str]] = set()
    for task in lineage.tasks:
        agent_name = task.assigned_agent or "unassigned"
        stage = task.stage or "unknown"
        key = (agent_name, stage)
        if key not in seen_agents:
            seen_agents.add(key)
            agent_steps.append(
                TraceStep(
                    kind=TraceStepKind.AGENT,
                    id=f"agent:{agent_name}:{stage}",
                    name=agent_name,
                    timestamp=task.started_at,
                    details={"stage": stage, "agent": agent_name},
                    links=[task.id],
                )
            )

    # ── Artifacts ──────────────────────────────────────────────────────────
    artifact_steps: list[TraceStep] = []
    for artifact in lineage.artifacts:
        # Link to the task or stage that produced it
        producer_task_ids = [
            t.id for t in lineage.tasks
            if t.stage == artifact.produced_by_stage
        ]
        links = producer_task_ids if producer_task_ids else [artifact.produced_by_stage]
        artifact_steps.append(
            TraceStep(
                kind=TraceStepKind.ARTIFACT,
                id=artifact.id,
                name=artifact.name,
                timestamp=artifact.created_at,
                details={
                    "artifact_type": artifact.artifact_type.value,
                    "stage": artifact.produced_by_stage,
                    "path": artifact.path,
                    "content_preview": (artifact.content or "")[:100] if artifact.content else None,
                },
                links=links,
            )
        )

    # ── Validations ────────────────────────────────────────────────────────
    validation_steps: list[TraceStep] = []
    for val in lineage.validations:
        validation_steps.append(
            TraceStep(
                kind=TraceStepKind.VALIDATION,
                id=f"validation:{val.rule_name}:{val.stage}:{val.validated_at.isoformat()}",
                name=val.rule_name,
                timestamp=val.validated_at,
                details={
                    "passed": val.passed,
                    "stage": val.stage,
                    "severity": val.severity.value,
                    "message": val.message,
                    "evidence": val.evidence,
                },
                links=[val.stage] if val.stage else [],
            )
        )

    # ── Approvals ──────────────────────────────────────────────────────────
    approval_steps: list[TraceStep] = []
    for appr in lineage.approvals:
        approval_steps.append(
            TraceStep(
                kind=TraceStepKind.APPROVAL,
                id=appr.id,
                name=f"Approval: {appr.stage_name}",
                timestamp=appr.requested_at,
                details={
                    "stage": appr.stage_name,
                    "status": appr.status.value,
                    "approver": appr.approver,
                    "rationale": appr.decision_rationale,
                    "is_override": appr.is_override,
                },
                links=[appr.stage_name],
            )
        )

    # ── Result (terminal leaf) ──────────────────────────────────────────────
    failed_stages = [
        name
        for name, ctx in state.stages.items()
        if ctx.status == StageStatus.FAILED
    ]
    result_step = TraceStep(
        kind=TraceStepKind.RESULT,
        id=f"result:{state.id}",
        name=state.status.value,
        timestamp=state.completed_at,
        details={
            "status": state.status.value,
            "stage_count": len(state.stages),
            "failed_stages": failed_stages,
            "safe_stopped": state.safe_stopped,
        },
        links=[req.id],
    )

    return ExecutionTrace(
        workflow_id=state.id,
        workflow_status=state.status.value,
        created_at=state.created_at,
        completed_at=state.completed_at,
        requirement=req_step,
        decisions=decision_steps,
        tasks=task_steps,
        agents=agent_steps,
        artifacts=artifact_steps,
        validations=validation_steps,
        approvals=approval_steps,
        result=result_step,
    )


def compute_workflow_metrics(state: WorkflowState) -> WorkflowMetrics:
    """
    Compute reliability metrics for a single workflow run from WorkflowState.

    Metrics computed:
      • End-to-end latency  (created_at → completed_at)
      • Per-stage latency   (StageContext.started_at → completed_at)
      • Total retries       (sum of attempt_records across all stages)
      • Total rollbacks     (len of rolled_back_stages)
      • MTTR               (mean time from first failure to recovery,
                             only for stages that retried and succeeded)
    """
    stage_metrics_list: list[StageMetrics] = []

    for stage_name, ctx in state.stages.items():
        latency: float | None = None
        if ctx.started_at and ctx.completed_at:
            latency = (ctx.completed_at - ctx.started_at).total_seconds()

        # attempt_count = 1 (no retries) or 1 + len(attempt_records) for retries
        # ctx.attempt is 0-indexed; after N retries ctx.attempt == N-1 (last attempt)
        attempt_count = (ctx.attempt + 1) if ctx.attempt_records or ctx.attempt > 0 else 1

        stage_metrics_list.append(
            StageMetrics(
                stage_name=stage_name,
                stage_id=ctx.stage_id,
                status=ctx.status.value,
                latency_seconds=latency,
                attempt_count=attempt_count,
                retried=len(ctx.attempt_records) > 0,
                rolled_back=ctx.rollback_performed,
                fallback_used=ctx.fallback_used,
            )
        )

    # End-to-end latency
    e2e_latency: float | None = None
    if state.created_at and state.completed_at:
        e2e_latency = (state.completed_at - state.created_at).total_seconds()

    total_retries = sum(len(ctx.attempt_records) for ctx in state.stages.values())
    total_rollbacks = len(state.rolled_back_stages)

    # MTTR — only for stages that retried AND eventually succeeded
    recovery_times: list[float] = []
    for ctx in state.stages.values():
        if (
            ctx.attempt_records
            and ctx.status == StageStatus.COMPLETED
            and ctx.completed_at
        ):
            first_failure_ts = ctx.attempt_records[0].timestamp
            recovery_seconds = (ctx.completed_at - first_failure_ts).total_seconds()
            recovery_times.append(recovery_seconds)

    mttr: float | None = statistics.mean(recovery_times) if recovery_times else None

    return WorkflowMetrics(
        workflow_id=state.id,
        status=state.status.value,
        succeeded=state.status == WorkflowStatus.COMPLETED,
        total_latency_seconds=e2e_latency,
        stage_metrics=stage_metrics_list,
        total_retries=total_retries,
        total_rollbacks=total_rollbacks,
        total_policy_evaluations=len(state.policy_evaluations),
        total_approvals=len(state.approvals),
        mttr_seconds=mttr,
    )


def compute_reliability_metrics(states: list[WorkflowState]) -> ReliabilityMetrics:
    """
    Compute cross-run reliability metrics from a list of WorkflowState objects.

    Pass in all workflow runs for a given time window (sprint, day, deployment
    cycle) to get aggregate success rate, retry frequency, rollback frequency,
    mean latency, and MTTR.
    """
    if not states:
        return ReliabilityMetrics(
            total_runs=0,
            successful_runs=0,
            failed_runs=0,
            success_rate=0.0,
            failure_rate=0.0,
            total_retries=0,
            retry_frequency=0.0,
            total_rollbacks=0,
            rollback_frequency=0.0,
        )

    all_metrics = [compute_workflow_metrics(s) for s in states]

    total = len(states)
    successful = sum(1 for m in all_metrics if m.succeeded)
    failed = total - successful
    total_retries = sum(m.total_retries for m in all_metrics)
    total_rollbacks = sum(m.total_rollbacks for m in all_metrics)

    e2e_latencies = [
        m.total_latency_seconds
        for m in all_metrics
        if m.total_latency_seconds is not None
    ]
    stage_latencies = [
        sm.latency_seconds
        for m in all_metrics
        for sm in m.stage_metrics
        if sm.latency_seconds is not None
    ]

    # MTTR across all runs: mean recovery time for retried+recovered stages
    all_recovery_times: list[float] = []
    for state in states:
        for ctx in state.stages.values():
            if (
                ctx.attempt_records
                and ctx.status == StageStatus.COMPLETED
                and ctx.completed_at
            ):
                first_failure_ts = ctx.attempt_records[0].timestamp
                recovery_seconds = (ctx.completed_at - first_failure_ts).total_seconds()
                all_recovery_times.append(recovery_seconds)

    return ReliabilityMetrics(
        total_runs=total,
        successful_runs=successful,
        failed_runs=failed,
        success_rate=successful / total,
        failure_rate=failed / total,
        total_retries=total_retries,
        retry_frequency=total_retries / total,
        total_rollbacks=total_rollbacks,
        rollback_frequency=total_rollbacks / total,
        mean_e2e_latency_seconds=(
            statistics.mean(e2e_latencies) if e2e_latencies else None
        ),
        mean_stage_latency_seconds=(
            statistics.mean(stage_latencies) if stage_latencies else None
        ),
        mttr_seconds=(
            statistics.mean(all_recovery_times) if all_recovery_times else None
        ),
    )


def build_observability_report(state: WorkflowState) -> WorkflowObservabilityReport:
    """
    Build the complete observability artifact for one workflow run.

    Combines structured logs, execution trace, and reliability metrics
    into a single JSON-serializable object.
    """
    return WorkflowObservabilityReport(
        workflow_id=state.id,
        execution_trace=build_execution_trace(state),
        metrics=compute_workflow_metrics(state),
        structured_logs=build_structured_logs(state),
    )
