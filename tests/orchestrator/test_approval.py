"""
Tests for human approval checkpoints and controlled autonomy.

Required coverage:
  1.  ApprovalPolicy maps each ActionImpact to the correct AutonomyLevel
  2.  ApprovalPolicy.requires_human_approval() returns True for HIGH_IMPACT+
  3.  ApprovalPolicy.allows_agent_execution() returns False for CRITICAL only
  4.  AutoApproveGateway always returns approved=True
  5.  AutoRejectGateway always returns approved=False
  6.  PresetApprovalGateway returns decisions by stage name; wildcard fallback
  7.  EscalatingApprovalGateway escalates on rejection; approves at level 1
  8.  EscalatingApprovalGateway returns final rejection when all levels reject
  9.  Stage with requires_approval=True + AutoApproveGateway → COMPLETED
  10. Stage with requires_approval=True + AutoRejectGateway → FAILED + REJECTED record
  11. Stage with requires_approval=True + no gateway → FAILED + TIMED_OUT record
  12. Stage with action_impact=HIGH_IMPACT + policy + AutoApproveGateway → COMPLETED
  13. Stage with action_impact=HIGH_IMPACT + no gateway → FAILED + TIMED_OUT
  14. Stage with action_impact=CRITICAL → FAILED immediately (HUMAN_ONLY, no gateway call)
  15. Stage with action_impact=ROUTINE → executes without any approval
  16. final_approval_required=True + AutoApproveGateway → COMPLETED
  17. final_approval_required=True + AutoRejectGateway → FAILED
  18. final_approval_required=True + no gateway → FAILED
  19. Approval records stored in WorkflowState.approvals after decision
  20. Approval record carries impact_level, escalation_level, decision_rationale
  21. Audit trail contains approval_requested + approval_resolved events
  22. Audit trail contains final_qc_requested event when final_approval_required
  23. EscalatingApprovalGateway approval record shows escalation_level > 0
  24. Downstream stages after an approved stage still execute (no collateral block)
  25. is_override flag propagated to Approval record

No I/O, no network, no DB. asyncio_mode=auto (pyproject.toml) handles async tests.
"""
from __future__ import annotations

import pytest

from orchestrator.core.autonomy import (
    ActionImpact,
    AgentAction,
    AgentAutonomyMode,
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
    AutoApproveGateway,
    AutoRejectGateway,
    AutonomyLevel,
    DEFAULT_APPROVAL_POLICY,
    EscalatingApprovalGateway,
    HighImpactActionType,
    PresetApprovalGateway,
)
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.models import (
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    WorkflowStatus,
)
from orchestrator.core.results import ApprovalStatus
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def requirement() -> Requirement:
    return Requirement(
        title="Test workflow",
        raw_text="Run a test workflow to exercise approval checkpoints",
        requirement_type=RequirementType.GREENFIELD,
    )


@pytest.fixture()
def single_stage_definition() -> WorkflowDefinition:
    """One-stage workflow for isolated approval tests."""
    return WorkflowDefinition(
        name="approval-test",
        description="Single stage for approval testing",
        stages=["protected"],
    )


@pytest.fixture()
def two_stage_definition() -> WorkflowDefinition:
    """Two-stage linear workflow: protected → downstream."""
    return WorkflowDefinition(
        name="two-stage-approval-test",
        description="Two stages for downstream propagation testing",
        stages=["protected", "downstream"],
        dependencies=[
            StageDependency(from_stage="protected", to_stage="downstream"),
        ],
    )


# ─── Stage stubs ──────────────────────────────────────────────────────────────


class _BaseTestStage(BaseStage):
    """Minimal base: passing gates, no rollback."""

    stage_name: str = ""

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        ctx.output_data["executed"] = True
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        return ctx


class _RequiresApprovalStage(_BaseTestStage):
    """Stage that explicitly sets requires_approval=True."""
    stage_name = "protected"
    requires_approval = True
    action_impact = ActionImpact.ROUTINE  # explicit override wins over policy


class _HighImpactStage(_BaseTestStage):
    """Stage with HIGH_IMPACT action_impact — policy enforces approval."""
    stage_name = "protected"
    action_impact = ActionImpact.HIGH_IMPACT
    high_impact_action_type = HighImpactActionType.SCHEMA_MIGRATION


class _CriticalStage(_BaseTestStage):
    """Stage with CRITICAL action_impact — always blocked (HUMAN_ONLY)."""
    stage_name = "protected"
    action_impact = ActionImpact.CRITICAL


class _RoutineStage(_BaseTestStage):
    """Stage with ROUTINE impact — never needs approval."""
    stage_name = "protected"
    action_impact = ActionImpact.ROUTINE


class _DownstreamStage(_BaseTestStage):
    """Simple downstream stage that records whether it ran."""
    stage_name = "downstream"


# ─── 1-3: ApprovalPolicy unit tests ───────────────────────────────────────────


class TestApprovalPolicy:
    def test_routine_maps_to_full_auto(self) -> None:
        assert DEFAULT_APPROVAL_POLICY.required_autonomy(ActionImpact.ROUTINE) == AutonomyLevel.FULL_AUTO

    def test_significant_maps_to_supervised(self) -> None:
        assert DEFAULT_APPROVAL_POLICY.required_autonomy(ActionImpact.SIGNIFICANT) == AutonomyLevel.SUPERVISED

    def test_high_impact_maps_to_approval_required(self) -> None:
        assert DEFAULT_APPROVAL_POLICY.required_autonomy(ActionImpact.HIGH_IMPACT) == AutonomyLevel.APPROVAL_REQUIRED

    def test_critical_maps_to_human_only(self) -> None:
        assert DEFAULT_APPROVAL_POLICY.required_autonomy(ActionImpact.CRITICAL) == AutonomyLevel.HUMAN_ONLY

    def test_requires_human_approval_false_for_routine(self) -> None:
        assert not DEFAULT_APPROVAL_POLICY.requires_human_approval(ActionImpact.ROUTINE)

    def test_requires_human_approval_false_for_significant(self) -> None:
        assert not DEFAULT_APPROVAL_POLICY.requires_human_approval(ActionImpact.SIGNIFICANT)

    def test_requires_human_approval_true_for_high_impact(self) -> None:
        assert DEFAULT_APPROVAL_POLICY.requires_human_approval(ActionImpact.HIGH_IMPACT)

    def test_requires_human_approval_true_for_critical(self) -> None:
        assert DEFAULT_APPROVAL_POLICY.requires_human_approval(ActionImpact.CRITICAL)

    def test_allows_agent_execution_true_for_routine(self) -> None:
        assert DEFAULT_APPROVAL_POLICY.allows_agent_execution(ActionImpact.ROUTINE)

    def test_allows_agent_execution_true_for_high_impact(self) -> None:
        # HIGH_IMPACT requires approval but agents CAN execute after approval
        assert DEFAULT_APPROVAL_POLICY.allows_agent_execution(ActionImpact.HIGH_IMPACT)

    def test_allows_agent_execution_false_for_critical(self) -> None:
        # CRITICAL → HUMAN_ONLY; agents are completely blocked
        assert not DEFAULT_APPROVAL_POLICY.allows_agent_execution(ActionImpact.CRITICAL)

    def test_custom_policy_raises_approval_threshold(self) -> None:
        """A stricter policy can require approval starting at SIGNIFICANT."""
        strict = ApprovalPolicy(
            approval_threshold=ActionImpact.SIGNIFICANT,
            critical_threshold=ActionImpact.CRITICAL,
        )
        assert strict.requires_human_approval(ActionImpact.SIGNIFICANT)
        assert strict.requires_human_approval(ActionImpact.HIGH_IMPACT)

    def test_custom_policy_lower_critical_threshold(self) -> None:
        """Setting critical_threshold to HIGH_IMPACT blocks agent execution there."""
        strict = ApprovalPolicy(
            approval_threshold=ActionImpact.HIGH_IMPACT,
            critical_threshold=ActionImpact.HIGH_IMPACT,
        )
        assert not strict.allows_agent_execution(ActionImpact.HIGH_IMPACT)


# ─── 4-8: Gateway unit tests ──────────────────────────────────────────────────


class TestApprovalGateways:
    async def test_auto_approve_always_approves(self) -> None:
        gw = AutoApproveGateway(approver="test-approver")
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="release",
            stage_summary="Deploy to production",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is True
        assert decision.approver == "test-approver"
        assert decision.request_id == request.id

    async def test_auto_reject_always_rejects(self) -> None:
        gw = AutoRejectGateway(reason="Policy violation")
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="release",
            stage_summary="Deploy to production",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is False
        assert decision.rationale == "Policy violation"

    async def test_preset_approves_named_stage(self) -> None:
        gw = PresetApprovalGateway({"release": True, "migration": False})
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="release",
            stage_summary="Deploy",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is True

    async def test_preset_rejects_named_stage(self) -> None:
        gw = PresetApprovalGateway({"release": True, "migration": False})
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="migration",
            stage_summary="Migrate",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is False

    async def test_preset_wildcard_fallback(self) -> None:
        gw = PresetApprovalGateway({"*": True})
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="anything",
            stage_summary="Anything",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is True

    async def test_preset_no_wildcard_default_rejects(self) -> None:
        gw = PresetApprovalGateway({"release": True})
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="unknown",
            stage_summary="Unknown stage",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is False  # no match, no wildcard → False

    async def test_escalating_approves_at_level_1(self) -> None:
        """First gateway rejects; second gateway approves at escalation level 1."""
        gw = EscalatingApprovalGateway([
            AutoRejectGateway(reason="Initial reviewer rejected"),
            AutoApproveGateway(approver="senior-approver"),
        ])
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="release",
            stage_summary="Deploy to prod",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is True
        assert decision.approver == "senior-approver"
        assert decision.escalation_level == 1

    async def test_escalating_all_reject_returns_false(self) -> None:
        gw = EscalatingApprovalGateway([
            AutoRejectGateway(reason="Tier-1 rejected"),
            AutoRejectGateway(reason="Tier-2 rejected"),
        ])
        request = ApprovalRequest(
            workflow_id="wf-1",
            stage_name="release",
            stage_summary="Deploy",
        )
        decision = await gw.request_approval(request)
        assert decision.approved is False
        assert decision.escalation_level == 1  # ended at highest level

    def test_escalating_requires_at_least_one_gateway(self) -> None:
        with pytest.raises(ValueError, match="at least one gateway"):
            EscalatingApprovalGateway([])


# ─── 9-11: requires_approval=True enforcement ─────────────────────────────────


class TestRequiresApprovalFlag:
    async def test_auto_approve_gateway_lets_stage_complete(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["protected"].status.value == "completed"

    async def test_auto_reject_gateway_fails_stage(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=AutoRejectGateway(reason="Rejected by policy"),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["protected"].status.value == "failed"

    async def test_no_gateway_fails_protected_stage(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=None,  # no gateway
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["protected"].status.value == "failed"

    async def test_rejection_approval_record_has_rejected_status(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=AutoRejectGateway(),
        )
        state = await engine.run(requirement)

        assert len(state.approvals) == 1
        assert state.approvals[0].status == ApprovalStatus.REJECTED
        assert state.approvals[0].stage_name == "protected"

    async def test_no_gateway_approval_record_has_timed_out_status(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=None,
        )
        state = await engine.run(requirement)

        assert len(state.approvals) == 1
        assert state.approvals[0].status == ApprovalStatus.TIMED_OUT


# ─── 12-13: action_impact=HIGH_IMPACT enforcement ─────────────────────────────


class TestHighImpactActionEnforcement:
    async def test_high_impact_approved_completes(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _HighImpactStage()},
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)
        assert state.status == WorkflowStatus.COMPLETED

    async def test_high_impact_no_gateway_fails(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _HighImpactStage()},
            approval_gateway=None,
        )
        state = await engine.run(requirement)
        assert state.status == WorkflowStatus.FAILED

    async def test_high_impact_approval_record_stores_impact_level(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _HighImpactStage()},
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        assert len(state.approvals) == 1
        approval = state.approvals[0]
        assert approval.impact_level == ActionImpact.HIGH_IMPACT.value
        assert approval.action_type == HighImpactActionType.SCHEMA_MIGRATION.value

    async def test_high_impact_rejection_stores_decision_rationale(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _HighImpactStage()},
            approval_gateway=AutoRejectGateway(reason="Schema change not reviewed yet"),
        )
        state = await engine.run(requirement)

        assert len(state.approvals) == 1
        approval = state.approvals[0]
        assert "Schema change not reviewed yet" in approval.decision_rationale


# ─── 14: CRITICAL action block ────────────────────────────────────────────────


class TestCriticalActionBlock:
    async def test_critical_stage_blocked_regardless_of_gateway(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        """CRITICAL stages fail immediately — no gateway call is ever made."""
        gateway = AutoApproveGateway()  # would approve if asked
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _CriticalStage()},
            approval_gateway=gateway,
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["protected"].status.value == "failed"
        # No approval record because the engine never reaches the approval checkpoint
        assert len(state.approvals) == 0

    async def test_critical_stage_fail_event_in_audit_trail(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _CriticalStage()},
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "critical_action_blocked" in events

    async def test_critical_stage_error_message_mentions_human_only(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _CriticalStage()},
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        error = state.stages["protected"].error
        assert error is not None
        assert "HUMAN_ONLY" in error or "human" in error.lower()


# ─── 15: ROUTINE stage — no approval ──────────────────────────────────────────


class TestRoutineStageNoApproval:
    async def test_routine_stage_completes_without_gateway(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RoutineStage()},
            approval_gateway=None,  # no gateway — should not matter for ROUTINE
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert len(state.approvals) == 0  # no approval requested

    async def test_routine_stage_completes_without_approval_events(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RoutineStage()},
        )
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "approval_requested" not in events


# ─── 16-18: Final human QC checkpoint ────────────────────────────────────────


class TestFinalQCCheckpoint:
    async def test_final_qc_approved_workflow_completed(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RoutineStage()},
            approval_gateway=AutoApproveGateway(approver="qc-lead"),
            final_approval_required=True,
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        qc_approvals = [a for a in state.approvals if a.stage_name == "__final_qc__"]
        assert len(qc_approvals) == 1
        assert qc_approvals[0].status == ApprovalStatus.APPROVED
        assert qc_approvals[0].approver == "qc-lead"

    async def test_final_qc_rejected_workflow_fails(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RoutineStage()},
            approval_gateway=AutoRejectGateway(reason="QC standards not met"),
            final_approval_required=True,
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        qc_approvals = [a for a in state.approvals if a.stage_name == "__final_qc__"]
        assert len(qc_approvals) == 1
        assert qc_approvals[0].status == ApprovalStatus.REJECTED

    async def test_final_qc_no_gateway_fails(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RoutineStage()},
            approval_gateway=None,
            final_approval_required=True,
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        qc_approvals = [a for a in state.approvals if a.stage_name == "__final_qc__"]
        assert len(qc_approvals) == 1
        assert qc_approvals[0].status == ApprovalStatus.TIMED_OUT

    async def test_final_qc_not_requested_when_false(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RoutineStage()},
            final_approval_required=False,
        )
        state = await engine.run(requirement)

        qc_approvals = [a for a in state.approvals if a.stage_name == "__final_qc__"]
        assert len(qc_approvals) == 0


# ─── 19-21: Approval record content ──────────────────────────────────────────


class TestApprovalRecordContent:
    async def test_approved_record_has_approver_and_rationale(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=AutoApproveGateway(approver="alice@example.com"),
        )
        state = await engine.run(requirement)

        approval = state.approvals[0]
        assert approval.approver == "alice@example.com"
        assert approval.status == ApprovalStatus.APPROVED
        assert approval.decision_rationale != ""

    async def test_approval_record_has_impact_and_escalation(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _HighImpactStage()},
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        approval = state.approvals[0]
        assert approval.impact_level == "high_impact"
        assert approval.escalation_level == 0  # no escalation in this test

    async def test_escalated_approval_stores_escalation_level(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        gw = EscalatingApprovalGateway([
            AutoRejectGateway(reason="Tier-1 rejects"),
            AutoApproveGateway(approver="senior"),
        ])
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _HighImpactStage()},
            approval_gateway=gw,
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        approval = state.approvals[0]
        assert approval.escalation_level == 1
        assert approval.approver == "senior"


# ─── 21-22: Audit trail events ────────────────────────────────────────────────


class TestAuditTrailApprovalEvents:
    async def test_approved_stage_has_requested_and_resolved_events(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "approval_requested" in events
        assert "approval_resolved" in events

    async def test_rejected_stage_audit_shows_rejection(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=AutoRejectGateway(),
        )
        state = await engine.run(requirement)

        resolve_entries = [
            e for e in state.audit_trail if e.event == "approval_resolved"
        ]
        assert len(resolve_entries) == 1
        assert resolve_entries[0].details.get("approved") is False

    async def test_no_gateway_produces_timed_out_event(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=None,
        )
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "approval_timed_out" in events

    async def test_final_qc_requested_event_in_audit(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RoutineStage()},
            approval_gateway=AutoApproveGateway(),
            final_approval_required=True,
        )
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "final_qc_requested" in events
        assert "final_qc_resolved" in events


# ─── 24: Downstream stages unaffected by upstream approval ────────────────────


class TestDownstreamNotBlocked:
    async def test_approved_stage_allows_downstream_to_execute(
        self,
        requirement: Requirement,
        two_stage_definition: WorkflowDefinition,
    ) -> None:
        """After approval of 'protected', the 'downstream' stage must still run."""
        engine = WorkflowEngine(
            definition=two_stage_definition,
            stages={
                "protected": _RequiresApprovalStage(),
                "downstream": _DownstreamStage(),
            },
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["protected"].status.value == "completed"
        assert state.stages["downstream"].status.value == "completed"
        assert state.stages["downstream"].output_data.get("executed") is True

    async def test_rejected_upstream_blocks_downstream(
        self,
        requirement: Requirement,
        two_stage_definition: WorkflowDefinition,
    ) -> None:
        """When 'protected' is rejected, 'downstream' must be BLOCKED."""
        engine = WorkflowEngine(
            definition=two_stage_definition,
            stages={
                "protected": _RequiresApprovalStage(),
                "downstream": _DownstreamStage(),
            },
            approval_gateway=AutoRejectGateway(),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["protected"].status.value == "failed"
        assert state.stages["downstream"].status.value == "blocked"


# ─── 25: is_override propagation ─────────────────────────────────────────────


class TestIsOverride:
    async def test_override_decision_propagated_to_approval_record(
        self, requirement: Requirement, single_stage_definition: WorkflowDefinition
    ) -> None:
        """
        When an approver explicitly overrides an agent recommendation,
        the Approval audit record must carry is_override=True.
        """

        class _OverrideGateway:
            async def request_approval(
                self, request: ApprovalRequest
            ) -> ApprovalDecision:
                return ApprovalDecision(
                    request_id=request.id,
                    approved=True,
                    approver="vp-engineering",
                    rationale="Overriding team lead recommendation",
                    is_override=True,
                    override_reason="Business deadline requires immediate release",
                )

        engine = WorkflowEngine(
            definition=single_stage_definition,
            stages={"protected": _RequiresApprovalStage()},
            approval_gateway=_OverrideGateway(),  # type: ignore[arg-type]
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert len(state.approvals) == 1
        approval = state.approvals[0]
        assert approval.is_override is True
        assert approval.approver == "vp-engineering"


# ─── AgentAction model ────────────────────────────────────────────────────────


class TestAgentActionModel:
    def test_create_high_impact_action(self) -> None:
        action = AgentAction(
            title="Run schema migration",
            description="Add url_clicks table to the database",
            impact=ActionImpact.HIGH_IMPACT,
            action_type=HighImpactActionType.SCHEMA_MIGRATION,
            agent_name="db_agent",
            autonomy_mode=AgentAutonomyMode.RECOMMEND,
            is_destructive=False,
            is_irreversible=True,
        )
        assert action.impact == ActionImpact.HIGH_IMPACT
        assert action.autonomy_mode == AgentAutonomyMode.RECOMMEND
        assert action.is_irreversible is True

    def test_create_critical_action_as_recommend_only(self) -> None:
        action = AgentAction(
            title="Revoke all API keys",
            description="Emergency security response",
            impact=ActionImpact.CRITICAL,
            action_type=HighImpactActionType.SECURITY_CHANGE,
            agent_name="security_agent",
            autonomy_mode=AgentAutonomyMode.RECOMMEND,  # must be RECOMMEND for CRITICAL
            is_security_sensitive=True,
            is_irreversible=True,
        )
        assert action.impact == ActionImpact.CRITICAL
        assert action.autonomy_mode == AgentAutonomyMode.RECOMMEND

    def test_high_impact_action_types_are_all_distinct(self) -> None:
        values = {t.value for t in HighImpactActionType}
        assert len(values) == len(HighImpactActionType)

    def test_all_required_categories_present(self) -> None:
        """The five required high-impact categories must be in HighImpactActionType."""
        required = {
            "production_release",
            "destructive_change",
            "security_change",
            "schema_migration",
            "high_risk_code",
        }
        actual = {t.value for t in HighImpactActionType}
        assert required <= actual
