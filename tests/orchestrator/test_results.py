"""
Unit tests for execution output domain types.

Covers: Artifact, ExecutionResult, ValidationResult, Risk, Decision, Approval.
No I/O, no network, no DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchestrator.core.results import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactStatus,
    ArtifactType,
    Decision,
    DecisionType,
    ExecutionResult,
    ExecutionStatus,
    Risk,
    RiskSeverity,
    ValidationResult,
    ValidationSeverity,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ─── Artifact ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestArtifact:
    def test_defaults(self) -> None:
        a = Artifact(
            name="url_service.py",
            artifact_type=ArtifactType.CODE,
            produced_by_stage="implementation",
        )
        assert a.status == ArtifactStatus.DRAFT
        assert a.content is None
        assert a.path is None
        assert a.produced_by_agent is None
        assert isinstance(a.id, str) and len(a.id) > 0

    def test_is_available_with_content(self) -> None:
        a = Artifact(
            name="schema.sql",
            artifact_type=ArtifactType.SCHEMA,
            produced_by_stage="design",
            content="CREATE TABLE urls (...);",
        )
        assert a.is_available

    def test_is_available_with_path(self) -> None:
        a = Artifact(
            name="report.md",
            artifact_type=ArtifactType.REPORT,
            produced_by_stage="testing",
            path="/tmp/report.md",
        )
        assert a.is_available

    def test_is_not_available_when_no_content_or_path(self) -> None:
        a = Artifact(
            name="ghost.py",
            artifact_type=ArtifactType.CODE,
            produced_by_stage="implementation",
        )
        assert not a.is_available

    def test_is_validated_when_status_validated(self) -> None:
        a = Artifact(
            name="test_suite.py",
            artifact_type=ArtifactType.TEST,
            produced_by_stage="testing",
            status=ArtifactStatus.VALIDATED,
        )
        assert a.is_validated

    def test_is_not_validated_when_draft(self) -> None:
        a = Artifact(
            name="test_suite.py",
            artifact_type=ArtifactType.TEST,
            produced_by_stage="testing",
        )
        assert not a.is_validated

    def test_unique_ids_per_instance(self) -> None:
        a1 = Artifact(
            name="a.py", artifact_type=ArtifactType.CODE, produced_by_stage="s"
        )
        a2 = Artifact(
            name="b.py", artifact_type=ArtifactType.CODE, produced_by_stage="s"
        )
        assert a1.id != a2.id

    def test_all_artifact_types_accepted(self) -> None:
        for t in ArtifactType:
            a = Artifact(name="f", artifact_type=t, produced_by_stage="s")
            assert a.artifact_type == t


# ─── ExecutionResult ──────────────────────────────────────────────────────────


@pytest.mark.unit
class TestExecutionResult:
    def _make_result(self, status: ExecutionStatus = ExecutionStatus.SUCCESS) -> ExecutionResult:
        return ExecutionResult(
            task_id="task-1",
            agent_name="requirements_agent",
            status=status,
        )

    def test_defaults(self) -> None:
        r = self._make_result()
        assert r.artifacts == []
        assert r.decisions == []
        assert r.validations == []
        assert r.risks == []
        assert r.completed_at is None
        assert r.duration_ms is None
        assert r.error is None

    def test_succeeded_property(self) -> None:
        assert self._make_result(ExecutionStatus.SUCCESS).succeeded
        assert not self._make_result(ExecutionStatus.FAILURE).succeeded

    def test_failed_property(self) -> None:
        assert self._make_result(ExecutionStatus.FAILURE).failed
        assert not self._make_result(ExecutionStatus.SUCCESS).failed

    def test_mark_complete_sets_status_and_timestamp(self) -> None:
        r = self._make_result()
        r.mark_complete(ExecutionStatus.FAILURE, error="agent crashed")
        assert r.status == ExecutionStatus.FAILURE
        assert r.error == "agent crashed"
        assert r.completed_at is not None

    def test_mark_complete_calculates_duration(self) -> None:
        r = self._make_result()
        r.mark_complete(ExecutionStatus.SUCCESS)
        assert r.duration_ms is not None
        assert r.duration_ms >= 0

    def test_can_hold_artifacts(self) -> None:
        artifact = Artifact(
            name="code.py",
            artifact_type=ArtifactType.CODE,
            produced_by_stage="implementation",
        )
        r = ExecutionResult(
            task_id="t1",
            agent_name="impl_agent",
            status=ExecutionStatus.SUCCESS,
            artifacts=[artifact],
        )
        assert len(r.artifacts) == 1
        assert r.artifacts[0].name == "code.py"

    def test_unique_ids_per_instance(self) -> None:
        r1 = self._make_result()
        r2 = self._make_result()
        assert r1.id != r2.id


# ─── ValidationResult ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestValidationResult:
    def test_passed_result_not_blocking(self) -> None:
        v = ValidationResult(
            rule_name="coverage_threshold",
            passed=True,
            severity=ValidationSeverity.ERROR,
            message="Coverage 85% >= 80% threshold",
        )
        assert not v.is_blocking

    def test_failed_error_result_is_blocking(self) -> None:
        v = ValidationResult(
            rule_name="coverage_threshold",
            passed=False,
            severity=ValidationSeverity.ERROR,
            message="Coverage 60% < 80% threshold",
        )
        assert v.is_blocking

    def test_failed_critical_result_is_blocking(self) -> None:
        v = ValidationResult(
            rule_name="security_scan",
            passed=False,
            severity=ValidationSeverity.CRITICAL,
            message="SQL injection vulnerability detected",
        )
        assert v.is_blocking

    def test_failed_warning_not_blocking(self) -> None:
        v = ValidationResult(
            rule_name="doc_coverage",
            passed=False,
            severity=ValidationSeverity.WARNING,
            message="Docstring coverage 40% < 60% recommendation",
        )
        assert not v.is_blocking

    def test_failed_info_not_blocking(self) -> None:
        v = ValidationResult(
            rule_name="lint_suggestion",
            passed=False,
            severity=ValidationSeverity.INFO,
            message="Consider using f-strings",
        )
        assert not v.is_blocking

    def test_evidence_stored(self) -> None:
        v = ValidationResult(
            rule_name="line_count",
            passed=True,
            message="OK",
            evidence={"lines": 42, "limit": 500},
        )
        assert v.evidence["lines"] == 42


# ─── Risk ─────────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestRisk:
    def test_defaults(self) -> None:
        r = Risk(
            title="No rate limiting",
            description="Endpoint has no rate limiting",
            severity=RiskSeverity.HIGH,
            stage="design",
        )
        assert not r.accepted
        assert r.mitigation is None
        assert r.category == "general"
        assert isinstance(r.id, str)

    def test_all_severities_accepted(self) -> None:
        for severity in RiskSeverity:
            r = Risk(
                title="t",
                description="d",
                severity=severity,
                stage="s",
            )
            assert r.severity == severity

    def test_mitigation_can_be_set(self) -> None:
        r = Risk(
            title="No rate limiting",
            description="No rate limiting",
            severity=RiskSeverity.HIGH,
            stage="design",
            mitigation="Add Redis-backed rate limiter on the API gateway",
        )
        assert "Redis" in r.mitigation

    def test_acceptance_flag(self) -> None:
        r = Risk(
            title="Third-party dep",
            description="Depends on third-party lib",
            severity=RiskSeverity.LOW,
            stage="implementation",
            accepted=True,
        )
        assert r.accepted


# ─── Decision ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestDecision:
    def _base_decision(self, decision_id: str | None = None, parent_id: str | None = None) -> Decision:
        kwargs = dict(
            decision_type=DecisionType.ARCHITECTURAL,
            title="Use PostgreSQL",
            description="Choose PostgreSQL as primary datastore",
            rationale="Team familiarity and strong async driver support",
            stage="design",
        )
        if decision_id:
            kwargs["id"] = decision_id
        if parent_id:
            kwargs["parent_decision_id"] = parent_id
        return Decision(**kwargs)

    def test_defaults(self) -> None:
        d = self._base_decision()
        assert d.made_by == "orchestrator"
        assert d.alternatives_considered == []
        assert d.downstream_impacts == []
        assert d.parent_decision_id is None
        assert isinstance(d.id, str)

    def test_parent_decision_id_links_decisions(self) -> None:
        parent = self._base_decision(decision_id="parent-1")
        child = self._base_decision(parent_id="parent-1")
        assert child.parent_decision_id == parent.id

    def test_alternatives_considered(self) -> None:
        d = Decision(
            decision_type=DecisionType.TRADE_OFF,
            title="sync vs async",
            description="Use async handlers",
            rationale="Better throughput under load",
            stage="design",
            alternatives_considered=["synchronous Django ORM", "threading model"],
        )
        assert "synchronous Django ORM" in d.alternatives_considered

    def test_downstream_impacts(self) -> None:
        d = Decision(
            decision_type=DecisionType.ARCHITECTURAL,
            title="Use event sourcing",
            description="Use event sourcing pattern",
            rationale="Enables full audit replay",
            stage="design",
            downstream_impacts=["implementation", "testing", "release"],
        )
        assert "testing" in d.downstream_impacts

    def test_all_decision_types_accepted(self) -> None:
        for dt in DecisionType:
            d = Decision(
                decision_type=dt,
                title="t",
                description="d",
                rationale="r",
                stage="s",
            )
            assert d.decision_type == dt


# ─── Approval ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestApproval:
    def _make_approval(self) -> Approval:
        return Approval(
            workflow_id="wf-1",
            stage_name="release",
            summary="Approve production deployment of URL shortener v1.0",
        )

    def test_initial_status_is_pending(self) -> None:
        a = self._make_approval()
        assert a.status == ApprovalStatus.PENDING

    def test_is_not_resolved_when_pending(self) -> None:
        assert not self._make_approval().is_resolved

    def test_is_resolved_when_approved(self) -> None:
        a = self._make_approval()
        a.status = ApprovalStatus.APPROVED
        a.approver = "jane.doe"
        a.decided_at = _now()
        assert a.is_resolved
        assert a.was_approved

    def test_is_resolved_when_rejected(self) -> None:
        a = self._make_approval()
        a.status = ApprovalStatus.REJECTED
        assert a.is_resolved
        assert not a.was_approved

    def test_is_resolved_when_timed_out(self) -> None:
        a = self._make_approval()
        a.status = ApprovalStatus.TIMED_OUT
        assert a.is_resolved
        assert not a.was_approved

    def test_context_bag_stored(self) -> None:
        a = Approval(
            workflow_id="wf-1",
            stage_name="release",
            summary="Approve deployment",
            context={"risk_count": 2, "artifacts": ["url_service.py"]},
        )
        assert a.context["risk_count"] == 2

    def test_unique_ids_per_instance(self) -> None:
        a1 = self._make_approval()
        a2 = self._make_approval()
        assert a1.id != a2.id
