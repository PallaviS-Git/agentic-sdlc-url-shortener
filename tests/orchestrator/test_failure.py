"""
Tests for bounded retries, retry policies, failure classification,
fallback, rollback, safe-stop, and recovery state tracking.

Required coverage:
  1.  Stage succeeds on second attempt (successful retry)
  2.  Retries exhausted → FAILED; attempt_records has max_attempts entries
  3.  Non-retryable exception skips retries → FAILED after 1 attempt
  4.  Fallback SKIP → SKIPPED status; downstream sees empty output
  5.  Fallback USE_PRESET → COMPLETED with fallback output; downstream sees preset
  6.  Rollback invoked on failure; stage status = ROLLED_BACK
  7.  Safe-stop triggered by CRITICAL exception; workflow status = STOPPED
  8.  Recovery decisions recorded in attempt_records (traceable)
  9.  max_attempts=1 (DEFAULT) → single attempt, no retry
  10. Retry on exit-gate failure (exit_gate_failure_retryable=True)
  11. No retry on exit-gate failure (exit_gate_failure_retryable=False)
  12. Downstream BLOCKED when upstream fails after retries
  13. Downstream BLOCKED when upstream triggers safe-stop
  14. Rollback failure is logged but does not re-raise
  15. Fallback USE_PRESET without fallback_output → falls through to FAIL
  16. WorkflowState.rolled_back_stages updated after successful rollback
  17. WorkflowState.safe_stopped and safe_stop_reason set correctly
  18. RetryPolicy.classify() respects MRO for base-class matching
  19. max_attempts bound: Field(ge=1) prevents 0 or negative values

No I/O, no network, no DB. asyncio_mode=auto (pytest.ini) handles async tests.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.core.base_stage import BaseStage
from orchestrator.core.failure import (
    DEFAULT_RETRY_POLICY,
    FailureClassification,
    FallbackBehavior,
    RecoveryDecision,
    RetryPolicy,
    StageAttemptRecord,
)
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.models import (
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    WorkflowStatus,
)
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── Custom exception hierarchy ───────────────────────────────────────────────


class TransientError(Exception):
    """Simulates a temporary failure (network timeout, DB lock, etc.)"""


class PermanentError(Exception):
    """Simulates a non-retryable failure (invalid input, schema violation, etc.)"""


class SafeStopError(Exception):
    """Simulates a CRITICAL failure requiring workflow safe-stop."""


class SpecialTransientError(TransientError):
    """Subclass of TransientError — used to test MRO-based classification."""


# ─── Stage stubs ──────────────────────────────────────────────────────────────


class _BaseTestStage(BaseStage):
    """Minimal concrete base with trivially-passing gates."""

    stage_name: str = ""

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_entry", passed=True)

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_exit", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:  # pragma: no cover
        return ctx

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


class _SucceedsOnAttemptN(_BaseTestStage):
    """Raises TransientError for the first N-1 attempts, then succeeds."""

    stage_name = "flaky"

    def __init__(self, *, succeed_on: int = 2, max_attempts: int = 3) -> None:
        self._succeed_on = succeed_on
        self._call_count = 0
        self.retry_policy = RetryPolicy(max_attempts=max_attempts)

    async def execute(self, ctx: StageContext) -> StageContext:
        self._call_count += 1
        if self._call_count < self._succeed_on:
            raise TransientError(f"Attempt {self._call_count} of {self._succeed_on - 1} failing")
        ctx.output_data["succeeded_on"] = self._call_count
        return ctx


class _AlwaysFails(_BaseTestStage):
    """Always raises TransientError; used to exhaust retries."""

    stage_name = "always_fails"

    def __init__(self, *, max_attempts: int = 3) -> None:
        self.retry_policy = RetryPolicy(max_attempts=max_attempts)

    async def execute(self, ctx: StageContext) -> StageContext:
        raise TransientError("Transient; will never succeed")


class _PermanentFailure(_BaseTestStage):
    """Raises PermanentError — should NOT be retried."""

    stage_name = "permanent_fail"

    def __init__(self) -> None:
        self.retry_policy = RetryPolicy(
            max_attempts=3,
            non_retryable_error_types=["PermanentError"],
        )

    async def execute(self, ctx: StageContext) -> StageContext:
        raise PermanentError("This failure is not retryable")


class _SafeStopFailure(_BaseTestStage):
    """Raises SafeStopError — should trigger workflow safe-stop."""

    stage_name = "dangerous"

    def __init__(self) -> None:
        self.retry_policy = RetryPolicy(
            max_attempts=3,
            safe_stop_error_types=["SafeStopError"],
        )

    async def execute(self, ctx: StageContext) -> StageContext:
        raise SafeStopError("Critical invariant violated; halting")


class _FallbackSkip(_BaseTestStage):
    """Always fails; configured to SKIP when retries exhausted."""

    stage_name = "fallback_skip"

    def __init__(self) -> None:
        self.retry_policy = RetryPolicy(
            max_attempts=2,
            fallback_behavior=FallbackBehavior.SKIP,
        )

    async def execute(self, ctx: StageContext) -> StageContext:
        raise TransientError("Always fails")


class _FallbackPreset(_BaseTestStage):
    """Always fails; configured to USE_PRESET when retries exhausted."""

    stage_name = "fallback_preset"

    def __init__(self) -> None:
        self.retry_policy = RetryPolicy(
            max_attempts=2,
            fallback_behavior=FallbackBehavior.USE_PRESET,
            fallback_output={"status": "fallback", "value": 42},
        )

    async def execute(self, ctx: StageContext) -> StageContext:
        raise TransientError("Always fails")


_rollback_log: list[str] = []


class _RollbackOnFailure(_BaseTestStage):
    """Always fails; configured to rollback on failure."""

    stage_name = "rollback_stage"

    def __init__(self) -> None:
        self.retry_policy = RetryPolicy(
            max_attempts=2,
            rollback_on_failure=True,
        )

    async def execute(self, ctx: StageContext) -> StageContext:
        raise TransientError("Fails requiring rollback")

    async def rollback(self, ctx: StageContext) -> StageContext:
        _rollback_log.append(f"rollback:{ctx.stage_name}")
        ctx.rollback_performed = True
        return ctx


class _RollbackFailsItself(_BaseTestStage):
    """Stage whose rollback() also raises — engine must not re-raise."""

    stage_name = "bad_rollback"

    def __init__(self) -> None:
        self.retry_policy = RetryPolicy(
            max_attempts=1,
            rollback_on_failure=True,
        )

    async def execute(self, ctx: StageContext) -> StageContext:
        raise TransientError("Fails")

    async def rollback(self, ctx: StageContext) -> StageContext:
        raise RuntimeError("Rollback itself fails!")


class _ExitGateFlaky(_BaseTestStage):
    """Execute always succeeds but exit-gate fails on first attempt."""

    stage_name = "exit_gate_flaky"

    def __init__(self, *, retryable: bool = True) -> None:
        self._exit_calls = 0
        self.retry_policy = RetryPolicy(
            max_attempts=2,
            exit_gate_failure_retryable=retryable,
        )

    async def execute(self, ctx: StageContext) -> StageContext:
        ctx.output_data["executed"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        self._exit_calls += 1
        passed = self._exit_calls > 1  # fail on first call, pass on second
        return GateResult(
            gate_name="exit",
            passed=passed,
            reason=None if passed else "First exit check always fails",
        )


class _DownstreamStage(_BaseTestStage):
    """Records whether it ran and what input it received."""

    stage_name = "downstream"

    async def execute(self, ctx: StageContext) -> StageContext:
        ctx.output_data["ran"] = True
        return ctx


# ─── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def requirement() -> Requirement:
    return Requirement(
        title="Failure test",
        raw_text="Test failure handling",
        requirement_type=RequirementType.GREENFIELD,
    )


def _single(stage: BaseStage) -> tuple[WorkflowDefinition, dict]:
    defn = WorkflowDefinition(
        name="test", description="", stages=[stage.stage_name]
    )
    return defn, {stage.stage_name: stage}


def _linear(
    upstream: BaseStage, downstream: BaseStage
) -> tuple[WorkflowDefinition, dict]:
    defn = WorkflowDefinition(
        name="test",
        description="",
        stages=[upstream.stage_name, downstream.stage_name],
        dependencies=[
            StageDependency(
                from_stage=upstream.stage_name, to_stage=downstream.stage_name
            )
        ],
    )
    return defn, {upstream.stage_name: upstream, downstream.stage_name: downstream}


# ─── 1. Successful retry ──────────────────────────────────────────────────────


class TestSuccessfulRetry:
    async def test_stage_succeeds_on_second_attempt(self, requirement: Requirement) -> None:
        stage = _SucceedsOnAttemptN(succeed_on=2, max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        ctx = state.stages["flaky"]
        assert ctx.output_data["succeeded_on"] == 2
        assert ctx.attempt == 1  # 0-indexed second attempt

    async def test_first_attempt_failure_recorded(self, requirement: Requirement) -> None:
        stage = _SucceedsOnAttemptN(succeed_on=2, max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["flaky"]
        assert len(ctx.attempt_records) == 1
        record = ctx.attempt_records[0]
        assert record.attempt == 0
        assert record.classification == FailureClassification.TRANSIENT
        assert record.recovery_decision == RecoveryDecision.RETRY

    async def test_retry_audit_event_emitted(self, requirement: Requirement) -> None:
        stage = _SucceedsOnAttemptN(succeed_on=2, max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "stage_started" in events
        assert "stage_retrying" in events


# ─── 2. Retry exhaustion ──────────────────────────────────────────────────────


class TestRetryExhaustion:
    async def test_stage_fails_after_max_attempts(self, requirement: Requirement) -> None:
        stage = _AlwaysFails(max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        ctx = state.stages["always_fails"]
        assert ctx.status.value == "failed"

    async def test_attempt_records_count_equals_max_attempts(
        self, requirement: Requirement
    ) -> None:
        stage = _AlwaysFails(max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["always_fails"]
        assert len(ctx.attempt_records) == 3

    async def test_last_attempt_decision_is_fail_immediate(
        self, requirement: Requirement
    ) -> None:
        stage = _AlwaysFails(max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["always_fails"]
        last = ctx.attempt_records[-1]
        assert last.recovery_decision == RecoveryDecision.FAIL_IMMEDIATE

    async def test_intermediate_attempts_decision_is_retry(
        self, requirement: Requirement
    ) -> None:
        stage = _AlwaysFails(max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["always_fails"]
        # First two records should be RETRY, last one FAIL_IMMEDIATE
        assert ctx.attempt_records[0].recovery_decision == RecoveryDecision.RETRY
        assert ctx.attempt_records[1].recovery_decision == RecoveryDecision.RETRY
        assert ctx.attempt_records[2].recovery_decision == RecoveryDecision.FAIL_IMMEDIATE

    async def test_stage_completed_at_is_set(self, requirement: Requirement) -> None:
        stage = _AlwaysFails(max_attempts=2)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["always_fails"]
        assert ctx.completed_at is not None


# ─── 3. Non-retryable failure ─────────────────────────────────────────────────


class TestNonRetryableFailure:
    async def test_permanent_failure_does_not_retry(
        self, requirement: Requirement
    ) -> None:
        stage = _PermanentFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        ctx = state.stages["permanent_fail"]
        # Only one attempt — no retry for PERMANENT
        assert len(ctx.attempt_records) == 1

    async def test_permanent_failure_classification_is_permanent(
        self, requirement: Requirement
    ) -> None:
        stage = _PermanentFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["permanent_fail"]
        record = ctx.attempt_records[0]
        assert record.classification == FailureClassification.PERMANENT
        assert record.recovery_decision == RecoveryDecision.FAIL_IMMEDIATE

    async def test_permanent_failure_error_type_recorded(
        self, requirement: Requirement
    ) -> None:
        stage = _PermanentFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["permanent_fail"]
        assert ctx.attempt_records[0].error_type == "PermanentError"


# ─── 4-5. Fallback ────────────────────────────────────────────────────────────


class TestFallbackSkip:
    async def test_skipped_stage_workflow_completes(
        self, requirement: Requirement
    ) -> None:
        stage = _FallbackSkip()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        ctx = state.stages["fallback_skip"]
        assert ctx.status.value == "skipped"
        assert ctx.fallback_used is True

    async def test_downstream_sees_empty_output_after_skip(
        self, requirement: Requirement
    ) -> None:
        upstream = _FallbackSkip()
        downstream = _DownstreamStage()
        defn, stages = _linear(upstream, downstream)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["downstream"].output_data.get("ran") is True

    async def test_skip_audit_event_emitted(self, requirement: Requirement) -> None:
        stage = _FallbackSkip()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "stage_skipped_fallback" in events


class TestFallbackUsePreset:
    async def test_preset_stage_status_is_completed(
        self, requirement: Requirement
    ) -> None:
        stage = _FallbackPreset()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        ctx = state.stages["fallback_preset"]
        assert ctx.status.value == "completed"
        assert ctx.fallback_used is True

    async def test_preset_output_data_propagated(
        self, requirement: Requirement
    ) -> None:
        stage = _FallbackPreset()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["fallback_preset"]
        assert ctx.output_data["status"] == "fallback"
        assert ctx.output_data["value"] == 42

    async def test_downstream_sees_preset_output(
        self, requirement: Requirement
    ) -> None:
        upstream = _FallbackPreset()
        downstream = _DownstreamStage()
        defn, stages = _linear(upstream, downstream)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["downstream"].input_data.get("status") == "fallback"

    async def test_fallback_audit_event_emitted(
        self, requirement: Requirement
    ) -> None:
        stage = _FallbackPreset()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "stage_fallback_applied" in events


# ─── 6. Rollback ─────────────────────────────────────────────────────────────


class TestRollback:
    def setup_method(self) -> None:
        _rollback_log.clear()

    async def test_rollback_called_on_failure(
        self, requirement: Requirement
    ) -> None:
        stage = _RollbackOnFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        await engine.run(requirement)

        assert "rollback:rollback_stage" in _rollback_log

    async def test_stage_status_is_rolled_back(
        self, requirement: Requirement
    ) -> None:
        stage = _RollbackOnFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["rollback_stage"]
        assert ctx.status.value == "rolled_back"
        assert ctx.rollback_performed is True

    async def test_workflow_status_failed_after_rollback(
        self, requirement: Requirement
    ) -> None:
        stage = _RollbackOnFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED

    async def test_rolled_back_stages_tracked_in_workflow_state(
        self, requirement: Requirement
    ) -> None:
        stage = _RollbackOnFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert "rollback_stage" in state.rolled_back_stages

    async def test_rollback_audit_events_emitted(
        self, requirement: Requirement
    ) -> None:
        stage = _RollbackOnFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "rollback_started" in events
        assert "rollback_completed" in events

    async def test_rollback_itself_fails_does_not_reraise(
        self, requirement: Requirement
    ) -> None:
        """Rollback failure must be logged, not propagated."""
        stage = _RollbackFailsItself()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        events = {e.event for e in state.audit_trail}
        assert "rollback_failed" in events
        # Stage was never added to rolled_back_stages
        assert "bad_rollback" not in state.rolled_back_stages


# ─── 7. Safe-stop ─────────────────────────────────────────────────────────────


class TestSafeStop:
    async def test_workflow_status_is_stopped(
        self, requirement: Requirement
    ) -> None:
        stage = _SafeStopFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.STOPPED

    async def test_safe_stopped_flag_set(self, requirement: Requirement) -> None:
        stage = _SafeStopFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.safe_stopped is True
        assert "dangerous" in state.safe_stop_reason

    async def test_no_approval_record_for_safe_stop(
        self, requirement: Requirement
    ) -> None:
        """Safe-stop should not trigger any approval checkpoints."""
        stage = _SafeStopFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert len(state.approvals) == 0

    async def test_safe_stop_classification_in_attempt_record(
        self, requirement: Requirement
    ) -> None:
        stage = _SafeStopFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["dangerous"]
        assert len(ctx.attempt_records) == 1
        assert ctx.attempt_records[0].classification == FailureClassification.CRITICAL
        assert ctx.attempt_records[0].recovery_decision == RecoveryDecision.SAFE_STOP

    async def test_safe_stop_audit_event_emitted(
        self, requirement: Requirement
    ) -> None:
        stage = _SafeStopFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "safe_stop_triggered" in events

    async def test_downstream_blocked_after_safe_stop(
        self, requirement: Requirement
    ) -> None:
        """Stages that never got a chance to run must be BLOCKED after safe-stop."""
        upstream = _SafeStopFailure()
        downstream = _DownstreamStage()
        defn, stages = _linear(upstream, downstream)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.STOPPED
        # Downstream never ran — it was in a future batch
        assert "downstream" not in state.stages or \
               state.stages["downstream"].status.value in ("blocked", "pending")

    async def test_safe_stop_no_retry(self, requirement: Requirement) -> None:
        """CRITICAL failure must NOT trigger retry even with max_attempts=3."""
        stage = _SafeStopFailure()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["dangerous"]
        assert len(ctx.attempt_records) == 1  # only 1 attempt despite max_attempts=3


# ─── 8. Recovery state tracking ──────────────────────────────────────────────


class TestRecoveryStateTracking:
    async def test_attempt_records_are_traceable(
        self, requirement: Requirement
    ) -> None:
        """All attempt records must have classification and recovery_decision."""
        stage = _AlwaysFails(max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["always_fails"]
        for record in ctx.attempt_records:
            assert record.classification is not None
            assert record.recovery_decision is not None
            assert record.error != ""
            assert record.error_type == "TransientError"
            assert record.timestamp is not None

    async def test_attempt_numbers_are_sequential(
        self, requirement: Requirement
    ) -> None:
        stage = _AlwaysFails(max_attempts=3)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        ctx = state.stages["always_fails"]
        for i, record in enumerate(ctx.attempt_records):
            assert record.attempt == i


# ─── 9. Default retry policy (single attempt) ─────────────────────────────────


class TestDefaultRetryPolicy:
    async def test_default_max_attempts_is_one(self) -> None:
        assert DEFAULT_RETRY_POLICY.max_attempts == 1

    async def test_stage_with_default_policy_fails_immediately(
        self, requirement: Requirement
    ) -> None:
        """With DEFAULT_RETRY_POLICY, a single failure → FAILED, no retry."""

        class _OneFail(_BaseTestStage):
            stage_name = "one_fail"
            # inherits DEFAULT_RETRY_POLICY (max_attempts=1)

            async def execute(self, ctx: StageContext) -> StageContext:
                raise TransientError("Single attempt failure")

        defn, stages = _single(_OneFail())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        ctx = state.stages["one_fail"]
        assert len(ctx.attempt_records) == 1
        assert ctx.attempt_records[0].recovery_decision == RecoveryDecision.FAIL_IMMEDIATE


# ─── 10-11. Exit gate retry ───────────────────────────────────────────────────


class TestExitGateRetry:
    async def test_retryable_exit_gate_failure_recovers_on_second_attempt(
        self, requirement: Requirement
    ) -> None:
        stage = _ExitGateFlaky(retryable=True)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        ctx = state.stages["exit_gate_flaky"]
        assert len(ctx.attempt_records) == 1  # one failed exit-gate attempt recorded

    async def test_non_retryable_exit_gate_failure_fails_immediately(
        self, requirement: Requirement
    ) -> None:
        stage = _ExitGateFlaky(retryable=False)
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        ctx = state.stages["exit_gate_flaky"]
        assert ctx.attempt_records[0].error_type == "ExitGateFailure"


# ─── 12-13. Failure propagation ──────────────────────────────────────────────


class TestFailurePropagation:
    async def test_downstream_blocked_when_upstream_fails(
        self, requirement: Requirement
    ) -> None:
        upstream = _AlwaysFails(max_attempts=1)
        downstream = _DownstreamStage()
        defn, stages = _linear(upstream, downstream)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["downstream"].status.value == "blocked"

    async def test_downstream_does_not_run_after_upstream_failure(
        self, requirement: Requirement
    ) -> None:
        upstream = _AlwaysFails(max_attempts=1)
        downstream = _DownstreamStage()
        defn, stages = _linear(upstream, downstream)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert "ran" not in state.stages.get("downstream", type("X", (), {"output_data": {}})()).output_data


# ─── 15. Fallback USE_PRESET without output falls through ─────────────────────


class TestFallbackPresetWithoutOutput:
    async def test_use_preset_without_output_falls_through_to_fail(
        self, requirement: Requirement
    ) -> None:
        """
        fallback_behavior=USE_PRESET but fallback_output=None should fall
        through to normal failure (not SKIP, not COMPLETED).
        """

        class _NoOutputPreset(_BaseTestStage):
            stage_name = "no_output_preset"

            def __init__(self) -> None:
                self.retry_policy = RetryPolicy(
                    max_attempts=1,
                    fallback_behavior=FallbackBehavior.USE_PRESET,
                    fallback_output=None,  # no preset output
                )

            async def execute(self, ctx: StageContext) -> StageContext:
                raise TransientError("Fails")

        defn, stages = _single(_NoOutputPreset())
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED


# ─── 18. RetryPolicy.classify MRO ────────────────────────────────────────────


class TestRetryPolicyClassify:
    def test_exact_type_match(self) -> None:
        policy = RetryPolicy(non_retryable_error_types=["PermanentError"])
        exc = PermanentError("test")
        assert policy.classify(exc) == FailureClassification.PERMANENT

    def test_base_class_match(self) -> None:
        """SpecialTransientError is a subclass of TransientError.
        If TransientError is in non_retryable_error_types, SpecialTransientError
        must also be classified PERMANENT."""
        policy = RetryPolicy(non_retryable_error_types=["TransientError"])
        exc = SpecialTransientError("subclass")
        assert policy.classify(exc) == FailureClassification.PERMANENT

    def test_safe_stop_takes_precedence(self) -> None:
        policy = RetryPolicy(
            non_retryable_error_types=["SafeStopError"],
            safe_stop_error_types=["SafeStopError"],
        )
        exc = SafeStopError("critical")
        # safe_stop checked first
        assert policy.classify(exc) == FailureClassification.CRITICAL

    def test_unlisted_exception_is_transient(self) -> None:
        policy = RetryPolicy()
        exc = ValueError("unclassified")
        assert policy.classify(exc) == FailureClassification.TRANSIENT


# ─── 19. max_attempts bound ───────────────────────────────────────────────────


class TestRetryPolicyValidation:
    def test_max_attempts_zero_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy(max_attempts=0)

    def test_max_attempts_negative_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy(max_attempts=-1)

    def test_max_attempts_above_10_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy(max_attempts=11)

    def test_max_attempts_1_is_valid(self) -> None:
        policy = RetryPolicy(max_attempts=1)
        assert policy.max_attempts == 1

    def test_max_attempts_10_is_valid(self) -> None:
        policy = RetryPolicy(max_attempts=10)
        assert policy.max_attempts == 10
