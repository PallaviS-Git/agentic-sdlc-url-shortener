"""
Concrete workflow execution engine.

WorkflowEngine is the runtime that transforms a WorkflowDefinition (a DAG
describing what stages exist and their dependencies) plus a registry of
BaseStage implementations into an actual SDLC run.

Execution model
───────────────
The engine uses the DAG's get_ready_stages() query at every step to decide
which stages to launch next. Stages with no pending dependencies are
"ready" and are executed concurrently using asyncio.gather. This means:

  - Sequential execution arises naturally when a stage has a single
    predecessor that is not yet complete.
  - Parallel execution arises naturally when multiple stages share no
    dependency edge and their predecessors are all complete.
  - Synchronization is implicit: a stage with N predecessors can only
    become ready after all N predecessors complete.

Stage lifecycle
───────────────
For each stage the engine follows this sequence:

  AWAITING_GATE → (entry gate evaluation)
    ↓ passes          ↓ fails
  IN_PROGRESS       FAILED (dependents → BLOCKED)
    ↓ succeeds  ↓ raises
    ↓ (exit gate evaluation)
      ↓ passes          ↓ fails
    COMPLETED         FAILED (dependents → BLOCKED)

Not implemented in this version (planned for later steps):
  - Retries (one attempt per stage; fail-fast on any failure)
  - Rollback (no cleanup of completed stages on failure)
  - Human approval checkpoints (requires_approval is ignored)
  - Dynamic re-planning when upstream outputs change

Import chain: asyncio, orchestrator.core.* — no external service calls.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from orchestrator.core.autonomy import (
    ApprovalGateway,
    ApprovalPolicy,
    ApprovalRequest,
    AutonomyLevel,
    DEFAULT_APPROVAL_POLICY,
)
from orchestrator.core.failure import (
    DEFAULT_RETRY_POLICY,
    FailureClassification,
    FallbackBehavior,
    RecoveryDecision,
    RetryPolicy,
    StageAttemptRecord,
)
from orchestrator.core.governance import ActionContext, EnforcementDecision, PolicyEngine
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.context import ExecutionContext
from orchestrator.core.graph import WorkflowDefinition
from orchestrator.core.models import (
    GateResult,
    Requirement,
    StageContext,
    StageStatus,
    StageTransition,
    WorkflowState,
    WorkflowStatus,
)
from orchestrator.core.results import Approval, ApprovalStatus


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ─── Engine errors ────────────────────────────────────────────────────────────


class WorkflowValidationError(Exception):
    """
    Raised at WorkflowEngine construction when the definition graph is invalid.

    Attributes:
        errors: List of all validation errors found (never empty when raised).
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        summary = "; ".join(errors)
        super().__init__(
            f"Workflow validation failed ({len(errors)} error(s)): {summary}"
        )


class StageNotRegisteredError(Exception):
    """
    Raised at WorkflowEngine construction when a stage in the definition
    has no corresponding implementation in the stages registry.

    Attributes:
        stage_name: The name of the stage that is missing an implementation.
    """

    def __init__(self, stage_name: str) -> None:
        self.stage_name = stage_name
        super().__init__(
            f"Stage '{stage_name}' is declared in the WorkflowDefinition but has no "
            f"registered implementation. Add it to the 'stages' dict passed to WorkflowEngine."
        )


# ─── Engine ───────────────────────────────────────────────────────────────────


class WorkflowEngine:
    """
    Executes a WorkflowDefinition by coordinating registered BaseStage implementations.

    Construction-time validation ensures errors surface immediately, not mid-run:
      - Graph topology (cycles, unknown stage references in dependencies)
      - Stage registry completeness (every stage in the definition has an impl)

    Run-time execution:
      - Traverses the DAG using repeated ready-stage queries
      - Executes all ready stages concurrently via asyncio.gather
      - Enforces entry and exit gates; fails stages that don't pass
      - Propagates stage outputs into ExecutionContext for downstream stages
      - Marks un-executed stages as BLOCKED when the workflow fails
    """

    def __init__(
        self,
        definition: WorkflowDefinition,
        stages: dict[str, BaseStage],
        *,
        approval_gateway: ApprovalGateway | None = None,
        approval_policy: ApprovalPolicy = DEFAULT_APPROVAL_POLICY,
        final_approval_required: bool = False,
        default_retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        policy_engine: PolicyEngine | None = None,
    ) -> None:
        """
        Initialise the engine, validating graph and registry immediately.

        Args:
            definition:              Immutable DAG describing the workflow topology.
            stages:                  Map of stage_name → BaseStage implementation.
                                     Must contain an entry for every stage in
                                     definition.stages. Extra entries are ignored.
            approval_gateway:        Gateway used to obtain human approval decisions.
                                     When None and a stage requires approval, that
                                     stage fails with status TIMED_OUT (fail-safe).
            approval_policy:         Rules mapping ActionImpact → AutonomyLevel.
                                     Defaults to DEFAULT_APPROVAL_POLICY
                                     (HIGH_IMPACT → approval; CRITICAL → blocked).
            final_approval_required: When True, a final human quality-control
                                     checkpoint is requested after all stages
                                     complete. The workflow stays AWAITING_APPROVAL
                                     until the gateway responds. If rejected or no
                                     gateway, the workflow is marked FAILED.

        Raises:
            WorkflowValidationError: Graph contains cycles or unknown stage references.
            StageNotRegisteredError: A definition stage has no implementation.
        """
        self.definition = definition
        self.stages = stages
        self.approval_gateway = approval_gateway
        self.approval_policy = approval_policy
        self.final_approval_required = final_approval_required
        self.default_retry_policy = default_retry_policy
        self.policy_engine = policy_engine
        self._validate()

    def _validate(self) -> None:
        """Validate graph and registry. Raises on the first category of error found."""
        # Graph topology first — most common source of misconfiguration
        graph_errors = self.definition.validate_graph()
        if graph_errors:
            raise WorkflowValidationError(graph_errors)

        # Registry completeness — every declared stage must have an implementation
        for stage_name in self.definition.stages:
            if stage_name not in self.stages:
                raise StageNotRegisteredError(stage_name)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self, requirement: Requirement) -> WorkflowState:
        """
        Execute the full SDLC workflow for the given requirement.

        Traverses the dependency DAG in iterations. Each iteration:
          1. Queries the DAG for currently-ready stages.
          2. Launches all ready stages concurrently (asyncio.gather).
          3. Updates completed/failed sets.
          4. If any stage failed: stops scheduling (fail-fast).

        The loop ends when either:
          - All stages complete (→ COMPLETED)
          - A stage fails (→ FAILED; dependents marked BLOCKED)

        Args:
            requirement: Normalized requirement driving this workflow run.

        Returns:
            Final WorkflowState with status COMPLETED or FAILED.
        """
        state, exec_ctx = self._init_state(requirement)
        completed: set[str] = set()
        failed: set[str] = set()
        all_stages = set(self.definition.stages)

        while True:
            # ── Determine what can run now ─────────────────────────────────
            ready = self.definition.get_ready_stages(completed=completed)

            if not ready:
                break  # Either all done, or blocked by failures

            state.add_audit_entry(
                "stages_scheduled",
                details={"stages": ready, "iteration_completed": sorted(completed)},
            )

            # ── Execute all ready stages concurrently ──────────────────────
            # return_exceptions=True prevents one stage's exception from
            # cancelling its siblings. _execute_stage should never raise
            # (it catches all exceptions internally), but this is defensive.
            raw_results = await asyncio.gather(
                *[self._execute_stage(sn, state, exec_ctx) for sn in ready],
                return_exceptions=True,
            )

            for stage_name, result in zip(ready, raw_results):
                if isinstance(result, BaseException):
                    # Unhandled exception from engine internals (engine bug)
                    failed.add(stage_name)
                    state.add_audit_entry(
                        "stage_engine_exception",
                        stage=stage_name,
                        details={
                            "error": str(result),
                            "type": type(result).__name__,
                        },
                    )
                elif result:
                    completed.add(stage_name)
                else:
                    failed.add(stage_name)

            if failed or state.safe_stopped:
                # Fail fast (or safe-stop): stop scheduling new stages
                break

        # ── Post-loop: final human QC checkpoint (when all stages passed) ───
        if not failed and not state.safe_stopped and self.final_approval_required:
            qc_ok = await self._final_qc_checkpoint(state, exec_ctx)
            if not qc_ok:
                state.status = WorkflowStatus.FAILED
                state.add_audit_entry(
                    "final_qc_rejected",
                    details={"reason": "Final human QC checkpoint was not approved"},
                )
                state.updated_at = _now()
                return state

        # ── Post-loop: determine final workflow status ─────────────────────
        self._finalise(state, all_stages, completed, failed)
        state.completed_at = _now()
        state.updated_at = state.completed_at
        return state

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _init_state(
        self, requirement: Requirement
    ) -> tuple[WorkflowState, ExecutionContext]:
        """Create initial WorkflowState and ExecutionContext for a new run."""
        state = WorkflowState(
            requirement=requirement,
            status=WorkflowStatus.RUNNING,
            workflow_definition=self.definition,
        )
        exec_ctx = ExecutionContext(workflow_id=state.id)
        state.execution_context = exec_ctx
        state.add_audit_entry(
            "workflow_started",
            details={
                "workflow": self.definition.name,
                "version": self.definition.version,
                "stage_count": len(self.definition.stages),
            },
        )
        return state, exec_ctx

    def _finalise(
        self,
        state: WorkflowState,
        all_stages: set[str],
        completed: set[str],
        failed: set[str],
    ) -> None:
        """Set final WorkflowStatus and mark un-executed stages as BLOCKED."""
        # Safe-stop: status already STOPPED; just block un-reached stages.
        if state.safe_stopped:
            untouched = all_stages - set(state.stages.keys())
            for stage_name in sorted(untouched):
                state.set_stage(
                    StageContext(stage_name=stage_name, status=StageStatus.BLOCKED)
                )
            state.add_audit_entry(
                "workflow_safe_stopped",
                details={"reason": state.safe_stop_reason},
            )
            return

        if failed:
            # Mark every stage that never got a chance to run
            unexecuted = all_stages - completed - failed
            for stage_name in sorted(unexecuted):
                state.set_stage(
                    StageContext(
                        stage_name=stage_name,
                        status=StageStatus.BLOCKED,
                    )
                )
            state.status = WorkflowStatus.FAILED
            state.add_audit_entry(
                "workflow_failed",
                details={
                    "failed_stages": sorted(failed),
                    "completed_stages": sorted(completed),
                    "blocked_stages": sorted(unexecuted),
                },
            )
        elif completed == all_stages:
            state.status = WorkflowStatus.COMPLETED
            state.add_audit_entry(
                "workflow_completed",
                details={"stages_completed": len(completed)},
            )
        else:
            # Defensive: should not reach here in normal operation
            state.status = WorkflowStatus.FAILED
            state.add_audit_entry(
                "workflow_incomplete",
                details={
                    "completed": sorted(completed),
                    "all_stages": sorted(all_stages),
                },
            )

    async def _execute_stage(
        self,
        stage_name: str,
        state: WorkflowState,
        exec_ctx: ExecutionContext,
    ) -> bool:
        """
        Execute a single stage through the entry_gate → execute → exit_gate lifecycle.

        This method NEVER raises — all exceptions from stage implementations are
        caught and recorded in the stage context. A False return means the stage
        failed; the reason is always in state.stages[stage_name].error.

        Thread-safety note: concurrent calls from asyncio.gather share `state` and
        `exec_ctx`. Since Python's asyncio is single-threaded (cooperative), mutations
        at non-await points are safe. Each stage writes to its own unique key in
        state.stages, so dict writes do not conflict.

        Args:
            stage_name: Name of the stage to execute.
            state:      Shared WorkflowState; mutated to record stage progress.
            exec_ctx:   Shared ExecutionContext; updated only on successful completion.

        Returns:
            True  → stage completed successfully (both gates passed, execute succeeded).
            False → stage failed at any point (entry gate, execute, or exit gate).
        """
        stage_impl = self.stages[stage_name]
        # Per-stage policy overrides the engine default
        policy = getattr(stage_impl, "retry_policy", self.default_retry_policy)
        preds = self.definition.get_predecessors(stage_name)
        input_data = exec_ctx.snapshot_for_stage(preds)

        stage_ctx = StageContext(
            stage_name=stage_name,
            status=StageStatus.AWAITING_GATE,
            input_data=input_data,
            started_at=_now(),
            max_attempts=policy.max_attempts,
        )
        state.set_stage(stage_ctx)

        # ── Entry gate ────────────────────────────────────────────────────────
        state.add_audit_entry("entry_gate_evaluating", stage=stage_name)

        try:
            entry_result: GateResult = await stage_impl.entry_gate(stage_ctx)
        except Exception as exc:
            return self._fail_stage(
                stage_ctx, state,
                event="entry_gate_exception",
                error=f"Entry gate raised an unexpected exception: {exc}",
                details={"error": str(exc)},
            )

        stage_ctx.entry_gate_results.append(entry_result)
        state.set_stage(stage_ctx)

        if not entry_result.passed:
            return self._fail_stage(
                stage_ctx, state,
                event="entry_gate_failed",
                error=(
                    f"Entry gate '{entry_result.gate_name}' did not pass: "
                    f"{entry_result.reason}"
                ),
                details={
                    "gate": entry_result.gate_name,
                    "reason": entry_result.reason,
                },
            )

        state.add_audit_entry("entry_gate_passed", stage=stage_name)

        # ── Record stage transition (lineage) ─────────────────────────────────
        # Captures what made this stage become ready — its predecessor stages.
        # Stage implementations may later set driving_decision_id on the
        # transition by annotating their decisions with downstream_impacts;
        # WorkflowLineage.get_decision_for_transition() resolves that.
        transition_reason = (
            f"Predecessors completed: {preds}" if preds else "Root stage; no predecessors"
        )
        state.add_stage_transition(
            StageTransition(
                stage_name=stage_name,
                predecessor_stages=preds,
                transition_reason=transition_reason,
                started_at=_now(),
            )
        )

        # ── CRITICAL action block ─────────────────────────────────────────────
        # CRITICAL (HUMAN_ONLY) stages are blocked before any gateway call.
        # Agents may only RECOMMEND critical actions; they cannot execute them.
        if (
            self.approval_policy.required_autonomy(stage_impl.action_impact)
            == AutonomyLevel.HUMAN_ONLY
        ):
            return self._fail_stage(
                stage_ctx,
                state,
                event="critical_action_blocked",
                error=(
                    f"Stage '{stage_name}' has CRITICAL impact "
                    f"(action_impact={stage_impl.action_impact.value}). "
                    "Agent execution is not permitted for HUMAN_ONLY actions. "
                    "An operator must perform this action manually."
                ),
                details={"impact": stage_impl.action_impact.value, "autonomy": "human_only"},
            )

        # ── Governance gate ───────────────────────────────────────────────────
        # Evaluate all registered policies before any execution begins.
        # A BLOCK decision fails the stage immediately.
        # A REQUIRE_APPROVAL decision is merged into the approval checkpoint below.
        # WARN and ALLOW decisions are recorded but do not change the flow.
        policy_requires_approval = False
        if self.policy_engine is not None:
            action_ctx = ActionContext(
                workflow_id=state.id,
                stage_name=stage_name,
                action_impact=stage_impl.action_impact,
                action_type=stage_impl.high_impact_action_type,
                metadata=dict(getattr(stage_impl, "policy_metadata", {})),
            )
            eval_record = self.policy_engine.evaluate(action_ctx)
            state.policy_evaluations.append(eval_record)
            state.add_audit_entry(
                "policy_evaluated",
                stage=stage_name,
                details={
                    "decision": eval_record.final_decision.value,
                    "violations": len(eval_record.violations),
                    "blocked_by": [
                        v.policy_id
                        for v in eval_record.violations
                        if v.decision == EnforcementDecision.BLOCK
                    ],
                },
            )
            if eval_record.final_decision == EnforcementDecision.BLOCK:
                blocked_ids = [
                    v.policy_id
                    for v in eval_record.violations
                    if v.decision == EnforcementDecision.BLOCK
                ]
                return self._fail_stage(
                    stage_ctx,
                    state,
                    event="policy_blocked",
                    error=(
                        f"Stage '{stage_name}' blocked by governance policy. "
                        f"Violated policies: {blocked_ids}. "
                        "Review PolicyEvaluationRecord in WorkflowState.policy_evaluations."
                    ),
                    details={
                        "policies": blocked_ids,
                        "violations": [
                            {"policy_id": v.policy_id, "message": v.message}
                            for v in eval_record.violations
                        ],
                    },
                )
            if eval_record.final_decision == EnforcementDecision.REQUIRE_APPROVAL:
                policy_requires_approval = True

        # ── Approval checkpoint ───────────────────────────────────────────────
        # Request human approval when the stage explicitly requires it OR when
        # the active autonomy policy demands it based on action_impact OR when
        # the governance policy engine requires it.
        needs_approval = (
            stage_impl.requires_approval
            or self.approval_policy.requires_human_approval(stage_impl.action_impact)
            or policy_requires_approval
        )
        if needs_approval:
            stage_ctx.status = StageStatus.AWAITING_APPROVAL
            state.set_stage(stage_ctx)
            approved = await self._request_approval_checkpoint(
                stage_name, stage_impl, state, exec_ctx
            )
            if not approved:
                return self._fail_stage(
                    stage_ctx,
                    state,
                    event="approval_rejected",
                    error=(
                        f"Stage '{stage_name}' was not approved to execute. "
                        "Check WorkflowState.approvals for the rejection record."
                    ),
                    details={"stage": stage_name, "requires_approval": stage_impl.requires_approval},
                )

        # ── Execute + exit-gate retry loop ───────────────────────────────────
        # Entry gate, lineage, CRITICAL block, and approval are all outside
        # the loop — they run exactly once regardless of retry count.
        stage_ctx.status = StageStatus.IN_PROGRESS
        state.set_stage(stage_ctx)

        succeeded = False
        last_error_msg: str = ""

        for attempt in range(policy.max_attempts):
            stage_ctx.attempt = attempt
            # Clear exit-gate results from any previous attempt so
            # properties like `exit_passed` reflect only the latest attempt.
            if attempt > 0:
                stage_ctx.exit_gate_results.clear()

            if attempt == 0:
                state.add_audit_entry("stage_started", stage=stage_name)
            else:
                state.add_audit_entry(
                    "stage_retrying",
                    stage=stage_name,
                    details={"attempt": attempt + 1, "max": policy.max_attempts},
                )

            # ── Execute ───────────────────────────────────────────────────────
            exec_exc: Exception | None = None
            try:
                stage_ctx = await stage_impl.execute(stage_ctx)
            except Exception as exc:
                exec_exc = exc

            if exec_exc is not None:
                classification = policy.classify(exec_exc)
                record = StageAttemptRecord(
                    attempt=attempt,
                    error=str(exec_exc),
                    error_type=type(exec_exc).__name__,
                    classification=classification,
                )

                if classification == FailureClassification.CRITICAL:
                    record.recovery_decision = RecoveryDecision.SAFE_STOP
                    stage_ctx.attempt_records.append(record)
                    state.set_stage(stage_ctx)
                    self._trigger_safe_stop(stage_name, stage_ctx, state, str(exec_exc))
                    return False

                can_retry = (
                    classification == FailureClassification.TRANSIENT
                    and attempt < policy.max_attempts - 1
                )
                if can_retry:
                    record.recovery_decision = RecoveryDecision.RETRY
                    stage_ctx.attempt_records.append(record)
                    state.set_stage(stage_ctx)
                    continue  # next attempt

                # PERMANENT or TRANSIENT-exhausted
                record.recovery_decision = RecoveryDecision.FAIL_IMMEDIATE
                stage_ctx.attempt_records.append(record)
                state.set_stage(stage_ctx)
                last_error_msg = str(exec_exc)
                break  # exit retry loop → failure handling

            # ── Exit gate ─────────────────────────────────────────────────────
            state.add_audit_entry("exit_gate_evaluating", stage=stage_name)

            exit_exc: Exception | None = None
            exit_result: GateResult | None = None
            try:
                exit_result = await stage_impl.exit_gate(stage_ctx)
            except Exception as exc:
                exit_exc = exc

            if exit_exc is not None:
                # Treat exit-gate exception as a transient error
                err_str = str(exit_exc)
                can_retry = (
                    policy.exit_gate_failure_retryable
                    and attempt < policy.max_attempts - 1
                )
                record = StageAttemptRecord(
                    attempt=attempt,
                    error=f"Exit gate raised: {err_str}",
                    error_type=type(exit_exc).__name__,
                    classification=FailureClassification.TRANSIENT,
                    recovery_decision=(
                        RecoveryDecision.RETRY if can_retry else RecoveryDecision.FAIL_IMMEDIATE
                    ),
                )
                stage_ctx.attempt_records.append(record)
                state.set_stage(stage_ctx)
                if can_retry:
                    state.add_audit_entry("exit_gate_retrying", stage=stage_name)
                    continue
                last_error_msg = err_str
                break

            # exit_result is always set here (no exception)
            assert exit_result is not None
            stage_ctx.exit_gate_results.append(exit_result)

            if exit_result.passed:
                state.add_audit_entry("exit_gate_passed", stage=stage_name)
                succeeded = True
                break

            # Exit gate returned failed (not raised)
            gate_err = (
                f"Exit gate '{exit_result.gate_name}' did not pass: "
                f"{exit_result.reason}"
            )
            state.add_audit_entry(
                "exit_gate_failed",
                stage=stage_name,
                details={"gate": exit_result.gate_name, "reason": exit_result.reason},
            )
            can_retry = (
                policy.exit_gate_failure_retryable
                and attempt < policy.max_attempts - 1
            )
            classification = (
                FailureClassification.TRANSIENT
                if policy.exit_gate_failure_retryable
                else FailureClassification.PERMANENT
            )
            record = StageAttemptRecord(
                attempt=attempt,
                error=gate_err,
                error_type="ExitGateFailure",
                classification=classification,
                recovery_decision=(
                    RecoveryDecision.RETRY if can_retry else RecoveryDecision.FAIL_IMMEDIATE
                ),
            )
            stage_ctx.attempt_records.append(record)
            state.set_stage(stage_ctx)
            if can_retry:
                state.add_audit_entry("exit_gate_retrying", stage=stage_name)
                continue
            last_error_msg = gate_err
            break

        # ── Post-retry: success path ──────────────────────────────────────────
        if succeeded:
            stage_ctx.status = StageStatus.COMPLETED
            stage_ctx.completed_at = _now()
            state.set_stage(stage_ctx)
            exec_ctx.record_stage_output(
                stage_name=stage_name,
                output_data=stage_ctx.output_data,
                artifacts=stage_ctx.artifacts,
                decisions=stage_ctx.decisions,
                risks=stage_ctx.risks,
            )
            state.add_audit_entry(
                "stage_completed",
                stage=stage_name,
                details={
                    "attempts": stage_ctx.attempt + 1,
                    "artifacts": len(stage_ctx.artifacts),
                    "decisions": len(stage_ctx.decisions),
                    "risks": len(stage_ctx.risks),
                },
            )
            return True

        # ── Post-retry: safe-stop was already handled (returns False inside loop)
        if state.safe_stopped:
            return False

        # ── Post-retry: fallback ──────────────────────────────────────────────
        if policy.fallback_behavior != FallbackBehavior.FAIL:
            fallback_ok = await self._apply_fallback(
                stage_name, stage_ctx, policy, state, exec_ctx
            )
            if fallback_ok:
                return True

        # ── Post-retry: rollback ──────────────────────────────────────────────
        if policy.rollback_on_failure:
            await self._do_rollback(stage_name, stage_impl, stage_ctx, state)
            # Rollback sets stage status to ROLLED_BACK; propagate error and return.
            stage_ctx.error = last_error_msg
            state.set_stage(stage_ctx)
            return False

        return self._fail_stage(
            stage_ctx,
            state,
            event="stage_failed_all_attempts",
            error=(
                f"Stage '{stage_name}' failed after {stage_ctx.attempt + 1} "
                f"attempt(s): {last_error_msg}"
            ),
            details={
                "attempts": stage_ctx.attempt + 1,
                "error": last_error_msg,
            },
        )

    def _trigger_safe_stop(
        self,
        stage_name: str,
        stage_ctx: StageContext,
        state: WorkflowState,
        error: str,
    ) -> None:
        """
        Halt the entire workflow immediately due to a CRITICAL failure.

        Safe-stop means *preserve state for investigation* — do not rollback,
        do not schedule more stages. The workflow status transitions to STOPPED.
        """
        stage_ctx.status = StageStatus.FAILED
        stage_ctx.error = error
        stage_ctx.completed_at = _now()
        state.set_stage(stage_ctx)
        state.safe_stopped = True
        state.safe_stop_reason = (
            f"Stage '{stage_name}' raised a CRITICAL exception: {error}"
        )
        state.status = WorkflowStatus.STOPPED
        state.add_audit_entry(
            "safe_stop_triggered",
            stage=stage_name,
            details={"error": error},
        )

    async def _apply_fallback(
        self,
        stage_name: str,
        stage_ctx: StageContext,
        policy: RetryPolicy,
        state: WorkflowState,
        exec_ctx: ExecutionContext,
    ) -> bool:
        """
        Apply the configured fallback behavior after all retries are exhausted.

        Returns True when fallback succeeds (stage is considered done),
        False when the fallback cannot be applied (FAIL behaviour).
        """
        if policy.fallback_behavior == FallbackBehavior.SKIP:
            stage_ctx.status = StageStatus.SKIPPED
            stage_ctx.fallback_used = True
            stage_ctx.completed_at = _now()
            state.set_stage(stage_ctx)
            state.add_audit_entry(
                "stage_skipped_fallback",
                stage=stage_name,
                details={"fallback": "skip"},
            )
            exec_ctx.record_stage_output(stage_name, {}, [], [], [])
            return True

        if (
            policy.fallback_behavior == FallbackBehavior.USE_PRESET
            and policy.fallback_output is not None
        ):
            stage_ctx.status = StageStatus.COMPLETED
            stage_ctx.output_data = dict(policy.fallback_output)
            stage_ctx.fallback_used = True
            stage_ctx.completed_at = _now()
            state.set_stage(stage_ctx)
            state.add_audit_entry(
                "stage_fallback_applied",
                stage=stage_name,
                details={"fallback": "use_preset"},
            )
            exec_ctx.record_stage_output(stage_name, stage_ctx.output_data, [], [], [])
            return True

        return False

    async def _do_rollback(
        self,
        stage_name: str,
        stage_impl: BaseStage,
        stage_ctx: StageContext,
        state: WorkflowState,
    ) -> None:
        """
        Call stage.rollback(), mark the stage as ROLLED_BACK, and record
        the rollback in WorkflowState.rolled_back_stages.

        Rollback failures are caught and logged — they do not prevent the
        workflow from continuing with its failure-path handling.
        """
        state.add_audit_entry("rollback_started", stage=stage_name)
        try:
            stage_ctx = await stage_impl.rollback(stage_ctx)
            stage_ctx.rollback_performed = True
            stage_ctx.status = StageStatus.ROLLED_BACK
            stage_ctx.completed_at = _now()
            state.set_stage(stage_ctx)
            state.rolled_back_stages.append(stage_name)
            state.add_audit_entry("rollback_completed", stage=stage_name)
        except Exception as exc:
            state.add_audit_entry(
                "rollback_failed",
                stage=stage_name,
                details={"error": str(exc)},
            )

    async def _request_approval_checkpoint(
        self,
        stage_name: str,
        stage_impl: BaseStage,
        state: WorkflowState,
        exec_ctx: ExecutionContext,
    ) -> bool:
        """
        Request human approval before a stage executes.

        Returns True if approved, False if rejected or if no gateway is
        configured (fail-safe: absent a gateway, protected stages are blocked).

        Side effects:
          - Adds an Approval record to WorkflowState.approvals.
          - Adds audit entries for approval_requested and approval_resolved.
          - Updates the stage status to AWAITING_APPROVAL while waiting
            and restores it on return (caller sets IN_PROGRESS if approved).
        """
        # Build the request with upstream context
        upstream_ids = [a.id for a in exec_ctx.artifacts]
        request = ApprovalRequest(
            workflow_id=state.id,
            stage_name=stage_name,
            requesting_agent=stage_name,
            stage_summary=(
                f"Stage '{stage_name}' requires human approval before execution. "
                f"Impact level: {stage_impl.action_impact.value}. "
                f"Action type: {stage_impl.high_impact_action_type or 'unspecified'}."
            ),
            risk_context={
                "action_impact": stage_impl.action_impact.value,
                "high_impact_action_type": (
                    stage_impl.high_impact_action_type.value
                    if stage_impl.high_impact_action_type
                    else None
                ),
                "requires_approval_explicit": stage_impl.requires_approval,
                "upstream_decisions": len(exec_ctx.decisions),
                "upstream_risks": len(exec_ctx.risks),
            },
            upstream_artifact_ids=upstream_ids,
        )

        state.add_audit_entry(
            "approval_requested",
            stage=stage_name,
            details={
                "request_id": request.id,
                "impact": stage_impl.action_impact.value,
                "gateway": type(self.approval_gateway).__name__ if self.approval_gateway else "none",
            },
        )

        # No gateway → fail-safe (cannot obtain approval)
        if self.approval_gateway is None:
            approval = Approval(
                workflow_id=state.id,
                stage_name=stage_name,
                summary=request.stage_summary,
                status=ApprovalStatus.TIMED_OUT,
                impact_level=stage_impl.action_impact.value,
                action_type=(
                    stage_impl.high_impact_action_type.value
                    if stage_impl.high_impact_action_type
                    else None
                ),
                decision_rationale="No approval gateway configured; approval cannot be obtained.",
            )
            state.add_approval(approval)
            state.add_audit_entry(
                "approval_timed_out",
                stage=stage_name,
                details={"reason": "no_gateway", "request_id": request.id},
            )
            return False

        # Gateway present → request decision
        decision = await self.approval_gateway.request_approval(request)

        approval = Approval(
            workflow_id=state.id,
            stage_name=stage_name,
            summary=request.stage_summary,
            status=ApprovalStatus.APPROVED if decision.approved else ApprovalStatus.REJECTED,
            approver=decision.approver,
            notes=decision.rationale,
            decided_at=decision.decided_at,
            impact_level=stage_impl.action_impact.value,
            action_type=(
                stage_impl.high_impact_action_type.value
                if stage_impl.high_impact_action_type
                else None
            ),
            decision_rationale=decision.rationale,
            escalation_level=decision.escalation_level,
            is_override=decision.is_override,
        )
        state.add_approval(approval)

        state.add_audit_entry(
            "approval_resolved",
            stage=stage_name,
            details={
                "request_id": request.id,
                "approved": decision.approved,
                "approver": decision.approver,
                "escalation_level": decision.escalation_level,
            },
        )
        return decision.approved

    async def _final_qc_checkpoint(
        self,
        state: WorkflowState,
        exec_ctx: ExecutionContext,
    ) -> bool:
        """
        Request a final human quality-control review of the completed workflow.

        Called after all stages have completed successfully, only when
        WorkflowEngine.final_approval_required=True.

        Returns True if the QC checkpoint is approved, False otherwise.
        The caller (run()) is responsible for marking the workflow FAILED
        when this returns False.
        """
        request = ApprovalRequest(
            workflow_id=state.id,
            stage_name="__final_qc__",
            requesting_agent="orchestrator",
            stage_summary=(
                f"Final human quality-control review of workflow '{self.definition.name}'. "
                f"All {len(self.definition.stages)} stages completed. "
                "Please review all artifacts, decisions, and risks before approving."
            ),
            risk_context={
                "stages_completed": len(state.completed_stage_names),
                "total_artifacts": len(exec_ctx.artifacts),
                "total_decisions": len(exec_ctx.decisions),
                "total_risks": len(exec_ctx.risks),
            },
            upstream_artifact_ids=[a.id for a in exec_ctx.artifacts],
        )

        state.status = WorkflowStatus.AWAITING_APPROVAL
        state.add_audit_entry(
            "final_qc_requested",
            details={
                "request_id": request.id,
                "gateway": type(self.approval_gateway).__name__ if self.approval_gateway else "none",
            },
        )

        if self.approval_gateway is None:
            approval = Approval(
                workflow_id=state.id,
                stage_name="__final_qc__",
                summary=request.stage_summary,
                status=ApprovalStatus.TIMED_OUT,
                decision_rationale="No approval gateway configured for final QC.",
            )
            state.add_approval(approval)
            state.add_audit_entry(
                "final_qc_timed_out",
                details={"reason": "no_gateway"},
            )
            return False

        decision = await self.approval_gateway.request_approval(request)

        approval = Approval(
            workflow_id=state.id,
            stage_name="__final_qc__",
            summary=request.stage_summary,
            status=ApprovalStatus.APPROVED if decision.approved else ApprovalStatus.REJECTED,
            approver=decision.approver,
            notes=decision.rationale,
            decided_at=decision.decided_at,
            decision_rationale=decision.rationale,
            escalation_level=decision.escalation_level,
            is_override=decision.is_override,
        )
        state.add_approval(approval)

        state.add_audit_entry(
            "final_qc_resolved",
            details={
                "approved": decision.approved,
                "approver": decision.approver,
            },
        )
        return decision.approved

    def _fail_stage(
        self,
        stage_ctx: StageContext,
        state: WorkflowState,
        event: str,
        error: str,
        details: dict,
    ) -> bool:
        """
        Mark a stage as FAILED, record the error, emit an audit event.

        Returns False so callers can 'return self._fail_stage(...)' cleanly.
        """
        stage_ctx.status = StageStatus.FAILED
        stage_ctx.error = error
        stage_ctx.completed_at = _now()
        state.set_stage(stage_ctx)
        state.add_audit_entry(event, stage=stage_ctx.stage_name, details=details)
        return False

    def __repr__(self) -> str:
        return (
            f"WorkflowEngine("
            f"workflow={self.definition.name!r}, "
            f"stages={len(self.stages)})"
        )
