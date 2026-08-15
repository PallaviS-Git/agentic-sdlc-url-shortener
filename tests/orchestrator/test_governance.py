"""
Tests for the governance and policy guardrail layer.

Coverage
────────
Unit (PolicyEngine + built-in policies):
  1.  ALLOW — no policies triggered → ALLOW decision, no violations
  2.  Aggregation — worst decision wins (BLOCK > REQUIRE_APPROVAL > WARN > ALLOW)
  3.  Policy exception → treated as BLOCK (fail-safe)
  4.  SEC-001 RequireSecurityScanForRelease — blocks release without scan
  5.  SEC-001 RequireSecurityScanForRelease — allows release with scan
  6.  SEC-001 — non-release action not affected
  7.  SEC-002 ProtectPiiData — blocks PII access without approval
  8.  SEC-002 — allows PII access with approval
  9.  SEC-002 — non-PII action not affected
  10. SEC-003 WarnOnHighRiskAction — WARN (does not block)
  11. COMP-001 RequireChangeTicket — blocks SIGNIFICANT without ticket
  12. COMP-001 — allows SIGNIFICANT with ticket
  13. COMP-001 — ROUTINE not affected
  14. COMP-002 EnforceDataRetentionPolicy — blocks data deletion without confirmation
  15. COMP-002 — allows data deletion with confirmation
  16. CC-001 RequireApprovalForProduction — returns REQUIRE_APPROVAL for production release
  17. CC-001 — non-production action not affected
  18. CC-002 RequireRollbackPlan — blocks HIGH_IMPACT without rollback plan
  19. CC-002 — allows HIGH_IMPACT with rollback plan
  20. CC-002 — SIGNIFICANT action not affected
  21. CC-003 FreezeWindowPolicy — blocks change inside freeze window
  22. CC-003 — allows change outside freeze window
  23. CC-003 — ROUTINE action not affected by freeze (default minimum_impact=SIGNIFICANT)
  24. CC-003 — custom now_fn makes test deterministic

Integration (through WorkflowEngine):
  25. Allowed action — no policy engine, stage executes normally
  26. Allowed action — policy engine with ALLOW result, stage executes normally
  27. Blocked security action — SEC-001 BLOCK → stage FAILED, workflow FAILED
  28. Blocked compliance action — COMP-001 BLOCK → stage FAILED, workflow FAILED
  29. Blocked change-control action — CC-002 BLOCK → stage FAILED, workflow FAILED
  30. WARN action — stage executes despite WARN violation
  31. Approval-required action — CC-001 REQUIRE_APPROVAL, gateway approves → stage COMPLETED
  32. Approval-required action — CC-001 REQUIRE_APPROVAL, gateway rejects → stage FAILED
  33. Approval-required action — CC-001 REQUIRE_APPROVAL, no gateway → stage FAILED
  34. Policy evaluation failure — broken policy → BLOCK (fail-safe) → stage FAILED
  35. Policy evaluation record stored in WorkflowState.policy_evaluations
  36. Blocked stage audit event 'policy_blocked' emitted
  37. ALLOW/WARN stages emit 'policy_evaluated' audit event
  38. Downstream stages BLOCKED when upstream is governance-blocked
  39. Multiple policies — all evaluated even when earlier one fires BLOCK

asyncio_mode=auto (pytest.ini).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from orchestrator.core.autonomy import ActionImpact, HighImpactActionType
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.governance import (
    ActionContext,
    EnforcementDecision,
    EnforceDataRetentionPolicy,
    FreezeWindowPolicy,
    Policy,
    PolicyDomain,
    PolicyEngine,
    PolicyEvaluationRecord,
    PolicyViolation,
    ProtectPiiData,
    RequireApprovalForProduction,
    RequireChangeTicket,
    RequireRollbackPlan,
    RequireSecurityScanForRelease,
    WarnOnHighRiskAction,
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
from orchestrator.core.autonomy import AutoApproveGateway, AutoRejectGateway


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _ctx(
    *,
    action_impact: ActionImpact = ActionImpact.ROUTINE,
    action_type: HighImpactActionType | None = None,
    metadata: dict[str, Any] | None = None,
    workflow_id: str = "wf-test",
    stage_name: str = "test_stage",
) -> ActionContext:
    return ActionContext(
        workflow_id=workflow_id,
        stage_name=stage_name,
        action_impact=action_impact,
        action_type=action_type,
        metadata=metadata or {},
    )


def _engine(*policies: Policy) -> PolicyEngine:
    return PolicyEngine(policies=list(policies))


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


class _RoutineStage(_BaseTestStage):
    """ROUTINE stage — no policy constraints."""
    stage_name = "routine"
    action_impact = ActionImpact.ROUTINE


class _ReleaseStageNoScan(_BaseTestStage):
    """Production release WITHOUT passing security scan → SEC-001 fires."""
    stage_name = "release"
    action_impact = ActionImpact.HIGH_IMPACT
    high_impact_action_type = HighImpactActionType.PRODUCTION_RELEASE
    policy_metadata = {"security_scan_passed": False}


class _ReleaseStageWithScan(_BaseTestStage):
    """Production release WITH passing security scan → SEC-001 allows."""
    stage_name = "release"
    action_impact = ActionImpact.HIGH_IMPACT
    high_impact_action_type = HighImpactActionType.PRODUCTION_RELEASE
    policy_metadata = {
        "security_scan_passed": True,
        "change_ticket_id": "CHG-9999",
        "rollback_plan_documented": True,
    }


class _SignificantNoTicket(_BaseTestStage):
    """SIGNIFICANT action without a change ticket → COMP-001 fires."""
    stage_name = "significant"
    action_impact = ActionImpact.SIGNIFICANT
    policy_metadata = {}


class _SignificantWithTicket(_BaseTestStage):
    """SIGNIFICANT action with a change ticket → COMP-001 allows."""
    stage_name = "significant"
    action_impact = ActionImpact.SIGNIFICANT
    policy_metadata = {"change_ticket_id": "CHG-0001"}


class _HighImpactNoRollbackPlan(_BaseTestStage):
    """HIGH_IMPACT without rollback plan → CC-002 fires."""
    stage_name = "high_impact"
    action_impact = ActionImpact.HIGH_IMPACT
    policy_metadata = {}


class _WarnStage(_BaseTestStage):
    """Stage with high_risk_action=True → SEC-003 emits WARN."""
    stage_name = "warn_stage"
    action_impact = ActionImpact.ROUTINE
    policy_metadata = {"high_risk_action": True}


class _ProductionReleaseStage(_BaseTestStage):
    """Production release → CC-001 requires approval."""
    stage_name = "prod_release"
    action_impact = ActionImpact.HIGH_IMPACT
    high_impact_action_type = HighImpactActionType.PRODUCTION_RELEASE
    policy_metadata = {
        "security_scan_passed": True,
        "change_ticket_id": "CHG-1111",
        "rollback_plan_documented": True,
    }


class _DownstreamStage(_BaseTestStage):
    stage_name = "downstream"


def _single(stage: BaseStage) -> tuple[WorkflowDefinition, dict]:
    defn = WorkflowDefinition(name="test", description="", stages=[stage.stage_name])
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


@pytest.fixture()
def requirement() -> Requirement:
    return Requirement(
        title="Governance test",
        raw_text="Test governance guardrails",
        requirement_type=RequirementType.GREENFIELD,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PolicyEngine unit tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPolicyEngineAggregation:
    def test_no_policies_returns_allow(self) -> None:
        engine = _engine()
        record = engine.evaluate(_ctx())
        assert record.final_decision == EnforcementDecision.ALLOW
        assert record.violations == []

    def test_all_allow_returns_allow(self) -> None:
        engine = _engine(RequireChangeTicket())
        # ROUTINE → not applicable → ALLOW
        record = engine.evaluate(_ctx(action_impact=ActionImpact.ROUTINE))
        assert record.final_decision == EnforcementDecision.ALLOW

    def test_worst_decision_wins_block_over_warn(self) -> None:
        engine = _engine(
            WarnOnHighRiskAction(),         # WARN
            RequireSecurityScanForRelease(), # BLOCK (for PRODUCTION_RELEASE without scan)
        )
        ctx = _ctx(
            action_impact=ActionImpact.HIGH_IMPACT,
            action_type=HighImpactActionType.PRODUCTION_RELEASE,
            metadata={"high_risk_action": True, "security_scan_passed": False},
        )
        record = engine.evaluate(ctx)
        assert record.final_decision == EnforcementDecision.BLOCK

    def test_worst_decision_wins_require_approval_over_warn(self) -> None:
        engine = _engine(
            WarnOnHighRiskAction(),          # WARN
            RequireApprovalForProduction(),  # REQUIRE_APPROVAL
        )
        ctx = _ctx(
            action_impact=ActionImpact.HIGH_IMPACT,
            action_type=HighImpactActionType.PRODUCTION_RELEASE,
            metadata={"high_risk_action": True},
        )
        record = engine.evaluate(ctx)
        assert record.final_decision == EnforcementDecision.REQUIRE_APPROVAL

    def test_all_policies_evaluated_even_when_one_blocks(self) -> None:
        """All policies must run regardless of intermediate decisions."""
        engine = _engine(
            RequireSecurityScanForRelease(),  # BLOCK
            RequireRollbackPlan(),            # BLOCK
        )
        ctx = _ctx(
            action_impact=ActionImpact.HIGH_IMPACT,
            action_type=HighImpactActionType.PRODUCTION_RELEASE,
            metadata={},
        )
        record = engine.evaluate(ctx)
        assert record.final_decision == EnforcementDecision.BLOCK
        # Both policies should have fired
        violated_ids = {v.policy_id for v in record.violations}
        assert "SEC-001" in violated_ids
        assert "CC-002" in violated_ids

    def test_policy_exception_becomes_block(self) -> None:
        class _BrokenPolicy(Policy):
            policy_id = "TEST-BROKEN"
            domain = PolicyDomain.SECURITY
            description = "Always raises"

            def evaluate(self, context: ActionContext) -> PolicyViolation | None:
                raise RuntimeError("Simulated broken policy")

        engine = _engine(_BrokenPolicy())
        record = engine.evaluate(_ctx())
        assert record.final_decision == EnforcementDecision.BLOCK
        assert len(record.violations) == 1
        assert "TEST-BROKEN" == record.violations[0].policy_id
        assert "RuntimeError" in record.violations[0].evidence["error_type"]

    def test_evaluation_record_captures_metadata(self) -> None:
        engine = _engine()
        ctx = _ctx(
            workflow_id="wf-42",
            stage_name="my_stage",
            action_impact=ActionImpact.SIGNIFICANT,
        )
        record = engine.evaluate(ctx)
        assert record.workflow_id == "wf-42"
        assert record.stage_name == "my_stage"
        assert record.action_impact == ActionImpact.SIGNIFICANT.value
        assert record.evaluated_by == "policy_engine"


# ═══════════════════════════════════════════════════════════════════════════════
# Security policies — unit
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequireSecurityScanForRelease:
    def test_blocks_production_release_without_scan(self) -> None:
        policy = RequireSecurityScanForRelease()
        ctx = _ctx(
            action_type=HighImpactActionType.PRODUCTION_RELEASE,
            metadata={"security_scan_passed": False},
        )
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.BLOCK
        assert violation.policy_id == "SEC-001"

    def test_allows_production_release_with_scan(self) -> None:
        policy = RequireSecurityScanForRelease()
        ctx = _ctx(
            action_type=HighImpactActionType.PRODUCTION_RELEASE,
            metadata={"security_scan_passed": True},
        )
        assert policy.evaluate(ctx) is None

    def test_non_release_action_not_affected(self) -> None:
        policy = RequireSecurityScanForRelease()
        ctx = _ctx(
            action_type=HighImpactActionType.SCHEMA_MIGRATION,
            metadata={},
        )
        assert policy.evaluate(ctx) is None

    def test_no_action_type_not_affected(self) -> None:
        policy = RequireSecurityScanForRelease()
        ctx = _ctx(metadata={})
        assert policy.evaluate(ctx) is None


class TestProtectPiiData:
    def test_blocks_pii_access_without_approval(self) -> None:
        policy = ProtectPiiData()
        ctx = _ctx(metadata={"pii_data_access": True, "pii_approved": False})
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.BLOCK
        assert violation.policy_id == "SEC-002"

    def test_allows_pii_access_with_approval(self) -> None:
        policy = ProtectPiiData()
        ctx = _ctx(metadata={"pii_data_access": True, "pii_approved": True})
        assert policy.evaluate(ctx) is None

    def test_non_pii_action_not_affected(self) -> None:
        policy = ProtectPiiData()
        ctx = _ctx(metadata={})
        assert policy.evaluate(ctx) is None


class TestWarnOnHighRiskAction:
    def test_warn_for_high_risk_flag(self) -> None:
        policy = WarnOnHighRiskAction()
        ctx = _ctx(metadata={"high_risk_action": True})
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.WARN
        assert violation.policy_id == "SEC-003"

    def test_no_warn_without_flag(self) -> None:
        policy = WarnOnHighRiskAction()
        ctx = _ctx(metadata={})
        assert policy.evaluate(ctx) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Compliance policies — unit
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequireChangeTicket:
    def test_blocks_significant_without_ticket(self) -> None:
        policy = RequireChangeTicket()
        ctx = _ctx(action_impact=ActionImpact.SIGNIFICANT, metadata={})
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.BLOCK
        assert violation.policy_id == "COMP-001"

    def test_blocks_high_impact_without_ticket(self) -> None:
        policy = RequireChangeTicket()
        ctx = _ctx(action_impact=ActionImpact.HIGH_IMPACT, metadata={})
        violation = policy.evaluate(ctx)
        assert violation is not None

    def test_allows_significant_with_ticket(self) -> None:
        policy = RequireChangeTicket()
        ctx = _ctx(
            action_impact=ActionImpact.SIGNIFICANT,
            metadata={"change_ticket_id": "CHG-0042"},
        )
        assert policy.evaluate(ctx) is None

    def test_routine_not_affected(self) -> None:
        policy = RequireChangeTicket()
        ctx = _ctx(action_impact=ActionImpact.ROUTINE, metadata={})
        assert policy.evaluate(ctx) is None


class TestEnforceDataRetentionPolicy:
    def test_blocks_data_deletion_without_confirmation(self) -> None:
        policy = EnforceDataRetentionPolicy()
        ctx = _ctx(metadata={"data_deletion": True})
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.BLOCK
        assert violation.policy_id == "COMP-002"

    def test_allows_deletion_with_confirmation(self) -> None:
        policy = EnforceDataRetentionPolicy()
        ctx = _ctx(
            metadata={"data_deletion": True, "retention_policy_checked": True}
        )
        assert policy.evaluate(ctx) is None

    def test_non_deletion_action_not_affected(self) -> None:
        policy = EnforceDataRetentionPolicy()
        ctx = _ctx(metadata={})
        assert policy.evaluate(ctx) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Change-control policies — unit
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequireApprovalForProduction:
    def test_requires_approval_for_production_release(self) -> None:
        policy = RequireApprovalForProduction()
        ctx = _ctx(action_type=HighImpactActionType.PRODUCTION_RELEASE)
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.REQUIRE_APPROVAL
        assert violation.policy_id == "CC-001"

    def test_non_production_action_not_affected(self) -> None:
        policy = RequireApprovalForProduction()
        ctx = _ctx(action_type=HighImpactActionType.SCHEMA_MIGRATION)
        assert policy.evaluate(ctx) is None

    def test_no_action_type_not_affected(self) -> None:
        policy = RequireApprovalForProduction()
        ctx = _ctx()
        assert policy.evaluate(ctx) is None


class TestRequireRollbackPlan:
    def test_blocks_high_impact_without_plan(self) -> None:
        policy = RequireRollbackPlan()
        ctx = _ctx(action_impact=ActionImpact.HIGH_IMPACT, metadata={})
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.BLOCK
        assert violation.policy_id == "CC-002"

    def test_allows_high_impact_with_plan(self) -> None:
        policy = RequireRollbackPlan()
        ctx = _ctx(
            action_impact=ActionImpact.HIGH_IMPACT,
            metadata={"rollback_plan_documented": True},
        )
        assert policy.evaluate(ctx) is None

    def test_significant_not_affected(self) -> None:
        policy = RequireRollbackPlan()
        ctx = _ctx(action_impact=ActionImpact.SIGNIFICANT, metadata={})
        assert policy.evaluate(ctx) is None


class TestFreezeWindowPolicy:
    _EPOCH = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _policy_with_fixed_clock(
        self,
        *,
        is_frozen: bool,
        minimum_impact: ActionImpact = ActionImpact.SIGNIFICANT,
    ) -> FreezeWindowPolicy:
        if is_frozen:
            start = self._EPOCH - timedelta(hours=1)
            end = self._EPOCH + timedelta(hours=1)
        else:
            start = self._EPOCH + timedelta(days=10)
            end = self._EPOCH + timedelta(days=11)

        return FreezeWindowPolicy(
            freeze_windows=[(start, end)],
            minimum_impact=minimum_impact,
            now_fn=lambda: self._EPOCH,
        )

    def test_blocks_change_inside_freeze_window(self) -> None:
        policy = self._policy_with_fixed_clock(is_frozen=True)
        ctx = _ctx(action_impact=ActionImpact.SIGNIFICANT)
        violation = policy.evaluate(ctx)
        assert violation is not None
        assert violation.decision == EnforcementDecision.BLOCK
        assert violation.policy_id == "CC-003"

    def test_allows_change_outside_freeze_window(self) -> None:
        policy = self._policy_with_fixed_clock(is_frozen=False)
        ctx = _ctx(action_impact=ActionImpact.SIGNIFICANT)
        assert policy.evaluate(ctx) is None

    def test_routine_not_affected_by_default_minimum_impact(self) -> None:
        policy = self._policy_with_fixed_clock(is_frozen=True)
        ctx = _ctx(action_impact=ActionImpact.ROUTINE)
        # ROUTINE < SIGNIFICANT (default minimum_impact) → not affected
        assert policy.evaluate(ctx) is None

    def test_high_impact_affected_inside_freeze_window(self) -> None:
        policy = self._policy_with_fixed_clock(is_frozen=True)
        ctx = _ctx(action_impact=ActionImpact.HIGH_IMPACT)
        violation = policy.evaluate(ctx)
        assert violation is not None

    def test_no_freeze_windows_always_allows(self) -> None:
        policy = FreezeWindowPolicy(freeze_windows=[])
        ctx = _ctx(action_impact=ActionImpact.HIGH_IMPACT)
        assert policy.evaluate(ctx) is None


# ═══════════════════════════════════════════════════════════════════════════════
# Integration tests — through WorkflowEngine
# ═══════════════════════════════════════════════════════════════════════════════


class TestGovernanceIntegrationAllowed:
    async def test_no_policy_engine_stage_executes(
        self, requirement: Requirement
    ) -> None:
        stage = _RoutineStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["routine"].output_data.get("ran") is True

    async def test_policy_engine_allow_stage_executes(
        self, requirement: Requirement
    ) -> None:
        stage = _RoutineStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireChangeTicket()]),
        )
        state = await engine.run(requirement)

        # ROUTINE → RequireChangeTicket does not apply → ALLOW
        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["routine"].output_data.get("ran") is True
        # Policy evaluation record stored
        assert len(state.policy_evaluations) == 1
        assert state.policy_evaluations[0].final_decision == EnforcementDecision.ALLOW

    async def test_policy_evaluated_audit_event_emitted(
        self, requirement: Requirement
    ) -> None:
        stage = _RoutineStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireChangeTicket()]),
        )
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "policy_evaluated" in events


class TestGovernanceIntegrationBlocked:
    async def test_blocked_security_action_stage_fails(
        self, requirement: Requirement
    ) -> None:
        stage = _ReleaseStageNoScan()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireSecurityScanForRelease()]),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["release"].status.value == "failed"

    async def test_blocked_security_policy_blocked_audit_event(
        self, requirement: Requirement
    ) -> None:
        stage = _ReleaseStageNoScan()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireSecurityScanForRelease()]),
        )
        state = await engine.run(requirement)

        events = {e.event for e in state.audit_trail}
        assert "policy_blocked" in events

    async def test_blocked_compliance_action_stage_fails(
        self, requirement: Requirement
    ) -> None:
        stage = _SignificantNoTicket()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireChangeTicket()]),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["significant"].status.value == "failed"

    async def test_blocked_change_control_stage_fails(
        self, requirement: Requirement
    ) -> None:
        stage = _HighImpactNoRollbackPlan()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireRollbackPlan()]),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["high_impact"].status.value == "failed"

    async def test_blocked_stage_blocks_downstream(
        self, requirement: Requirement
    ) -> None:
        upstream = _ReleaseStageNoScan()
        downstream = _DownstreamStage()
        defn, stages = _linear(upstream, downstream)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireSecurityScanForRelease()]),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["downstream"].status.value == "blocked"

    async def test_policy_evaluation_record_stored_on_block(
        self, requirement: Requirement
    ) -> None:
        stage = _ReleaseStageNoScan()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireSecurityScanForRelease()]),
        )
        state = await engine.run(requirement)

        assert len(state.policy_evaluations) == 1
        record = state.policy_evaluations[0]
        assert record.final_decision == EnforcementDecision.BLOCK
        assert any(v.policy_id == "SEC-001" for v in record.violations)


class TestGovernanceIntegrationWarn:
    async def test_warn_action_executes_despite_violation(
        self, requirement: Requirement
    ) -> None:
        stage = _WarnStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[WarnOnHighRiskAction()]),
        )
        state = await engine.run(requirement)

        # WARN → execution proceeds
        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["warn_stage"].output_data.get("ran") is True

    async def test_warn_violation_recorded_in_policy_evaluations(
        self, requirement: Requirement
    ) -> None:
        stage = _WarnStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[WarnOnHighRiskAction()]),
        )
        state = await engine.run(requirement)

        assert len(state.policy_evaluations) == 1
        record = state.policy_evaluations[0]
        assert record.final_decision == EnforcementDecision.WARN
        assert any(v.decision == EnforcementDecision.WARN for v in record.violations)


class TestGovernanceIntegrationRequireApproval:
    async def test_require_approval_gateway_approves_stage_completes(
        self, requirement: Requirement
    ) -> None:
        stage = _ProductionReleaseStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireApprovalForProduction()]),
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED

    async def test_require_approval_gateway_rejects_stage_fails(
        self, requirement: Requirement
    ) -> None:
        stage = _ProductionReleaseStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireApprovalForProduction()]),
            approval_gateway=AutoRejectGateway(),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED

    async def test_require_approval_no_gateway_stage_fails(
        self, requirement: Requirement
    ) -> None:
        """No approval gateway → fail-safe: block execution."""
        stage = _ProductionReleaseStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireApprovalForProduction()]),
            # No approval_gateway
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED

    async def test_require_approval_record_stored(
        self, requirement: Requirement
    ) -> None:
        stage = _ProductionReleaseStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[RequireApprovalForProduction()]),
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        assert len(state.policy_evaluations) == 1
        assert state.policy_evaluations[0].final_decision == EnforcementDecision.REQUIRE_APPROVAL


class TestGovernanceIntegrationEvaluationFailure:
    async def test_broken_policy_blocks_execution(
        self, requirement: Requirement
    ) -> None:
        class _BrokenPolicy(Policy):
            policy_id = "TEST-BROKEN"
            domain = PolicyDomain.SECURITY
            description = "Always raises"

            def evaluate(self, context: ActionContext) -> PolicyViolation | None:
                raise RuntimeError("Simulated broken policy")

        stage = _RoutineStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[_BrokenPolicy()]),
        )
        state = await engine.run(requirement)

        # Fail-safe: broken policy → BLOCK
        assert state.status == WorkflowStatus.FAILED
        assert any(
            v.policy_id == "TEST-BROKEN" and v.decision == EnforcementDecision.BLOCK
            for record in state.policy_evaluations
            for v in record.violations
        )

    async def test_broken_policy_audit_record_includes_error(
        self, requirement: Requirement
    ) -> None:
        class _BrokenPolicy(Policy):
            policy_id = "BROKEN-2"
            domain = PolicyDomain.COMPLIANCE
            description = "Raises ValueError"

            def evaluate(self, context: ActionContext) -> PolicyViolation | None:
                raise ValueError("Unexpected condition")

        stage = _RoutineStage()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(policies=[_BrokenPolicy()]),
        )
        state = await engine.run(requirement)

        violation = state.policy_evaluations[0].violations[0]
        assert "ValueError" in violation.evidence["error_type"]
        assert "Unexpected condition" in violation.evidence["error"]


class TestGovernanceIntegrationAllPoliciesRun:
    async def test_all_policies_evaluated_before_block(
        self, requirement: Requirement
    ) -> None:
        """All policies run even if the first one fires BLOCK."""
        stage = _ReleaseStageNoScan()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(
                policies=[
                    RequireSecurityScanForRelease(),  # BLOCK (no scan)
                    RequireRollbackPlan(),             # BLOCK (no rollback plan)
                ]
            ),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.FAILED
        record = state.policy_evaluations[0]
        violated_ids = {v.policy_id for v in record.violations}
        assert "SEC-001" in violated_ids
        assert "CC-002" in violated_ids


class TestGovernanceIntegrationPassesThrough:
    async def test_stage_with_all_preconditions_executes(
        self, requirement: Requirement
    ) -> None:
        """Stage satisfying all policies executes normally.
        The stage is HIGH_IMPACT so the default autonomy policy requires
        approval — supply AutoApproveGateway so the test focuses on policy."""
        stage = _ReleaseStageWithScan()
        defn, stages = _single(stage)
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            policy_engine=PolicyEngine(
                policies=[
                    RequireSecurityScanForRelease(),
                    RequireChangeTicket(),
                    RequireRollbackPlan(),
                ]
            ),
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.run(requirement)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["release"].output_data.get("ran") is True
