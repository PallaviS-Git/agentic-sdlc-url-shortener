"""
Tests for the WorkflowEngine — DAG-driven SDLC execution engine.

Required coverage (per specification):
  1. Valid dependency graph       → workflow runs to completion
  2. Invalid dependency           → WorkflowValidationError at construction
  3. Circular dependency          → WorkflowValidationError at construction
  4. Parallel branches            → both branches execute and complete
  5. Synchronization              → convergence stage waits for all predecessors
  6. Entry gate failure           → stage + workflow marked FAILED; dependents BLOCKED
  7. Exit gate failure            → stage fails after executing; dependents BLOCKED

Also covers:
  - Stage registry validation (StageNotRegisteredError)
  - Context propagation between stages (output → input)
  - Sequential execution order respected
  - Audit trail completeness
  - Blocked stages recorded in WorkflowState

No I/O, no network, no DB. asyncio_mode=auto (pytest.ini) handles async tests.
"""
from __future__ import annotations

import pytest

from orchestrator.core.base_stage import BaseStage
from orchestrator.core.context import ExecutionContext
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.models import (
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    StageStatus,
    WorkflowStatus,
)
from orchestrator.engine.workflow_engine import (
    StageNotRegisteredError,
    WorkflowEngine,
    WorkflowValidationError,
)


# ─── Test doubles ─────────────────────────────────────────────────────────────


class _SpyStage(BaseStage):
    """
    Configurable BaseStage test double.

    Records which stages were executed (via a shared log list) and can be
    configured to fail at the entry gate, exit gate, or during execute().
    """

    def __init__(
        self,
        name: str,
        *,
        log: list[str] | None = None,
        entry_pass: bool = True,
        exit_pass: bool = True,
        output: dict | None = None,
        raises_on_execute: bool = False,
    ) -> None:
        self.stage_name = name
        self._log = log if log is not None else []
        self._entry_pass = entry_pass
        self._exit_pass = exit_pass
        self._output = output or {}
        self._raises_on_execute = raises_on_execute

    async def entry_gate(self, context: StageContext) -> GateResult:
        return GateResult(
            gate_name=f"{self.stage_name}_entry",
            passed=self._entry_pass,
            reason=None if self._entry_pass else "Test: entry gate configured to fail",
        )

    async def execute(self, context: StageContext) -> StageContext:
        if self._raises_on_execute:
            raise RuntimeError(
                f"Stage '{self.stage_name}' is configured to raise on execute"
            )
        self._log.append(self.stage_name)
        context.output_data.update(self._output)
        context.output_data[f"{self.stage_name}_executed"] = True
        return context

    async def exit_gate(self, context: StageContext) -> GateResult:
        return GateResult(
            gate_name=f"{self.stage_name}_exit",
            passed=self._exit_pass,
            reason=None if self._exit_pass else "Test: exit gate configured to fail",
        )

    async def rollback(self, context: StageContext) -> StageContext:
        return context


class _CapturingStage(BaseStage):
    """
    BaseStage test double that captures its input_data for assertion.

    Used to verify that upstream stage output propagates as downstream input.
    """

    def __init__(self, name: str) -> None:
        self.stage_name = name
        self.received_input: dict = {}

    async def entry_gate(self, context: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_entry", passed=True)

    async def execute(self, context: StageContext) -> StageContext:
        self.received_input.update(context.input_data)
        return context

    async def exit_gate(self, context: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_exit", passed=True)

    async def rollback(self, context: StageContext) -> StageContext:
        return context


# ─── Builder helpers ──────────────────────────────────────────────────────────


def _req() -> Requirement:
    return Requirement(
        title="Test requirement",
        raw_text="Build a URL shortener",
        requirement_type=RequirementType.GREENFIELD,
    )


def _linear_def(stages: list[str]) -> WorkflowDefinition:
    """stages[0] → stages[1] → ... → stages[-1]"""
    deps = [
        StageDependency(from_stage=stages[i], to_stage=stages[i + 1])
        for i in range(len(stages) - 1)
    ]
    return WorkflowDefinition(name="linear", description="", stages=stages, dependencies=deps)


def _spy_registry(
    names: list[str],
    log: list[str] | None = None,
    **overrides: _SpyStage,
) -> dict[str, BaseStage]:
    """Build a stage registry. Pass keyword overrides for specific stages."""
    registry: dict[str, BaseStage] = {n: _SpyStage(n, log=log) for n in names}
    registry.update(overrides)
    return registry


# ─── 1. Construction validation ───────────────────────────────────────────────


@pytest.mark.unit
class TestWorkflowEngineConstruction:
    """All validation happens at construction time — errors surface immediately."""

    def test_valid_engine_constructs_successfully(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(wf, _spy_registry(["A", "B"]))
        assert engine is not None

    # ── 2. Invalid dependency ──────────────────────────────────────────────────

    def test_unknown_to_stage_in_dependency_raises(self) -> None:
        """A dependency referencing a stage not in stages list is invalid."""
        wf = WorkflowDefinition(
            name="bad", description="",
            stages=["A"],
            dependencies=[StageDependency(from_stage="A", to_stage="GHOST")],
        )
        with pytest.raises(WorkflowValidationError) as exc:
            WorkflowEngine(wf, {"A": _SpyStage("A")})
        assert "GHOST" in str(exc.value)

    def test_unknown_from_stage_in_dependency_raises(self) -> None:
        wf = WorkflowDefinition(
            name="bad", description="",
            stages=["B"],
            dependencies=[StageDependency(from_stage="GHOST", to_stage="B")],
        )
        with pytest.raises(WorkflowValidationError):
            WorkflowEngine(wf, {"B": _SpyStage("B")})

    def test_validation_error_exposes_all_errors(self) -> None:
        wf = WorkflowDefinition(
            name="multi_bad", description="",
            stages=["A"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="X"),
                StageDependency(from_stage="A", to_stage="Y"),
            ],
        )
        with pytest.raises(WorkflowValidationError) as exc:
            WorkflowEngine(wf, {"A": _SpyStage("A")})
        assert len(exc.value.errors) >= 2

    # ── 3. Circular dependency ─────────────────────────────────────────────────

    def test_two_stage_cycle_raises(self) -> None:
        """A ↔ B is a cycle — no valid execution order exists."""
        wf = WorkflowDefinition(
            name="cyclic", description="",
            stages=["A", "B"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="A"),
            ],
        )
        with pytest.raises(WorkflowValidationError) as exc:
            WorkflowEngine(wf, _spy_registry(["A", "B"]))
        assert "cycle" in str(exc.value).lower()

    def test_three_stage_cycle_raises(self) -> None:
        """A → B → C → A is a cycle."""
        wf = WorkflowDefinition(
            name="cyclic3", description="",
            stages=["A", "B", "C"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="C"),
                StageDependency(from_stage="C", to_stage="A"),
            ],
        )
        with pytest.raises(WorkflowValidationError):
            WorkflowEngine(wf, _spy_registry(["A", "B", "C"]))

    def test_self_loop_raises(self) -> None:
        """A stage depending on itself is a trivial cycle."""
        wf = WorkflowDefinition(
            name="self_loop", description="",
            stages=["A"],
            dependencies=[StageDependency(from_stage="A", to_stage="A")],
        )
        with pytest.raises(WorkflowValidationError):
            WorkflowEngine(wf, {"A": _SpyStage("A")})

    # ── Stage registry completeness ────────────────────────────────────────────

    def test_missing_stage_implementation_raises(self) -> None:
        wf = _linear_def(["A", "B"])
        with pytest.raises(StageNotRegisteredError) as exc:
            WorkflowEngine(wf, {"A": _SpyStage("A")})  # B missing
        assert "B" in str(exc.value)
        assert exc.value.stage_name == "B"

    def test_extra_registered_stages_are_allowed(self) -> None:
        """Stages in the registry but not in the definition are silently ignored."""
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A"), "EXTRA": _SpyStage("EXTRA")},
        )
        assert engine is not None


# ─── 1. Valid dependency graph — sequential execution ─────────────────────────


@pytest.mark.unit
class TestLinearExecution:
    async def test_single_stage_completes(self) -> None:
        wf = WorkflowDefinition(
            name="single", description="", stages=["only"], dependencies=[]
        )
        engine = WorkflowEngine(wf, {"only": _SpyStage("only")})
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["only"].status == StageStatus.COMPLETED

    async def test_linear_workflow_all_stages_complete(self) -> None:
        stages = ["req", "design", "impl", "test"]
        wf = _linear_def(stages)
        engine = WorkflowEngine(wf, _spy_registry(stages))
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        for name in stages:
            assert state.stages[name].status == StageStatus.COMPLETED

    async def test_execution_order_respects_dependencies(self) -> None:
        """A strictly linear chain must execute in declaration order."""
        stages = ["A", "B", "C", "D"]
        log: list[str] = []
        wf = _linear_def(stages)
        engine = WorkflowEngine(wf, _spy_registry(stages, log=log))
        await engine.run(_req())

        assert log == ["A", "B", "C", "D"]

    async def test_workflow_status_is_pending_before_run(self) -> None:
        """WorkflowState starts PENDING before we call run()."""
        from orchestrator.core.models import WorkflowState
        wf = _linear_def(["A"])
        # We check the state is initially PENDING by constructing it manually
        state = WorkflowState(
            requirement=_req(),
            workflow_definition=wf,
        )
        assert state.status == WorkflowStatus.PENDING

    async def test_audit_trail_has_lifecycle_events(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(wf, _spy_registry(["A", "B"]))
        state = await engine.run(_req())

        events = {e.event for e in state.audit_trail}
        assert "workflow_started" in events
        assert "stage_started" in events
        assert "stage_completed" in events
        assert "workflow_completed" in events

    async def test_stage_timing_recorded(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(wf, {"A": _SpyStage("A")})
        state = await engine.run(_req())

        ctx = state.stages["A"]
        assert ctx.started_at is not None
        assert ctx.completed_at is not None
        assert ctx.completed_at >= ctx.started_at

    async def test_workflow_links_to_definition(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(wf, {"A": _SpyStage("A")})
        state = await engine.run(_req())

        assert state.workflow_definition is wf

    async def test_workflow_requirement_stored(self) -> None:
        wf = _linear_def(["A"])
        req = _req()
        engine = WorkflowEngine(wf, {"A": _SpyStage("A")})
        state = await engine.run(req)

        assert state.requirement.id == req.id


# ─── 4. Parallel branches ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestParallelBranches:
    """
    Root → branch_a AND root → branch_b.
    Both branches share root as predecessor. Once root completes, both
    can run in the same asyncio.gather call.
    """

    def _parallel_def(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="parallel", description="",
            stages=["root", "branch_a", "branch_b"],
            dependencies=[
                StageDependency(from_stage="root", to_stage="branch_a"),
                StageDependency(from_stage="root", to_stage="branch_b"),
            ],
        )

    async def test_both_parallel_branches_complete(self) -> None:
        engine = WorkflowEngine(
            self._parallel_def(),
            _spy_registry(["root", "branch_a", "branch_b"]),
        )
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["branch_a"].status == StageStatus.COMPLETED
        assert state.stages["branch_b"].status == StageStatus.COMPLETED

    async def test_root_executes_before_parallel_branches(self) -> None:
        log: list[str] = []
        engine = WorkflowEngine(
            self._parallel_def(),
            _spy_registry(["root", "branch_a", "branch_b"], log=log),
        )
        await engine.run(_req())

        assert log[0] == "root"
        assert set(log[1:]) == {"branch_a", "branch_b"}

    async def test_parallel_branches_both_in_execution_context(self) -> None:
        engine = WorkflowEngine(
            self._parallel_def(),
            _spy_registry(["root", "branch_a", "branch_b"]),
        )
        state = await engine.run(_req())

        ctx: ExecutionContext = state.execution_context
        assert "branch_a" in ctx.completed_stages
        assert "branch_b" in ctx.completed_stages

    async def test_sdlc_example_two_parallel_first_stages(self) -> None:
        """
        User's conceptual diagram:
          req → architecture    (parallel)
          req → task_decomp     (parallel)
        """
        wf = WorkflowDefinition(
            name="sdlc_parallel", description="",
            stages=["req", "architecture", "task_decomp"],
            dependencies=[
                StageDependency(from_stage="req", to_stage="architecture"),
                StageDependency(from_stage="req", to_stage="task_decomp"),
            ],
        )
        engine = WorkflowEngine(wf, _spy_registry(["req", "architecture", "task_decomp"]))
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["architecture"].status == StageStatus.COMPLETED
        assert state.stages["task_decomp"].status == StageStatus.COMPLETED


# ─── 5. Synchronization (convergence barriers) ────────────────────────────────


@pytest.mark.unit
class TestSynchronizationBarriers:
    """
    Diamond pattern: A → B, A → C, B → D, C → D.
    D is the synchronization barrier — it cannot start until BOTH B and C complete.
    """

    def _diamond_def(self) -> WorkflowDefinition:
        return WorkflowDefinition(
            name="diamond", description="",
            stages=["A", "B", "C", "D"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="A", to_stage="C"),
                StageDependency(from_stage="B", to_stage="D"),
                StageDependency(from_stage="C", to_stage="D"),
            ],
        )

    async def test_diamond_all_stages_complete(self) -> None:
        engine = WorkflowEngine(self._diamond_def(), _spy_registry(["A", "B", "C", "D"]))
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        for name in ["A", "B", "C", "D"]:
            assert state.stages[name].status == StageStatus.COMPLETED

    async def test_sync_barrier_executes_last(self) -> None:
        log: list[str] = []
        engine = WorkflowEngine(
            self._diamond_def(),
            _spy_registry(["A", "B", "C", "D"], log=log),
        )
        await engine.run(_req())

        assert log[0] == "A"      # root always first
        assert log[-1] == "D"     # barrier always last
        assert "B" in log
        assert "C" in log

    async def test_sync_barrier_d_runs_after_both_b_and_c(self) -> None:
        log: list[str] = []
        engine = WorkflowEngine(
            self._diamond_def(),
            _spy_registry(["A", "B", "C", "D"], log=log),
        )
        await engine.run(_req())

        assert log.index("B") < log.index("D")
        assert log.index("C") < log.index("D")

    async def test_full_sdlc_example_with_convergence(self) -> None:
        """
        Full example from the user's specification:
          req → arch
          req → task_decomp → implementation → testing (sync)
          req → task_decomp → test_planning  → testing (sync)
          testing → docs → release
        """
        all_stages = [
            "req", "arch", "task_decomp",
            "implementation", "test_planning",
            "testing", "docs", "release",
        ]
        wf = WorkflowDefinition(
            name="sdlc", description="",
            stages=all_stages,
            dependencies=[
                StageDependency(from_stage="req",            to_stage="arch"),
                StageDependency(from_stage="req",            to_stage="task_decomp"),
                StageDependency(from_stage="task_decomp",    to_stage="implementation"),
                StageDependency(from_stage="task_decomp",    to_stage="test_planning"),
                StageDependency(from_stage="implementation", to_stage="testing"),
                StageDependency(from_stage="test_planning",  to_stage="testing"),
                StageDependency(from_stage="testing",        to_stage="docs"),
                StageDependency(from_stage="docs",           to_stage="release"),
            ],
        )
        engine = WorkflowEngine(wf, _spy_registry(all_stages))
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        for name in all_stages:
            assert state.stages[name].status == StageStatus.COMPLETED

    async def test_testing_is_synchronization_point(self) -> None:
        """'testing' has two predecessors — it must wait for both."""
        all_stages = [
            "task_decomp", "implementation", "test_planning", "testing",
        ]
        wf = WorkflowDefinition(
            name="mini_sdlc", description="",
            stages=all_stages,
            dependencies=[
                StageDependency(from_stage="task_decomp",    to_stage="implementation"),
                StageDependency(from_stage="task_decomp",    to_stage="test_planning"),
                StageDependency(from_stage="implementation", to_stage="testing"),
                StageDependency(from_stage="test_planning",  to_stage="testing"),
            ],
        )
        log: list[str] = []
        engine = WorkflowEngine(wf, _spy_registry(all_stages, log=log))
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        # testing must run after BOTH implementation and test_planning
        assert log.index("implementation") < log.index("testing")
        assert log.index("test_planning") < log.index("testing")


# ─── 6. Entry gate failure ────────────────────────────────────────────────────


@pytest.mark.unit
class TestEntryGateFailure:
    async def test_entry_gate_failure_marks_stage_failed(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], B=_SpyStage("B", entry_pass=False)),
        )
        state = await engine.run(_req())

        assert state.stages["B"].status == StageStatus.FAILED

    async def test_entry_gate_failure_marks_workflow_failed(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], B=_SpyStage("B", entry_pass=False)),
        )
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.FAILED

    async def test_entry_gate_failure_records_error_message(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], B=_SpyStage("B", entry_pass=False)),
        )
        state = await engine.run(_req())

        error = state.stages["B"].error
        assert error is not None
        assert "B_entry" in error or "entry" in error.lower()

    async def test_entry_gate_failure_records_gate_result(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], B=_SpyStage("B", entry_pass=False)),
        )
        state = await engine.run(_req())

        b_ctx = state.stages["B"]
        assert len(b_ctx.entry_gate_results) == 1
        assert not b_ctx.entry_gate_results[0].passed

    async def test_entry_gate_failure_blocks_dependents(self) -> None:
        """C depends on B. B fails entry gate. C is blocked."""
        wf = _linear_def(["A", "B", "C"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B", "C"], B=_SpyStage("B", entry_pass=False)),
        )
        state = await engine.run(_req())

        assert "C" in state.stages
        assert state.stages["C"].status == StageStatus.BLOCKED

    async def test_first_stage_entry_gate_failure(self) -> None:
        """Root stage failing at entry gate fails the entire workflow."""
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], A=_SpyStage("A", entry_pass=False)),
        )
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["A"].status == StageStatus.FAILED
        assert state.stages["B"].status == StageStatus.BLOCKED

    async def test_entry_gate_failure_audit_events(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", entry_pass=False)},
        )
        state = await engine.run(_req())

        events = {e.event for e in state.audit_trail}
        assert "entry_gate_failed" in events

    async def test_parallel_entry_gate_failure_stops_workflow(self) -> None:
        """If one parallel branch fails entry gate, the workflow fails."""
        wf = WorkflowDefinition(
            name="par", description="",
            stages=["root", "ok_branch", "fail_branch"],
            dependencies=[
                StageDependency(from_stage="root", to_stage="ok_branch"),
                StageDependency(from_stage="root", to_stage="fail_branch"),
            ],
        )
        engine = WorkflowEngine(
            wf,
            _spy_registry(
                ["root", "ok_branch", "fail_branch"],
                fail_branch=_SpyStage("fail_branch", entry_pass=False),
            ),
        )
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.FAILED


# ─── 7. Exit gate failure ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestExitGateFailure:
    async def test_exit_gate_failure_marks_stage_failed(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], B=_SpyStage("B", exit_pass=False)),
        )
        state = await engine.run(_req())

        assert state.stages["B"].status == StageStatus.FAILED

    async def test_exit_gate_failure_marks_workflow_failed(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], B=_SpyStage("B", exit_pass=False)),
        )
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.FAILED

    async def test_exit_gate_failure_stage_still_ran(self) -> None:
        """Stage that fails exit gate must have executed its logic."""
        log: list[str] = []
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", log=log, exit_pass=False)},
        )
        await engine.run(_req())

        assert "A" in log  # execute() was called

    async def test_exit_gate_failure_records_gate_result(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", exit_pass=False)},
        )
        state = await engine.run(_req())

        a_ctx = state.stages["A"]
        assert len(a_ctx.exit_gate_results) == 1
        assert not a_ctx.exit_gate_results[0].passed

    async def test_exit_gate_failure_records_error_message(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", exit_pass=False)},
        )
        state = await engine.run(_req())

        error = state.stages["A"].error
        assert error is not None
        assert "A_exit" in error or "exit" in error.lower()

    async def test_exit_gate_failure_blocks_dependents(self) -> None:
        """C depends on B. B fails exit gate. C is blocked."""
        log: list[str] = []
        wf = _linear_def(["A", "B", "C"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(
                ["A", "B", "C"],
                log=log,
                B=_SpyStage("B", log=log, exit_pass=False),
            ),
        )
        state = await engine.run(_req())

        assert "C" not in log            # C never ran
        assert state.stages["C"].status == StageStatus.BLOCKED

    async def test_exit_gate_failure_audit_events(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", exit_pass=False)},
        )
        state = await engine.run(_req())

        events = {e.event for e in state.audit_trail}
        assert "exit_gate_failed" in events


# ─── Context propagation ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestContextPropagation:
    async def test_stage_output_propagates_to_downstream_input(self) -> None:
        """A's output_data becomes available in B's input_data."""
        wf = _linear_def(["A", "B"])
        capturing_b = _CapturingStage("B")
        engine = WorkflowEngine(
            wf,
            {
                "A": _SpyStage("A", output={"a_result": "hello"}),
                "B": capturing_b,
            },
        )
        await engine.run(_req())

        assert capturing_b.received_input.get("a_result") == "hello"

    async def test_execution_context_records_all_stages(self) -> None:
        stages = ["A", "B", "C"]
        wf = _linear_def(stages)
        engine = WorkflowEngine(wf, _spy_registry(stages))
        state = await engine.run(_req())

        ctx: ExecutionContext = state.execution_context
        assert set(ctx.completed_stages) == {"A", "B", "C"}

    async def test_execution_context_stores_stage_outputs(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            {
                "A": _SpyStage("A", output={"key_a": "val_a"}),
                "B": _SpyStage("B", output={"key_b": "val_b"}),
            },
        )
        state = await engine.run(_req())

        ctx: ExecutionContext = state.execution_context
        assert ctx.get_output("A", "key_a") == "val_a"
        assert ctx.get_output("B", "key_b") == "val_b"

    async def test_failed_stage_output_not_in_context(self) -> None:
        """Output from a failed stage should not be propagated."""
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", exit_pass=False, output={"secret": "should_not_appear"})},
        )
        state = await engine.run(_req())

        ctx: ExecutionContext = state.execution_context
        # A failed, so its output should not be in the context
        assert "A" not in ctx.completed_stages


# ─── Stage execution raises ───────────────────────────────────────────────────


@pytest.mark.unit
class TestExecuteRaises:
    async def test_execute_exception_marks_stage_failed(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", raises_on_execute=True)},
        )
        state = await engine.run(_req())

        assert state.stages["A"].status == StageStatus.FAILED
        assert state.status == WorkflowStatus.FAILED

    async def test_execute_exception_error_message_recorded(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", raises_on_execute=True)},
        )
        state = await engine.run(_req())

        error = state.stages["A"].error
        assert error is not None
        assert "exception" in error.lower() or "raise" in error.lower()

    async def test_execute_exception_blocks_dependents(self) -> None:
        wf = _linear_def(["A", "B", "C"])
        log: list[str] = []
        engine = WorkflowEngine(
            wf,
            {
                "A": _SpyStage("A", log=log),
                "B": _SpyStage("B", log=log, raises_on_execute=True),
                "C": _SpyStage("C", log=log),
            },
        )
        state = await engine.run(_req())

        assert "C" not in log
        assert state.stages["C"].status == StageStatus.BLOCKED

    async def test_failed_workflow_has_audit_event(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(
            wf,
            {"A": _SpyStage("A", raises_on_execute=True)},
        )
        state = await engine.run(_req())

        events = {e.event for e in state.audit_trail}
        assert "workflow_failed" in events


# ─── WorkflowState completeness ───────────────────────────────────────────────


@pytest.mark.unit
class TestWorkflowStateCompleteness:
    async def test_completed_workflow_has_no_blocked_stages(self) -> None:
        stages = ["A", "B", "C"]
        wf = _linear_def(stages)
        engine = WorkflowEngine(wf, _spy_registry(stages))
        state = await engine.run(_req())

        assert state.status == WorkflowStatus.COMPLETED
        for ctx in state.stages.values():
            assert ctx.status != StageStatus.BLOCKED

    async def test_failed_workflow_blocked_stages_in_state(self) -> None:
        """When B fails, C (depending on B) is explicitly recorded as BLOCKED."""
        wf = _linear_def(["A", "B", "C"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B", "C"], B=_SpyStage("B", entry_pass=False)),
        )
        state = await engine.run(_req())

        assert "C" in state.stages
        assert state.stages["C"].status == StageStatus.BLOCKED

    async def test_completed_stages_set_accurate(self) -> None:
        stages = ["A", "B", "C"]
        wf = _linear_def(stages)
        engine = WorkflowEngine(wf, _spy_registry(stages))
        state = await engine.run(_req())

        assert state.completed_stage_names == {"A", "B", "C"}

    async def test_failed_stage_in_failed_set(self) -> None:
        wf = _linear_def(["A", "B"])
        engine = WorkflowEngine(
            wf,
            _spy_registry(["A", "B"], B=_SpyStage("B", entry_pass=False)),
        )
        state = await engine.run(_req())

        assert "B" in state.failed_stage_names

    async def test_repr(self) -> None:
        wf = _linear_def(["A"])
        engine = WorkflowEngine(wf, {"A": _SpyStage("A")})
        assert "WorkflowEngine" in repr(engine)
        assert "linear" in repr(engine)
