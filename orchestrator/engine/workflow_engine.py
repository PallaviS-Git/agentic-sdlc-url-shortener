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

from orchestrator.core.base_stage import BaseStage
from orchestrator.core.context import ExecutionContext
from orchestrator.core.graph import WorkflowDefinition
from orchestrator.core.models import (
    GateResult,
    Requirement,
    StageContext,
    StageStatus,
    WorkflowState,
    WorkflowStatus,
)


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
    ) -> None:
        """
        Initialise the engine, validating graph and registry immediately.

        Args:
            definition: Immutable DAG describing the workflow topology.
            stages:     Map of stage_name → BaseStage implementation.
                        Must contain an entry for every stage in definition.stages.
                        Extra entries (not in the definition) are silently ignored.

        Raises:
            WorkflowValidationError: Graph contains cycles or unknown stage references.
            StageNotRegisteredError: A definition stage has no implementation.
        """
        self.definition = definition
        self.stages = stages
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

            if failed:
                # Fail fast: stop scheduling new stages
                break

        # ── Post-loop: determine final workflow status ─────────────────────
        self._finalise(state, all_stages, completed, failed)
        state.updated_at = _now()
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
        preds = self.definition.get_predecessors(stage_name)
        input_data = exec_ctx.snapshot_for_stage(preds)

        stage_ctx = StageContext(
            stage_name=stage_name,
            status=StageStatus.AWAITING_GATE,
            input_data=input_data,
            started_at=_now(),
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

        # ── Execute ───────────────────────────────────────────────────────────
        stage_ctx.status = StageStatus.IN_PROGRESS
        state.set_stage(stage_ctx)
        state.add_audit_entry("stage_started", stage=stage_name)

        try:
            stage_ctx = await stage_impl.execute(stage_ctx)
        except Exception as exc:
            return self._fail_stage(
                stage_ctx, state,
                event="stage_execution_failed",
                error=f"Stage execution raised an exception: {exc}",
                details={"error": str(exc)},
            )

        # ── Exit gate ─────────────────────────────────────────────────────────
        state.add_audit_entry("exit_gate_evaluating", stage=stage_name)

        try:
            exit_result: GateResult = await stage_impl.exit_gate(stage_ctx)
        except Exception as exc:
            return self._fail_stage(
                stage_ctx, state,
                event="exit_gate_exception",
                error=f"Exit gate raised an unexpected exception: {exc}",
                details={"error": str(exc)},
            )

        stage_ctx.exit_gate_results.append(exit_result)

        if not exit_result.passed:
            return self._fail_stage(
                stage_ctx, state,
                event="exit_gate_failed",
                error=(
                    f"Exit gate '{exit_result.gate_name}' did not pass: "
                    f"{exit_result.reason}"
                ),
                details={
                    "gate": exit_result.gate_name,
                    "reason": exit_result.reason,
                },
            )

        state.add_audit_entry("exit_gate_passed", stage=stage_name)

        # ── Successful completion ─────────────────────────────────────────────
        stage_ctx.status = StageStatus.COMPLETED
        stage_ctx.completed_at = _now()
        state.set_stage(stage_ctx)

        # Propagate outputs into the shared ExecutionContext so downstream
        # stages can read them via snapshot_for_stage().
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
                "artifacts": len(stage_ctx.artifacts),
                "decisions": len(stage_ctx.decisions),
                "risks": len(stage_ctx.risks),
            },
        )
        return True

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
