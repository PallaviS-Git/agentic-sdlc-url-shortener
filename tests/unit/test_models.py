"""
Unit tests for orchestrator core domain models.

No I/O, no DB, no network. Pure model contract verification.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchestrator.core.models import (
    AmbiguityItem,
    AuditEntry,
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    StageStatus,
    Task,
    TaskStatus,
    WorkflowState,
    WorkflowStatus,
)


# ─── Requirement ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestRequirement:
    def test_id_is_generated(self, greenfield_requirement: Requirement) -> None:
        assert greenfield_requirement.id
        assert len(greenfield_requirement.id) == 36  # UUID4 string

    def test_created_at_is_utc_aware(self, greenfield_requirement: Requirement) -> None:
        assert greenfield_requirement.created_at.tzinfo is not None

    def test_defaults_are_empty_lists(self) -> None:
        req = Requirement(
            title="T",
            raw_text="raw",
            requirement_type=RequirementType.GREENFIELD,
        )
        assert req.ambiguities == []
        assert req.constraints == []
        assert req.acceptance_criteria == []
        assert req.normalized_text is None

    def test_is_fully_resolved_true_when_no_ambiguities(
        self, greenfield_requirement: Requirement
    ) -> None:
        assert greenfield_requirement.is_fully_resolved  # no ambiguities

    def test_is_fully_resolved_false_when_unresolved_items(
        self, ambiguous_requirement: Requirement
    ) -> None:
        assert not ambiguous_requirement.is_fully_resolved

    def test_is_fully_resolved_true_after_all_resolved(
        self, ambiguous_requirement: Requirement
    ) -> None:
        for item in ambiguous_requirement.ambiguities:
            item.resolved = True
            item.resolution = "resolved"
        assert ambiguous_requirement.is_fully_resolved

    def test_ambiguity_item_defaults(self) -> None:
        item = AmbiguityItem(field="scope", description="unclear scope")
        assert not item.resolved
        assert item.resolution is None
        assert item.resolved_at is None


# ─── Task ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestTask:
    def test_default_status_is_pending(self) -> None:
        t = Task(title="T", description="D")
        assert t.status == TaskStatus.PENDING

    def test_depends_on_empty_by_default(self) -> None:
        t = Task(title="T", description="D")
        assert t.depends_on == []

    def test_dependency_chain(self) -> None:
        parent = Task(title="Parent", description="P")
        child = Task(title="Child", description="C", depends_on=[parent.id])
        assert parent.id in child.depends_on

    def test_artifacts_empty_by_default(self) -> None:
        t = Task(title="T", description="D")
        assert t.artifacts == {}

    def test_unique_ids(self) -> None:
        t1 = Task(title="T1", description="D")
        t2 = Task(title="T2", description="D")
        assert t1.id != t2.id


# ─── GateResult ───────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestGateResult:
    def test_passing_gate(self) -> None:
        result = GateResult(passed=True, gate_name="entry")
        assert result.passed
        assert result.reason is None

    def test_failing_gate_with_reason(self) -> None:
        result = GateResult(
            passed=False,
            gate_name="exit",
            reason="test coverage below threshold",
        )
        assert not result.passed
        assert "coverage" in result.reason

    def test_evaluated_at_is_utc_aware(self) -> None:
        result = GateResult(passed=True, gate_name="entry")
        assert result.evaluated_at.tzinfo is not None


# ─── StageContext ──────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestStageContext:
    def test_defaults(self, empty_stage_context: StageContext) -> None:
        ctx = empty_stage_context
        assert ctx.status == StageStatus.PENDING
        assert ctx.attempt == 0
        assert ctx.max_attempts == 3
        assert ctx.input_data == {}
        assert ctx.output_data == {}
        assert not ctx.rollback_performed

    def test_has_retries_remaining_initially(
        self, empty_stage_context: StageContext
    ) -> None:
        assert empty_stage_context.has_retries_remaining

    def test_has_retries_remaining_false_when_exhausted(
        self, empty_stage_context: StageContext
    ) -> None:
        empty_stage_context.attempt = 3
        assert not empty_stage_context.has_retries_remaining

    def test_entry_passed_false_when_no_results(
        self, empty_stage_context: StageContext
    ) -> None:
        assert not empty_stage_context.entry_passed

    def test_entry_passed_true_when_all_pass(
        self, empty_stage_context: StageContext
    ) -> None:
        empty_stage_context.entry_gate_results = [
            GateResult(passed=True, gate_name="precondition_check"),
            GateResult(passed=True, gate_name="policy_check"),
        ]
        assert empty_stage_context.entry_passed

    def test_entry_passed_false_when_any_fail(
        self, empty_stage_context: StageContext
    ) -> None:
        empty_stage_context.entry_gate_results = [
            GateResult(passed=True, gate_name="precondition_check"),
            GateResult(passed=False, gate_name="policy_check", reason="blocked"),
        ]
        assert not empty_stage_context.entry_passed

    def test_exit_passed_false_when_no_results(
        self, empty_stage_context: StageContext
    ) -> None:
        assert not empty_stage_context.exit_passed


# ─── WorkflowState ────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestWorkflowState:
    def test_initial_status_is_pending(self, workflow_state: WorkflowState) -> None:
        assert workflow_state.status == WorkflowStatus.PENDING

    def test_audit_trail_empty_initially(self, workflow_state: WorkflowState) -> None:
        assert workflow_state.audit_trail == []

    def test_add_audit_entry_appends(self, workflow_state: WorkflowState) -> None:
        workflow_state.add_audit_entry("workflow_started", details={"trigger": "manual"})
        assert len(workflow_state.audit_trail) == 1
        entry = workflow_state.audit_trail[0]
        assert entry.event == "workflow_started"
        assert entry.details["trigger"] == "manual"
        assert entry.actor == "orchestrator"

    def test_add_audit_entry_bumps_updated_at(
        self, workflow_state: WorkflowState
    ) -> None:
        before = workflow_state.updated_at
        workflow_state.add_audit_entry("stage_started", stage="requirements")
        assert workflow_state.updated_at >= before

    def test_add_audit_entry_with_human_actor(
        self, workflow_state: WorkflowState
    ) -> None:
        workflow_state.add_audit_entry(
            "approval_granted", stage="release", actor="human:admin"
        )
        assert workflow_state.audit_trail[0].actor == "human:admin"

    def test_set_stage_updates_current_stage(
        self, workflow_state: WorkflowState, empty_stage_context: StageContext
    ) -> None:
        workflow_state.set_stage(empty_stage_context)
        assert workflow_state.current_stage == "requirements"
        assert "requirements" in workflow_state.stages

    def test_get_stage_returns_none_for_unknown(
        self, workflow_state: WorkflowState
    ) -> None:
        assert workflow_state.get_stage("nonexistent") is None

    def test_get_stage_returns_context_after_set(
        self, workflow_state: WorkflowState, empty_stage_context: StageContext
    ) -> None:
        workflow_state.set_stage(empty_stage_context)
        retrieved = workflow_state.get_stage("requirements")
        assert retrieved is not None
        assert retrieved.stage_name == "requirements"

    def test_multiple_audit_entries_preserve_order(
        self, workflow_state: WorkflowState
    ) -> None:
        events = ["workflow_started", "stage_started", "gate_evaluated", "stage_completed"]
        for event in events:
            workflow_state.add_audit_entry(event)
        recorded = [e.event for e in workflow_state.audit_trail]
        assert recorded == events

    def test_workflow_id_is_unique(
        self, greenfield_requirement: Requirement
    ) -> None:
        w1 = WorkflowState(requirement=greenfield_requirement)
        w2 = WorkflowState(requirement=greenfield_requirement)
        assert w1.id != w2.id
