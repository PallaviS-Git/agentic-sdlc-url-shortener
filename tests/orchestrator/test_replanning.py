"""
Tests for dynamic workflow replanning.

The workflow must detect when an upstream output changes, determine which
downstream stages are impacted, and re-execute only those stages while
preserving all unaffected completed work.

Coverage
────────
Impact analysis (analyze_impact — pure, synchronous)
  1.  Terminal stage change → no impacted stages (no-impact change)
  2.  Mid-stage change → only downstream stages impacted
  3.  Root stage change → all successors impacted
  4.  Requirement-level change (originating_stage=None) → all stages impacted
  5.  Branching topology: upstream change impacts all branches
  6.  Stale artifact IDs collected from impacted stage contexts
  7.  Stale decision IDs collected from impacted stage contexts
  8.  Preserved stages = all stages not in impacted set
  9.  ImpactAnalysis.has_impact is False for no-impact events
  10. Invalid originating_stage raises ValueError

Replan execution
  11. No-impact replan → skipped (replan_count increments, stages_replanned=[])
  12. Single downstream impact → only that stage re-executes (others untouched)
  13. Multiple downstream impacts → all downstream stages re-execute
  14. Unaffected stages are preserved: output unchanged, not re-executed
  15. Requirement change → all stages re-execute
  16. Architecture change (DECISION_CHANGED) → downstream stages re-execute
  17. Preserved stages' outputs available to replanned stages via ExecutionContext
  18. Replanned stages receive fresh inputs from rebuilt context
  19. replan_count increments on every replan call
  20. replan_history grows by one entry per replan

Governance re-check
  21. Replanned stage goes through governance gate again
  22. governance_reevaluations contains replanned stage names
  23. Governance BLOCK during replan → stage fails, workflow FAILED

Approval re-check
  24. Stage with requires_approval=True re-requests approval during replan
  25. approvals_rerequested contains replanned stage names
  26. Replan approval rejected → stage fails

Audit trail
  27. replan_initiated audit event emitted
  28. replan_completed audit event emitted
  29. replan_skipped event emitted for no-impact replans

Lineage
  30. ReplanResult in state.replan_history has correct change_event
  31. ReplanResult has correct stages_preserved and stages_replanned
  32. Multiple replan cycles each get a unique ReplanResult in history

asyncio_mode=auto (pyproject.toml).
"""
from __future__ import annotations

import pytest

from orchestrator.core.autonomy import (
    ActionImpact,
    AutoApproveGateway,
    AutoRejectGateway,
)
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.governance import (
    EnforcementDecision,
    Policy,
    PolicyDomain,
    PolicyEngine,
    PolicyViolation,
    RequireChangeTicket,
)
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.models import (
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    StageStatus,
    WorkflowStatus,
)
from orchestrator.core.replanning import (
    ChangeEvent,
    ChangeEventType,
    ImpactAnalysis,
    ReplanResult,
)
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── Stage stubs ──────────────────────────────────────────────────────────────


class _BaseTestStage(BaseStage):
    stage_name: str = ""

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_entry", passed=True)

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_exit", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        return ctx

    async def rollback(self, ctx: StageContext) -> StageContext:
        return ctx


class _CountingStage(_BaseTestStage):
    """Records call count and propagates its name as output."""

    def __init__(self, name: str) -> None:
        self.stage_name = name
        self.call_count = 0

    async def execute(self, ctx: StageContext) -> StageContext:
        self.call_count += 1
        ctx.output_data[self.stage_name] = f"{self.stage_name}_v{self.call_count}"
        return ctx


class _RequiresApprovalStage(_CountingStage):
    requires_approval = True


class _BlockedByPolicyStage(_CountingStage):
    """Always blocked by governance; used in governance-re-check tests."""
    action_impact = ActionImpact.SIGNIFICANT
    policy_metadata = {}  # missing change_ticket_id → COMP-001 fires


# ─── Topology helpers ─────────────────────────────────────────────────────────
#
# Shared topologies used across tests:
#
#  linear_4:    req → arch → impl → tests
#  branching:   arch → impl
#                   → docs
#               (impl has no further successors in this graph)


def _stages_linear_4() -> dict[str, _CountingStage]:
    return {
        "req":  _CountingStage("req"),
        "arch": _CountingStage("arch"),
        "impl": _CountingStage("impl"),
        "tests": _CountingStage("tests"),
    }


def _defn_linear_4() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="sdlc",
        description="4-stage linear SDLC",
        stages=["req", "arch", "impl", "tests"],
        dependencies=[
            StageDependency(from_stage="req",  to_stage="arch"),
            StageDependency(from_stage="arch", to_stage="impl"),
            StageDependency(from_stage="impl", to_stage="tests"),
        ],
    )


def _stages_branching() -> dict[str, _CountingStage]:
    return {
        "arch": _CountingStage("arch"),
        "impl": _CountingStage("impl"),
        "docs": _CountingStage("docs"),
    }


def _defn_branching() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="branching",
        description="Branching: arch → {impl, docs}",
        stages=["arch", "impl", "docs"],
        dependencies=[
            StageDependency(from_stage="arch", to_stage="impl"),
            StageDependency(from_stage="arch", to_stage="docs"),
        ],
    )


def _change(
    originating: str | None,
    event_type: ChangeEventType = ChangeEventType.ARTIFACT_CHANGED,
    description: str = "Something changed",
) -> ChangeEvent:
    return ChangeEvent(
        event_type=event_type,
        originating_stage=originating,
        change_description=description,
    )


@pytest.fixture()
def requirement() -> Requirement:
    return Requirement(
        title="Replan test",
        raw_text="Build the URL shortener",
        requirement_type=RequirementType.GREENFIELD,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Impact analysis (analyze_impact — pure, sync)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnalyzeImpact:
    def _engine(self, defn, stages) -> WorkflowEngine:
        return WorkflowEngine(definition=defn, stages=stages)

    def _empty_state(self, defn, stages, req) -> "WorkflowState":
        from orchestrator.core.models import WorkflowState, WorkflowStatus
        return WorkflowState(requirement=req, status=WorkflowStatus.COMPLETED)

    def test_terminal_stage_has_no_impact(self, requirement: Requirement) -> None:
        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = self._empty_state(_defn_linear_4(), stages, requirement)
        impact = engine.analyze_impact(state, _change("tests"))
        assert impact.impacted_stages == []
        assert not impact.has_impact

    def test_mid_stage_impacts_only_downstream(self, requirement: Requirement) -> None:
        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = self._empty_state(_defn_linear_4(), stages, requirement)
        impact = engine.analyze_impact(state, _change("arch"))
        assert sorted(impact.impacted_stages) == ["impl", "tests"]
        assert "req" not in impact.impacted_stages
        assert "arch" not in impact.impacted_stages

    def test_root_stage_impacts_all_successors(self, requirement: Requirement) -> None:
        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = self._empty_state(_defn_linear_4(), stages, requirement)
        impact = engine.analyze_impact(state, _change("req"))
        assert sorted(impact.impacted_stages) == ["arch", "impl", "tests"]

    def test_requirement_change_impacts_all_stages(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = self._empty_state(_defn_linear_4(), stages, requirement)
        impact = engine.analyze_impact(
            state,
            _change(None, ChangeEventType.REQUIREMENT_CHANGE, "Scope expanded"),
        )
        assert sorted(impact.impacted_stages) == ["arch", "impl", "req", "tests"]

    def test_branching_topology_impacts_all_branches(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_branching()
        engine = self._engine(_defn_branching(), stages)
        state = self._empty_state(_defn_branching(), stages, requirement)
        impact = engine.analyze_impact(state, _change("arch"))
        assert sorted(impact.impacted_stages) == ["docs", "impl"]

    def test_stale_artifact_ids_collected(self, requirement: Requirement) -> None:
        from orchestrator.core.models import WorkflowState, WorkflowStatus
        from orchestrator.core.results import Artifact, ArtifactType

        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = WorkflowState(requirement=requirement, status=WorkflowStatus.COMPLETED)
        art = Artifact(id="art-001", name="schema.sql", artifact_type=ArtifactType.SCHEMA,
                       produced_by_stage="impl")
        impl_ctx = StageContext(stage_name="impl", status=StageStatus.COMPLETED)
        impl_ctx.artifacts.append(art)
        state.stages["impl"] = impl_ctx

        impact = engine.analyze_impact(state, _change("arch"))
        assert "art-001" in impact.invalidated_artifact_ids

    def test_stale_decision_ids_collected(self, requirement: Requirement) -> None:
        from orchestrator.core.models import WorkflowState, WorkflowStatus
        from orchestrator.core.results import Decision, DecisionType

        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = WorkflowState(requirement=requirement, status=WorkflowStatus.COMPLETED)
        dec = Decision(
            id="dec-001",
            decision_type=DecisionType.SCOPE,
            title="API style",
            description="REST vs GraphQL",
            rationale="REST chosen",
            stage="impl",
        )
        impl_ctx = StageContext(stage_name="impl", status=StageStatus.COMPLETED)
        impl_ctx.decisions.append(dec)
        state.stages["impl"] = impl_ctx

        impact = engine.analyze_impact(state, _change("arch"))
        assert "dec-001" in impact.invalidated_decision_ids

    def test_preserved_stages_complement_of_impacted(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = self._empty_state(_defn_linear_4(), stages, requirement)
        impact = engine.analyze_impact(state, _change("arch"))
        all_stages = set(_defn_linear_4().stages)
        assert set(impact.impacted_stages) | set(impact.preserved_stages) == all_stages
        assert not (set(impact.impacted_stages) & set(impact.preserved_stages))

    def test_has_impact_false_for_no_impact_event(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = self._empty_state(_defn_linear_4(), stages, requirement)
        impact = engine.analyze_impact(state, _change("tests"))
        assert impact.has_impact is False

    def test_invalid_originating_stage_raises_value_error(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = self._engine(_defn_linear_4(), stages)
        state = self._empty_state(_defn_linear_4(), stages, requirement)
        with pytest.raises(ValueError, match="not defined in the workflow"):
            engine.analyze_impact(state, _change("nonexistent_stage"))


# ═══════════════════════════════════════════════════════════════════════════════
# Replan execution
# ═══════════════════════════════════════════════════════════════════════════════


class TestReplanExecution:
    async def test_no_impact_replan_skips_execution(
        self, requirement: Requirement
    ) -> None:
        """Terminal stage change → replan is a no-op; nothing re-executes."""
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        initial_calls = {name: s.call_count for name, s in stages.items()}
        state = await engine.replan(state, _change("tests"))

        # All stages preserved
        result = state.replan_history[-1]
        assert result.stages_replanned == []
        assert state.replan_count == 1
        # No extra calls
        for name, stage in stages.items():
            assert stage.call_count == initial_calls[name]
        # Audit trail includes skipped event
        events = {e.event for e in state.audit_trail}
        assert "replan_skipped" in events

    async def test_single_downstream_impact(self, requirement: Requirement) -> None:
        """Change on impl → only tests re-executes."""
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        # Verify initial call counts
        assert stages["tests"].call_count == 1

        state = await engine.replan(state, _change("impl"))
        result = state.replan_history[-1]

        assert "tests" in result.stages_replanned
        assert "impl" not in result.stages_replanned
        assert "arch" not in result.stages_replanned
        assert stages["tests"].call_count == 2  # re-executed
        assert stages["impl"].call_count == 1   # preserved

    async def test_multiple_downstream_impacts_linear(
        self, requirement: Requirement
    ) -> None:
        """Change on arch → impl AND tests both re-execute."""
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        state = await engine.replan(state, _change("arch"))
        result = state.replan_history[-1]

        assert sorted(result.stages_replanned) == ["impl", "tests"]
        assert stages["impl"].call_count == 2
        assert stages["tests"].call_count == 2
        assert stages["arch"].call_count == 1  # preserved
        assert stages["req"].call_count == 1   # preserved

    async def test_unaffected_stages_not_re_executed(
        self, requirement: Requirement
    ) -> None:
        """Preserved stages keep their original outputs and are not called again."""
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        # Capture preserved stage output before replan
        arch_output_before = dict(state.stages["arch"].output_data)

        state = await engine.replan(state, _change("impl"))

        # arch was not replanned
        assert stages["arch"].call_count == 1
        # arch output unchanged in state
        assert state.stages["arch"].output_data == arch_output_before

    async def test_requirement_change_replans_all_stages(
        self, requirement: Requirement
    ) -> None:
        """originating_stage=None → all stages re-execute."""
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        state = await engine.replan(
            state,
            _change(None, ChangeEventType.REQUIREMENT_CHANGE, "Scope changed"),
        )
        result = state.replan_history[-1]

        assert sorted(result.stages_replanned) == ["arch", "impl", "req", "tests"]
        for stage in stages.values():
            assert stage.call_count == 2

    async def test_architecture_change_decision_changed(
        self, requirement: Requirement
    ) -> None:
        """DECISION_CHANGED on arch → impl and tests re-execute."""
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        state = await engine.replan(
            state,
            _change("arch", ChangeEventType.DECISION_CHANGED, "Switched from REST to GraphQL"),
        )
        result = state.replan_history[-1]

        assert sorted(result.stages_replanned) == ["impl", "tests"]
        assert result.change_event.event_type == ChangeEventType.DECISION_CHANGED

    async def test_branching_topology_replans_all_branches(
        self, requirement: Requirement
    ) -> None:
        """Change on arch in branching topology replans both impl and docs."""
        stages = _stages_branching()
        engine = WorkflowEngine(definition=_defn_branching(), stages=stages)
        state = await engine.run(requirement)

        state = await engine.replan(state, _change("arch"))
        result = state.replan_history[-1]

        assert sorted(result.stages_replanned) == ["docs", "impl"]
        assert stages["impl"].call_count == 2
        assert stages["docs"].call_count == 2
        assert stages["arch"].call_count == 1  # preserved

    async def test_preserved_outputs_available_to_replanned_stages(
        self, requirement: Requirement
    ) -> None:
        """Replanned stages receive preserved stage outputs in input_data."""
        stages = {
            "req": _CountingStage("req"),
            "impl": _CountingStage("impl"),
        }
        defn = WorkflowDefinition(
            name="two_stage",
            description="",
            stages=["req", "impl"],
            dependencies=[StageDependency(from_stage="req", to_stage="impl")],
        )
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        # Capture what req produced
        req_output = dict(state.stages["req"].output_data)

        # Replan impl (req is preserved)
        state = await engine.replan(state, _change("req"))

        # impl should receive req's output as input_data
        impl_ctx = state.stages["impl"]
        for k, v in req_output.items():
            assert impl_ctx.input_data.get(k) == v

    async def test_replan_count_increments(self, requirement: Requirement) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        assert state.replan_count == 0
        state = await engine.replan(state, _change("impl"))
        assert state.replan_count == 1
        state = await engine.replan(state, _change("arch"))
        assert state.replan_count == 2

    async def test_replan_history_grows(self, requirement: Requirement) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        assert len(state.replan_history) == 0
        state = await engine.replan(state, _change("impl"))
        assert len(state.replan_history) == 1
        state = await engine.replan(state, _change("arch"))
        assert len(state.replan_history) == 2

    async def test_workflow_remains_completed_after_successful_replan(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)
        state = await engine.replan(state, _change("arch"))
        assert state.status == WorkflowStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# Governance re-check
# ═══════════════════════════════════════════════════════════════════════════════


class TestGovernanceRecheck:
    async def test_replanned_stage_goes_through_governance(
        self, requirement: Requirement
    ) -> None:
        """PolicyEngine evaluates replanned stages again."""
        stages = _stages_linear_4()
        pe = PolicyEngine(policies=[RequireChangeTicket()])
        engine = WorkflowEngine(
            definition=_defn_linear_4(),
            stages=stages,
            policy_engine=pe,
        )
        state = await engine.run(requirement)

        pre_eval_count = len(state.policy_evaluations)
        state = await engine.replan(state, _change("impl"))
        result = state.replan_history[-1]

        # New policy evaluations were created for the replanned stage(s)
        assert len(state.policy_evaluations) > pre_eval_count
        assert "tests" in result.governance_reevaluations

    async def test_governance_block_during_replan_fails_stage(
        self, requirement: Requirement
    ) -> None:
        """If a policy blocks a replanned stage, the workflow fails."""

        class _AlwaysBlockPolicy(Policy):
            policy_id = "TEST-BLOCK"
            domain = PolicyDomain.SECURITY
            description = "Always blocks"

            def evaluate(self, context) -> PolicyViolation | None:
                return PolicyViolation(
                    policy_id=self.policy_id,
                    domain=self.domain,
                    message="Blocked by test policy",
                    decision=EnforcementDecision.BLOCK,
                    evidence={},
                )

        # First run without policy (succeeds)
        stages = _stages_linear_4()
        engine_no_policy = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine_no_policy.run(requirement)
        assert state.status == WorkflowStatus.COMPLETED

        # Replan with engine that HAS the blocking policy
        engine_with_policy = WorkflowEngine(
            definition=_defn_linear_4(),
            stages=stages,
            policy_engine=PolicyEngine(policies=[_AlwaysBlockPolicy()]),
        )
        state = await engine_with_policy.replan(state, _change("arch"))
        assert state.status == WorkflowStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# Approval re-check
# ═══════════════════════════════════════════════════════════════════════════════


class TestApprovalRecheck:
    async def test_approval_stage_re_requests_approval_on_replan(
        self, requirement: Requirement
    ) -> None:
        """A stage with requires_approval=True requests fresh approval during replan."""
        stages = {
            "req": _CountingStage("req"),
            "impl": _RequiresApprovalStage("impl"),
        }
        defn = WorkflowDefinition(
            name="approval_test",
            description="",
            stages=["req", "impl"],
            dependencies=[StageDependency(from_stage="req", to_stage="impl")],
        )
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)
        pre_approval_count = len(state.approvals)

        state = await engine.replan(state, _change("req"))
        result = state.replan_history[-1]

        # impl re-ran and generated a new approval request
        assert len(state.approvals) > pre_approval_count
        assert "impl" in result.approvals_rerequested

    async def test_approval_rejected_during_replan_fails_workflow(
        self, requirement: Requirement
    ) -> None:
        """If an approval is rejected during replan, the stage and workflow fail."""
        stages = {
            "req": _CountingStage("req"),
            "impl": _RequiresApprovalStage("impl"),
        }
        defn = WorkflowDefinition(
            name="reject_test",
            description="",
            stages=["req", "impl"],
            dependencies=[StageDependency(from_stage="req", to_stage="impl")],
        )
        # First run: approve
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)
        assert state.status == WorkflowStatus.COMPLETED

        # Replan with reject gateway
        engine_reject = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoRejectGateway(),
        )
        state = await engine_reject.replan(state, _change("req"))
        assert state.status == WorkflowStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# Audit trail
# ═══════════════════════════════════════════════════════════════════════════════


class TestReplanAuditTrail:
    async def test_replan_initiated_event_emitted(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)
        state = await engine.replan(state, _change("impl"))
        events = {e.event for e in state.audit_trail}
        assert "replan_initiated" in events

    async def test_replan_completed_event_emitted(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)
        state = await engine.replan(state, _change("impl"))
        events = {e.event for e in state.audit_trail}
        assert "replan_completed" in events

    async def test_replan_skipped_event_for_no_impact(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)
        state = await engine.replan(state, _change("tests"))  # terminal stage
        events = {e.event for e in state.audit_trail}
        assert "replan_skipped" in events


# ═══════════════════════════════════════════════════════════════════════════════
# Lineage
# ═══════════════════════════════════════════════════════════════════════════════


class TestReplanLineage:
    async def test_replan_result_has_correct_change_event(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        change = _change("arch", ChangeEventType.DECISION_CHANGED, "Architecture revised")
        state = await engine.replan(state, change)
        result = state.replan_history[-1]

        assert result.change_event.event_type == ChangeEventType.DECISION_CHANGED
        assert result.change_event.originating_stage == "arch"
        assert result.change_event.change_description == "Architecture revised"

    async def test_replan_result_stages_preserved_and_replanned(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)
        state = await engine.replan(state, _change("arch"))

        result = state.replan_history[-1]
        assert sorted(result.stages_replanned) == ["impl", "tests"]
        assert sorted(result.stages_preserved) == ["arch", "req"]

    async def test_multiple_replan_cycles_all_in_history(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        state = await engine.replan(state, _change("arch"))
        state = await engine.replan(state, _change("impl"))

        assert len(state.replan_history) == 2
        assert state.replan_history[0].replan_cycle == 1
        assert state.replan_history[1].replan_cycle == 2
        assert state.replan_history[0].change_event.originating_stage == "arch"
        assert state.replan_history[1].change_event.originating_stage == "impl"

    async def test_replan_cycle_numbers_monotonically_increasing(
        self, requirement: Requirement
    ) -> None:
        stages = _stages_linear_4()
        engine = WorkflowEngine(definition=_defn_linear_4(), stages=stages)
        state = await engine.run(requirement)

        for _ in range(3):
            state = await engine.replan(state, _change("impl"))

        cycles = [r.replan_cycle for r in state.replan_history]
        assert cycles == [1, 2, 3]
