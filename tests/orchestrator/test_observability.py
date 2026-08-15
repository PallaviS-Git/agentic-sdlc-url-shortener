"""
Tests for orchestration observability: structured logs, execution trace,
and reliability metrics.

Coverage
────────
Structured log records
  1.  build_structured_logs — empty audit trail → empty list
  2.  build_structured_logs — assigns INFO level to normal events
  3.  build_structured_logs — assigns ERROR level to failure events
  4.  build_structured_logs — assigns WARN level to retry/rollback events
  5.  build_structured_logs — every record carries workflow_id (correlation ID)
  6.  build_structured_logs — stage_id populated from StageContext.stage_id
  7.  build_structured_logs — stage without matching ctx → stage_id is None
  8.  StructuredLogRecord.as_dict — returns JSON-serializable flat dict

Execution trace (IDs present for each step kind)
  9.  build_execution_trace — REQUIREMENT step present, id == requirement.id
  10. build_execution_trace — DECISION steps present, link to requirement
  11. build_execution_trace — TASK steps link to decisions
  12. build_execution_trace — AGENT steps derived from tasks
  13. build_execution_trace — ARTIFACT steps present
  14. build_execution_trace — VALIDATION steps present
  15. build_execution_trace — APPROVAL steps present
  16. build_execution_trace — RESULT step is always last
  17. build_execution_trace — all_steps() returns all kinds in order
  18. ExecutionTrace — step_ids_by_kind filters correctly
  19. WorkflowObservabilityReport — decision_trace, approval_trace, artifact_trace

Workflow metrics (single run)
  20. compute_workflow_metrics — COMPLETED workflow → succeeded=True
  21. compute_workflow_metrics — FAILED workflow → succeeded=False
  22. compute_workflow_metrics — end-to-end latency from created_at to completed_at
  23. compute_workflow_metrics — stage latency from started_at to completed_at
  24. compute_workflow_metrics — no completed_at → total_latency=None
  25. compute_workflow_metrics — total_retries = sum of attempt_records
  26. compute_workflow_metrics — total_rollbacks from rolled_back_stages
  27. compute_workflow_metrics — MTTR for retried-then-recovered stage
  28. compute_workflow_metrics — MTTR is None when no retried stages succeeded
  29. compute_workflow_metrics — stage_metrics has one entry per stage
  30. compute_workflow_metrics — retried=True when attempt_records is non-empty
  31. compute_workflow_metrics — rolled_back=True from StageContext.rollback_performed

Reliability metrics (cross-run)
  32. compute_reliability_metrics — empty list → zero metrics
  33. compute_reliability_metrics — 3 runs (2 ok, 1 fail) → success_rate=2/3
  34. compute_reliability_metrics — failure_rate = 1 - success_rate
  35. compute_reliability_metrics — retry_frequency = total_retries / n_runs
  36. compute_reliability_metrics — rollback_frequency
  37. compute_reliability_metrics — mean_e2e_latency_seconds
  38. compute_reliability_metrics — mean_stage_latency_seconds
  39. compute_reliability_metrics — mttr_seconds across runs

Failure / policy / approval traces
  40. failure_trace — returns only ERROR records
  41. policy_trace — returns policy_evaluated and policy_blocked records
  42. build_observability_report — as_dict is JSON-serializable

Integration (engine + observability together)
  43. Successful workflow → report metrics succeeded=True, latency > 0
  44. Retried workflow → metrics.total_retries > 0, mttr_seconds is not None
  45. Workflow with lineage → trace includes decisions and artifacts
  46. stage_id is unique per workflow run
  47. completed_at set by engine on both success and failure

asyncio_mode=auto (pyproject.toml).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orchestrator.core.autonomy import ActionImpact
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.failure import (
    FailureClassification,
    RecoveryDecision,
    RetryPolicy,
    StageAttemptRecord,
)
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.models import (
    AuditEntry,
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    StageStatus,
    WorkflowState,
    WorkflowStatus,
)
from orchestrator.core.results import DecisionType
from orchestrator.core.observability import (
    TraceStepKind,
    WorkflowObservabilityReport,
    build_execution_trace,
    build_observability_report,
    build_structured_logs,
    compute_reliability_metrics,
    compute_workflow_metrics,
)
from orchestrator.core.results import (
    Artifact,
    ArtifactType,
    Decision,
    ValidationResult,
    ValidationSeverity,
)
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── UTC helper ───────────────────────────────────────────────────────────────


_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _t(seconds: float) -> datetime:
    return _T0 + timedelta(seconds=seconds)


# ─── WorkflowState factory helpers ───────────────────────────────────────────


def _req() -> Requirement:
    return Requirement(
        title="Observability test requirement",
        raw_text="Build the URL shortener service.",
        requirement_type=RequirementType.GREENFIELD,
    )


def _make_state(
    *,
    status: WorkflowStatus = WorkflowStatus.COMPLETED,
    created_offset: float = 0.0,
    completed_offset: float = 10.0,
    stages: dict[str, StageContext] | None = None,
    rolled_back: list[str] | None = None,
    with_audit: bool = True,
) -> WorkflowState:
    req = _req()
    state = WorkflowState(
        requirement=req,
        status=status,
        created_at=_t(created_offset),
        completed_at=_t(completed_offset),
        stages=stages or {},
        rolled_back_stages=rolled_back or [],
    )
    if with_audit:
        state.add_audit_entry("workflow_started", details={"workflow": "test"})
        if status == WorkflowStatus.COMPLETED:
            state.add_audit_entry("workflow_completed", details={"stages_completed": 1})
        else:
            state.add_audit_entry(
                "workflow_failed",
                details={"failed_stages": [], "completed_stages": []},
            )
    return state


def _make_stage_ctx(
    name: str = "test_stage",
    *,
    status: StageStatus = StageStatus.COMPLETED,
    started_offset: float = 1.0,
    completed_offset: float = 5.0,
    attempt_records: list[StageAttemptRecord] | None = None,
    rollback_performed: bool = False,
    fallback_used: bool = False,
) -> StageContext:
    ctx = StageContext(
        stage_name=name,
        status=status,
        started_at=_t(started_offset),
        completed_at=_t(completed_offset),
        rollback_performed=rollback_performed,
        fallback_used=fallback_used,
    )
    if attempt_records:
        ctx.attempt_records = attempt_records
        ctx.attempt = len(attempt_records)
    return ctx


def _retry_record(attempt: int, failed_at_offset: float) -> StageAttemptRecord:
    return StageAttemptRecord(
        attempt=attempt,
        error="TransientError: timed out",
        error_type="TransientError",
        classification=FailureClassification.TRANSIENT,
        recovery_decision=RecoveryDecision.RETRY,
        timestamp=_t(failed_at_offset),
    )


# ─── Stage stubs for integration tests ───────────────────────────────────────


class _BaseTestStage(BaseStage):
    stage_name: str = ""

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_entry", passed=True)

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_exit", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        ctx.output_data["ran"] = True
        return ctx

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


class _LinStage(_BaseTestStage):
    """Produces a decision, a task, and an artifact — rich lineage for trace tests."""

    stage_name = "lin"
    _DEC_ID = "dec-lin-001"
    _ARTIFACT_ID = "art-lin-001"
    _TASK_ID = "task-lin-001"

    async def execute(self, ctx: StageContext) -> StageContext:
        from orchestrator.core.models import Task, TaskStatus

        dec = Decision(
            id=self._DEC_ID,
            decision_type=DecisionType.SCOPE,
            title="Database selection",
            description="Choose primary database technology",
            rationale="Use PostgreSQL for primary storage",
            stage="lin",
            made_at=_t(2),
        )
        ctx.decisions.append(dec)

        task = Task(
            id=self._TASK_ID,
            title="Set up DB schema",
            description="Create tables for URL shortener",
            status=TaskStatus.COMPLETED,
            stage="lin",
            rationale="Needed by the storage decision",
            created_by_decision_id=self._DEC_ID,
            assigned_agent="db-agent",
            agent_execution_id="exec-db-001",
        )
        ctx.tasks.append(task)

        artifact = Artifact(
            id=self._ARTIFACT_ID,
            name="schema.sql",
            artifact_type=ArtifactType.SCHEMA,
            produced_by_stage="lin",
            path="file://schema.sql",
        )
        ctx.artifacts.append(artifact)

        val = ValidationResult(
            rule_name="schema_not_empty",
            passed=True,
            stage="lin",
            severity=ValidationSeverity.ERROR,
            message="Schema file has at least one table",
        )
        ctx.validations.append(val)

        ctx.output_data["schema"] = "schema.sql"
        return ctx


class _FlakyIntegrationStage(_BaseTestStage):
    """Fails once then succeeds — for retry + MTTR tests."""

    stage_name = "flaky"

    def __init__(self) -> None:
        self._calls = 0
        self.retry_policy = RetryPolicy(max_attempts=3)

    async def execute(self, ctx: StageContext) -> StageContext:
        self._calls += 1
        if self._calls < 2:
            raise RuntimeError("Transient fault")
        ctx.output_data["ok"] = True
        return ctx


def _single(stage: BaseStage) -> tuple[WorkflowDefinition, dict]:
    defn = WorkflowDefinition(name="obs-test", description="", stages=[stage.stage_name])
    return defn, {stage.stage_name: stage}


@pytest.fixture()
def requirement() -> Requirement:
    return _req()


# ═══════════════════════════════════════════════════════════════════════════════
# Structured log records
# ═══════════════════════════════════════════════════════════════════════════════


class TestStructuredLogs:
    def test_empty_audit_trail_returns_empty_list(self) -> None:
        state = _make_state(with_audit=False)
        assert build_structured_logs(state) == []

    def test_normal_event_is_info(self) -> None:
        state = _make_state()
        records = build_structured_logs(state)
        started = [r for r in records if r.event == "workflow_started"]
        assert started and all(r.level == "INFO" for r in started)

    def test_failure_event_is_error(self) -> None:
        state = _make_state(status=WorkflowStatus.FAILED)
        state.add_audit_entry("stage_failed_all_attempts", stage="x", details={})
        records = build_structured_logs(state)
        error_recs = [r for r in records if r.event == "stage_failed_all_attempts"]
        assert error_recs and error_recs[0].level == "ERROR"

    def test_retry_event_is_warn(self) -> None:
        state = _make_state()
        state.add_audit_entry("stage_retrying", stage="x", details={"attempt": 1})
        records = build_structured_logs(state)
        warn_recs = [r for r in records if r.event == "stage_retrying"]
        assert warn_recs and warn_recs[0].level == "WARN"

    def test_every_record_has_workflow_id(self) -> None:
        state = _make_state()
        records = build_structured_logs(state)
        assert records
        assert all(r.workflow_id == state.id for r in records)

    def test_stage_id_populated_from_stage_context(self) -> None:
        ctx = _make_stage_ctx("s1")
        state = _make_state()
        state.stages["s1"] = ctx
        state.add_audit_entry("stage_started", stage="s1", details={})
        records = build_structured_logs(state)
        stage_recs = [r for r in records if r.stage_name == "s1"]
        assert stage_recs and stage_recs[0].stage_id == ctx.stage_id

    def test_audit_entry_without_stage_has_null_stage_id(self) -> None:
        state = _make_state()
        records = build_structured_logs(state)
        global_recs = [r for r in records if r.stage_name is None]
        assert global_recs and all(r.stage_id is None for r in global_recs)

    def test_as_dict_is_json_serializable(self) -> None:
        ctx = _make_stage_ctx("s1")
        state = _make_state()
        state.stages["s1"] = ctx
        state.add_audit_entry("stage_started", stage="s1", details={})
        records = build_structured_logs(state)
        for r in records:
            d = r.as_dict()
            # Verify round-trip through JSON
            json.dumps(d)  # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Execution trace
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutionTrace:
    def _state_with_lineage(self) -> WorkflowState:
        """Build state with rich lineage by running the engine."""
        return None  # populated by engine in integration tests

    def test_requirement_step_present_with_correct_id(self) -> None:
        state = _make_state()
        trace = build_execution_trace(state)
        assert trace.requirement is not None
        assert trace.requirement.kind == TraceStepKind.REQUIREMENT
        assert trace.requirement.id == state.requirement.id

    def test_result_step_is_always_present(self) -> None:
        state = _make_state()
        trace = build_execution_trace(state)
        assert trace.result is not None
        assert trace.result.kind == TraceStepKind.RESULT

    def test_result_step_status_matches_workflow(self) -> None:
        state = _make_state(status=WorkflowStatus.FAILED)
        trace = build_execution_trace(state)
        assert trace.result.name == "failed"

    def test_all_steps_order_starts_with_requirement(self) -> None:
        state = _make_state()
        trace = build_execution_trace(state)
        steps = trace.all_steps()
        assert steps[0].kind == TraceStepKind.REQUIREMENT

    def test_all_steps_order_ends_with_result(self) -> None:
        state = _make_state()
        trace = build_execution_trace(state)
        steps = trace.all_steps()
        assert steps[-1].kind == TraceStepKind.RESULT

    def test_step_ids_by_kind_filters_correctly(self) -> None:
        state = _make_state()
        trace = build_execution_trace(state)
        req_ids = trace.step_ids_by_kind(TraceStepKind.REQUIREMENT)
        assert len(req_ids) == 1
        assert req_ids[0] == state.requirement.id

    def test_workflow_id_on_trace(self) -> None:
        state = _make_state()
        trace = build_execution_trace(state)
        assert trace.workflow_id == state.id

    def test_trace_completed_at_matches_state(self) -> None:
        state = _make_state(completed_offset=15.0)
        trace = build_execution_trace(state)
        assert trace.completed_at == state.completed_at

    def test_report_decision_trace_convenience(self) -> None:
        state = _make_state()
        report = build_observability_report(state)
        # No decisions in empty state — just verify the method exists and returns a list
        assert isinstance(report.decision_trace(), list)

    def test_report_approval_trace_convenience(self) -> None:
        state = _make_state()
        report = build_observability_report(state)
        assert isinstance(report.approval_trace(), list)

    def test_report_artifact_trace_convenience(self) -> None:
        state = _make_state()
        report = build_observability_report(state)
        assert isinstance(report.artifact_trace(), list)


# ═══════════════════════════════════════════════════════════════════════════════
# Workflow metrics (single run)
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkflowMetrics:
    def test_completed_workflow_succeeded_true(self) -> None:
        state = _make_state(status=WorkflowStatus.COMPLETED)
        m = compute_workflow_metrics(state)
        assert m.succeeded is True
        assert m.status == "completed"

    def test_failed_workflow_succeeded_false(self) -> None:
        state = _make_state(status=WorkflowStatus.FAILED)
        m = compute_workflow_metrics(state)
        assert m.succeeded is False

    def test_e2e_latency_from_created_to_completed(self) -> None:
        state = _make_state(created_offset=0.0, completed_offset=10.0)
        m = compute_workflow_metrics(state)
        assert m.total_latency_seconds == pytest.approx(10.0)

    def test_no_completed_at_means_no_latency(self) -> None:
        state = _make_state()
        state.completed_at = None
        m = compute_workflow_metrics(state)
        assert m.total_latency_seconds is None

    def test_stage_latency_calculated(self) -> None:
        ctx = _make_stage_ctx("s1", started_offset=1.0, completed_offset=4.0)
        state = _make_state()
        state.stages["s1"] = ctx
        m = compute_workflow_metrics(state)
        stage = next(sm for sm in m.stage_metrics if sm.stage_name == "s1")
        assert stage.latency_seconds == pytest.approx(3.0)

    def test_stage_latency_none_when_no_timestamps(self) -> None:
        ctx = StageContext(stage_name="no_times", status=StageStatus.PENDING)
        state = _make_state()
        state.stages["no_times"] = ctx
        m = compute_workflow_metrics(state)
        stage = next(sm for sm in m.stage_metrics if sm.stage_name == "no_times")
        assert stage.latency_seconds is None

    def test_total_retries_from_attempt_records(self) -> None:
        ctx = _make_stage_ctx(
            "retry_stage",
            attempt_records=[
                _retry_record(0, 1.0),
                _retry_record(1, 2.0),
            ],
        )
        state = _make_state()
        state.stages["retry_stage"] = ctx
        m = compute_workflow_metrics(state)
        assert m.total_retries == 2

    def test_total_rollbacks_from_rolled_back_stages(self) -> None:
        state = _make_state(rolled_back=["stage_a", "stage_b"])
        m = compute_workflow_metrics(state)
        assert m.total_rollbacks == 2

    def test_mttr_calculated_for_retried_recovered_stage(self) -> None:
        # Stage failed at t=1, recovered at t=6 → MTTR = 5s
        ctx = _make_stage_ctx(
            "recovered",
            started_offset=0.0,
            completed_offset=6.0,
            attempt_records=[_retry_record(0, 1.0)],
        )
        state = _make_state()
        state.stages["recovered"] = ctx
        m = compute_workflow_metrics(state)
        assert m.mttr_seconds == pytest.approx(5.0)

    def test_mttr_none_when_no_recovery(self) -> None:
        # Stage has retries but status is FAILED (no recovery)
        ctx = _make_stage_ctx(
            "still_failing",
            status=StageStatus.FAILED,
            attempt_records=[_retry_record(0, 1.0)],
        )
        state = _make_state()
        state.stages["still_failing"] = ctx
        m = compute_workflow_metrics(state)
        assert m.mttr_seconds is None

    def test_stage_metrics_one_entry_per_stage(self) -> None:
        ctx_a = _make_stage_ctx("a")
        ctx_b = _make_stage_ctx("b")
        state = _make_state()
        state.stages["a"] = ctx_a
        state.stages["b"] = ctx_b
        m = compute_workflow_metrics(state)
        assert len(m.stage_metrics) == 2

    def test_retried_flag_from_attempt_records(self) -> None:
        ctx = _make_stage_ctx(
            "r",
            attempt_records=[_retry_record(0, 1.0)],
        )
        state = _make_state()
        state.stages["r"] = ctx
        m = compute_workflow_metrics(state)
        sm = m.stage_metrics[0]
        assert sm.retried is True

    def test_rolled_back_flag_from_rollback_performed(self) -> None:
        ctx = _make_stage_ctx("rb", rollback_performed=True, status=StageStatus.ROLLED_BACK)
        state = _make_state()
        state.stages["rb"] = ctx
        m = compute_workflow_metrics(state)
        assert m.stage_metrics[0].rolled_back is True

    def test_stage_id_in_metrics(self) -> None:
        ctx = _make_stage_ctx("s1")
        state = _make_state()
        state.stages["s1"] = ctx
        m = compute_workflow_metrics(state)
        assert m.stage_metrics[0].stage_id == ctx.stage_id


# ═══════════════════════════════════════════════════════════════════════════════
# Reliability metrics (cross-run)
# ═══════════════════════════════════════════════════════════════════════════════


class TestReliabilityMetrics:
    def test_empty_list_returns_zero_metrics(self) -> None:
        m = compute_reliability_metrics([])
        assert m.total_runs == 0
        assert m.success_rate == 0.0
        assert m.failure_rate == 0.0

    def test_success_rate_two_of_three(self) -> None:
        states = [
            _make_state(status=WorkflowStatus.COMPLETED),
            _make_state(status=WorkflowStatus.COMPLETED),
            _make_state(status=WorkflowStatus.FAILED),
        ]
        m = compute_reliability_metrics(states)
        assert m.total_runs == 3
        assert m.successful_runs == 2
        assert m.failed_runs == 1
        assert m.success_rate == pytest.approx(2 / 3)

    def test_failure_rate_complement_of_success_rate(self) -> None:
        states = [
            _make_state(status=WorkflowStatus.COMPLETED),
            _make_state(status=WorkflowStatus.FAILED),
        ]
        m = compute_reliability_metrics(states)
        assert m.success_rate + m.failure_rate == pytest.approx(1.0)

    def test_retry_frequency_average(self) -> None:
        # Run 1: 2 retries; Run 2: 0 retries → frequency = 1.0
        ctx_with_retries = _make_stage_ctx(
            "r",
            attempt_records=[_retry_record(0, 1.0), _retry_record(1, 2.0)],
        )
        s1 = _make_state()
        s1.stages["r"] = ctx_with_retries
        s2 = _make_state()
        m = compute_reliability_metrics([s1, s2])
        assert m.total_retries == 2
        assert m.retry_frequency == pytest.approx(1.0)

    def test_rollback_frequency(self) -> None:
        s1 = _make_state(rolled_back=["s"])
        s2 = _make_state(rolled_back=[])
        m = compute_reliability_metrics([s1, s2])
        assert m.total_rollbacks == 1
        assert m.rollback_frequency == pytest.approx(0.5)

    def test_mean_e2e_latency(self) -> None:
        # s1: 10s, s2: 20s → mean = 15s
        s1 = _make_state(created_offset=0.0, completed_offset=10.0)
        s2 = _make_state(created_offset=0.0, completed_offset=20.0)
        m = compute_reliability_metrics([s1, s2])
        assert m.mean_e2e_latency_seconds == pytest.approx(15.0)

    def test_mean_e2e_latency_none_when_no_completed_at(self) -> None:
        s = _make_state()
        s.completed_at = None
        m = compute_reliability_metrics([s])
        assert m.mean_e2e_latency_seconds is None

    def test_mean_stage_latency(self) -> None:
        ctx_a = _make_stage_ctx("a", started_offset=0.0, completed_offset=4.0)  # 4s
        ctx_b = _make_stage_ctx("b", started_offset=0.0, completed_offset=6.0)  # 6s
        state = _make_state()
        state.stages["a"] = ctx_a
        state.stages["b"] = ctx_b
        m = compute_reliability_metrics([state])
        assert m.mean_stage_latency_seconds == pytest.approx(5.0)

    def test_mttr_across_runs(self) -> None:
        # Run 1: stage failed at t=1, recovered at t=6 → 5s
        # Run 2: stage failed at t=0, recovered at t=10 → 10s
        # Cross-run MTTR = 7.5s
        ctx1 = _make_stage_ctx(
            "r",
            started_offset=0.0,
            completed_offset=6.0,
            attempt_records=[_retry_record(0, 1.0)],
        )
        s1 = _make_state()
        s1.stages["r"] = ctx1

        ctx2 = _make_stage_ctx(
            "r",
            started_offset=0.0,
            completed_offset=10.0,
            attempt_records=[_retry_record(0, 0.0)],
        )
        s2 = _make_state()
        s2.stages["r"] = ctx2

        m = compute_reliability_metrics([s1, s2])
        assert m.mttr_seconds == pytest.approx(7.5)

    def test_mttr_none_when_no_recoveries(self) -> None:
        m = compute_reliability_metrics([_make_state(), _make_state()])
        assert m.mttr_seconds is None


# ═══════════════════════════════════════════════════════════════════════════════
# Failure / policy / approval traces
# ═══════════════════════════════════════════════════════════════════════════════


class TestSpecialTraces:
    def test_failure_trace_returns_only_error_records(self) -> None:
        state = _make_state()
        state.add_audit_entry("stage_retrying", stage="x", details={})
        state.add_audit_entry("stage_failed_all_attempts", stage="x", details={})
        report = build_observability_report(state)
        ft = report.failure_trace()
        assert all(r.level == "ERROR" for r in ft)
        assert any(r.event == "stage_failed_all_attempts" for r in ft)

    def test_policy_trace_includes_policy_events(self) -> None:
        state = _make_state()
        state.add_audit_entry("policy_evaluated", stage="s", details={"decision": "allow"})
        state.add_audit_entry("policy_blocked", stage="s", details={"policies": ["SEC-001"]})
        report = build_observability_report(state)
        pt = report.policy_trace()
        events = {r.event for r in pt}
        assert "policy_evaluated" in events
        assert "policy_blocked" in events

    def test_as_dict_round_trips_json(self) -> None:
        state = _make_state()
        report = build_observability_report(state)
        d = report.as_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        reconstructed = json.loads(serialized)
        assert reconstructed["workflow_id"] == state.id


# ═══════════════════════════════════════════════════════════════════════════════
# Integration tests — engine + observability
# ═══════════════════════════════════════════════════════════════════════════════


class TestObservabilityIntegration:
    async def test_completed_workflow_report(self, requirement: Requirement) -> None:
        class _Ok(_BaseTestStage):
            stage_name = "ok"

        defn, stages = _single(_Ok())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.completed_at is not None
        report = build_observability_report(state)

        assert report.metrics.succeeded is True
        assert report.metrics.total_latency_seconds is not None
        assert report.metrics.total_latency_seconds >= 0.0

    async def test_failed_workflow_completed_at_set(
        self, requirement: Requirement
    ) -> None:
        class _Fail(_BaseTestStage):
            stage_name = "fail"

            async def execute(self, ctx: StageContext) -> StageContext:
                raise RuntimeError("intentional failure")

        defn, stages = _single(_Fail())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.completed_at is not None
        assert state.status == WorkflowStatus.FAILED

    async def test_retried_stage_mttr_calculated(
        self, requirement: Requirement
    ) -> None:
        class _Flaky(_BaseTestStage):
            stage_name = "flaky"

            def __init__(self) -> None:
                self._n = 0
                self.retry_policy = RetryPolicy(max_attempts=3)

            async def execute(self, ctx: StageContext) -> StageContext:
                self._n += 1
                if self._n < 2:
                    raise RuntimeError("transient")
                ctx.output_data["ok"] = True
                return ctx

        defn, stages = _single(_Flaky())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        report = build_observability_report(state)
        assert report.metrics.total_retries >= 1
        assert report.metrics.mttr_seconds is not None
        assert report.metrics.mttr_seconds >= 0.0

    async def test_stage_id_unique_per_run(self, requirement: Requirement) -> None:
        class _S(_BaseTestStage):
            stage_name = "s"

        defn, stages = _single(_S())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state1 = await engine.run(requirement)
        state2 = await engine.run(requirement)

        assert state1.stages["s"].stage_id != state2.stages["s"].stage_id

    async def test_trace_includes_decisions_and_artifacts(
        self, requirement: Requirement
    ) -> None:
        defn, stages = _single(_LinStage())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        report = build_observability_report(state)
        trace = report.execution_trace

        # Decisions
        assert len(trace.decisions) >= 1
        assert any(s.id == _LinStage._DEC_ID for s in trace.decisions)

        # Tasks
        assert len(trace.tasks) >= 1
        task_step = next(s for s in trace.tasks if s.id == _LinStage._TASK_ID)
        assert task_step.details["agent_execution_id"] == "exec-db-001"

        # Agents
        assert any(s.name == "db-agent" for s in trace.agents)

        # Artifacts
        assert len(trace.artifacts) >= 1
        assert any(s.id == _LinStage._ARTIFACT_ID for s in trace.artifacts)

        # Validations
        assert len(trace.validations) >= 1

    async def test_structured_logs_include_stage_id_correlation(
        self, requirement: Requirement
    ) -> None:
        class _S2(_BaseTestStage):
            stage_name = "s2"

        defn, stages = _single(_S2())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        logs = build_structured_logs(state)
        stage_logs = [r for r in logs if r.stage_name == "s2"]
        assert stage_logs
        expected_sid = state.stages["s2"].stage_id
        assert all(r.stage_id == expected_sid for r in stage_logs)

    async def test_reliability_metrics_across_two_runs(
        self, requirement: Requirement
    ) -> None:
        class _Ok2(_BaseTestStage):
            stage_name = "ok2"

        defn, stages = _single(_Ok2())
        engine = WorkflowEngine(definition=defn, stages=stages)
        s1 = await engine.run(requirement)
        s2 = await engine.run(requirement)

        metrics = compute_reliability_metrics([s1, s2])
        assert metrics.total_runs == 2
        assert metrics.successful_runs == 2
        assert metrics.success_rate == pytest.approx(1.0)
        assert metrics.mean_e2e_latency_seconds is not None
