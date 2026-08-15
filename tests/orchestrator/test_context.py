"""
Unit tests for ExecutionContext — cross-stage context propagation.

Covers: stage output recording, input snapshot building, artifact/decision/risk
queries, and decision lineage traversal.
No I/O, no network, no DB.
"""
from __future__ import annotations

import pytest

from orchestrator.core.context import ExecutionContext
from orchestrator.core.results import (
    Artifact,
    ArtifactType,
    Decision,
    DecisionType,
    Risk,
    RiskSeverity,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _artifact(name: str, stage: str, artifact_type: ArtifactType = ArtifactType.CODE) -> Artifact:
    return Artifact(name=name, artifact_type=artifact_type, produced_by_stage=stage)


def _decision(
    title: str,
    stage: str,
    decision_type: DecisionType = DecisionType.ARCHITECTURAL,
    parent_id: str | None = None,
    decision_id: str | None = None,
) -> Decision:
    kwargs: dict = dict(
        decision_type=decision_type,
        title=title,
        description=f"desc of {title}",
        rationale=f"rationale for {title}",
        stage=stage,
    )
    if parent_id:
        kwargs["parent_decision_id"] = parent_id
    if decision_id:
        kwargs["id"] = decision_id
    return Decision(**kwargs)


def _risk(title: str, stage: str, severity: RiskSeverity = RiskSeverity.MEDIUM) -> Risk:
    return Risk(title=title, description=f"desc of {title}", severity=severity, stage=stage)


def _ctx(workflow_id: str = "wf-test") -> ExecutionContext:
    return ExecutionContext(workflow_id=workflow_id)


# ─── Construction ─────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestExecutionContextConstruction:
    def test_starts_empty(self) -> None:
        ctx = _ctx()
        assert ctx.artifacts == []
        assert ctx.decisions == []
        assert ctx.risks == []
        assert ctx.stage_outputs == {}
        assert ctx.completed_stages == []

    def test_workflow_id_stored(self) -> None:
        ctx = _ctx("wf-42")
        assert ctx.workflow_id == "wf-42"

    def test_totals_are_zero_when_empty(self) -> None:
        ctx = _ctx()
        assert ctx.total_artifacts == 0
        assert ctx.total_decisions == 0
        assert ctx.total_risks == 0


# ─── Stage output recording ───────────────────────────────────────────────────


@pytest.mark.unit
class TestStageOutputRecording:
    def test_record_stage_output_stores_data(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {"requirement_id": "req-1"})
        assert ctx.stage_outputs["requirements"] == {"requirement_id": "req-1"}

    def test_completed_stages_reflects_recorded_stages(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {})
        ctx.record_stage_output("design", {})
        assert set(ctx.completed_stages) == {"requirements", "design"}

    def test_record_appends_artifacts(self) -> None:
        ctx = _ctx()
        a1 = _artifact("schema.sql", "design", ArtifactType.SCHEMA)
        a2 = _artifact("migration.sql", "design", ArtifactType.MIGRATION)
        ctx.record_stage_output("design", {}, artifacts=[a1, a2])
        assert ctx.total_artifacts == 2

    def test_record_appends_decisions(self) -> None:
        ctx = _ctx()
        d = _decision("Use PostgreSQL", "design")
        ctx.record_stage_output("design", {}, decisions=[d])
        assert ctx.total_decisions == 1

    def test_record_appends_risks(self) -> None:
        ctx = _ctx()
        r = _risk("No rate limit", "design")
        ctx.record_stage_output("design", {}, risks=[r])
        assert ctx.total_risks == 1

    def test_record_accumulates_across_stages(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output(
            "requirements", {"r": 1}, artifacts=[_artifact("req.md", "requirements")]
        )
        ctx.record_stage_output(
            "design", {"d": 2}, artifacts=[_artifact("arch.md", "design")]
        )
        assert ctx.total_artifacts == 2
        assert set(ctx.completed_stages) == {"requirements", "design"}

    def test_record_without_artifacts_keeps_existing(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {}, artifacts=[_artifact("a.py", "requirements")])
        ctx.record_stage_output("design", {})
        assert ctx.total_artifacts == 1

    def test_overwriting_stage_output_replaces_data(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {"x": 1})
        ctx.record_stage_output("requirements", {"x": 99})
        assert ctx.stage_outputs["requirements"]["x"] == 99


# ─── Snapshot for stage ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestSnapshotForStage:
    def test_empty_predecessors_returns_empty_dict(self) -> None:
        ctx = _ctx()
        snapshot = ctx.snapshot_for_stage([])
        assert snapshot == {}

    def test_single_predecessor_output_returned(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {"req_id": "r1", "title": "Shorten URL"})
        snapshot = ctx.snapshot_for_stage(["requirements"])
        assert snapshot == {"req_id": "r1", "title": "Shorten URL"}

    def test_multiple_predecessors_merged(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {"req_id": "r1"})
        ctx.record_stage_output("design", {"schema": "v1"})
        snapshot = ctx.snapshot_for_stage(["requirements", "design"])
        assert snapshot["req_id"] == "r1"
        assert snapshot["schema"] == "v1"

    def test_later_predecessor_wins_on_key_conflict(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("stage_a", {"key": "from_a"})
        ctx.record_stage_output("stage_b", {"key": "from_b"})
        snapshot = ctx.snapshot_for_stage(["stage_a", "stage_b"])
        assert snapshot["key"] == "from_b"

    def test_missing_predecessor_output_ignored(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {"x": 1})
        snapshot = ctx.snapshot_for_stage(["requirements", "design"])  # design not recorded
        assert snapshot == {"x": 1}

    def test_get_output_returns_specific_key(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {"req_id": "r1", "priority": "high"})
        assert ctx.get_output("requirements", "priority") == "high"

    def test_get_output_returns_default_for_missing_key(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output("requirements", {})
        assert ctx.get_output("requirements", "missing", default="fallback") == "fallback"

    def test_get_output_returns_default_for_missing_stage(self) -> None:
        ctx = _ctx()
        assert ctx.get_output("nonexistent", "key", default=0) == 0


# ─── Artifact queries ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestArtifactQueries:
    def _populated_ctx(self) -> ExecutionContext:
        ctx = _ctx()
        ctx.record_stage_output(
            "design",
            {},
            artifacts=[
                _artifact("schema.sql", "design", ArtifactType.SCHEMA),
                _artifact("arch.md", "design", ArtifactType.DOCUMENTATION),
            ],
        )
        ctx.record_stage_output(
            "implementation",
            {},
            artifacts=[
                _artifact("url_service.py", "implementation", ArtifactType.CODE),
                _artifact("url_repo.py", "implementation", ArtifactType.CODE),
            ],
        )
        ctx.record_stage_output(
            "testing",
            {},
            artifacts=[
                _artifact("test_url_service.py", "testing", ArtifactType.TEST),
            ],
        )
        return ctx

    def test_get_artifacts_by_type_code(self) -> None:
        ctx = self._populated_ctx()
        code = ctx.get_artifacts_by_type(ArtifactType.CODE)
        assert len(code) == 2
        assert all(a.artifact_type == ArtifactType.CODE for a in code)

    def test_get_artifacts_by_type_returns_empty_for_unknown(self) -> None:
        ctx = self._populated_ctx()
        reports = ctx.get_artifacts_by_type(ArtifactType.REPORT)
        assert reports == []

    def test_get_artifacts_by_stage(self) -> None:
        ctx = self._populated_ctx()
        design_artifacts = ctx.get_artifacts_by_stage("design")
        assert len(design_artifacts) == 2
        names = {a.name for a in design_artifacts}
        assert names == {"schema.sql", "arch.md"}

    def test_get_artifact_by_id_returns_correct(self) -> None:
        ctx = _ctx()
        a = _artifact("x.py", "implementation")
        ctx.record_stage_output("implementation", {}, artifacts=[a])
        found = ctx.get_artifact_by_id(a.id)
        assert found is not None
        assert found.name == "x.py"

    def test_get_artifact_by_id_returns_none_for_missing(self) -> None:
        ctx = _ctx()
        assert ctx.get_artifact_by_id("nonexistent-id") is None


# ─── Decision queries and lineage ─────────────────────────────────────────────


@pytest.mark.unit
class TestDecisionQueries:
    def test_get_decisions_by_type(self) -> None:
        ctx = _ctx()
        d1 = _decision("Use PostgreSQL", "design", DecisionType.ARCHITECTURAL)
        d2 = _decision("No caching yet", "design", DecisionType.SCOPE)
        ctx.record_stage_output("design", {}, decisions=[d1, d2])
        arch = ctx.get_decisions_by_type(DecisionType.ARCHITECTURAL)
        assert len(arch) == 1
        assert arch[0].title == "Use PostgreSQL"

    def test_get_decisions_by_stage(self) -> None:
        ctx = _ctx()
        d1 = _decision("Use async", "design")
        d2 = _decision("Use ULID", "implementation")
        ctx.record_stage_output("design", {}, decisions=[d1])
        ctx.record_stage_output("implementation", {}, decisions=[d2])
        design_decisions = ctx.get_decisions_by_stage("design")
        assert len(design_decisions) == 1
        assert design_decisions[0].title == "Use async"

    def test_get_decision_by_id_returns_correct(self) -> None:
        ctx = _ctx()
        d = _decision("Use Redis", "design", decision_id="d-42")
        ctx.record_stage_output("design", {}, decisions=[d])
        found = ctx.get_decision_by_id("d-42")
        assert found is not None
        assert found.title == "Use Redis"

    def test_get_decision_by_id_returns_none_for_missing(self) -> None:
        ctx = _ctx()
        assert ctx.get_decision_by_id("ghost") is None


@pytest.mark.unit
class TestDecisionLineage:
    """Decision lineage: root → intermediate → leaf."""

    def _build_lineage(self) -> tuple[Decision, Decision, Decision, ExecutionContext]:
        """
        Three-level decision chain:
          root (d1, no parent) → mid (d2, parent=d1) → leaf (d3, parent=d2)
        """
        d1 = _decision("Use PostgreSQL", "design", decision_id="d-root")
        d2 = _decision("Use asyncpg driver", "design", parent_id="d-root", decision_id="d-mid")
        d3 = _decision("Use asyncpg connection pool", "implementation", parent_id="d-mid", decision_id="d-leaf")
        ctx = _ctx()
        ctx.record_stage_output("design", {}, decisions=[d1, d2])
        ctx.record_stage_output("implementation", {}, decisions=[d3])
        return d1, d2, d3, ctx

    def test_lineage_of_root_returns_just_root(self) -> None:
        d1, _, _, ctx = self._build_lineage()
        lineage = ctx.get_decision_lineage("d-root")
        assert len(lineage) == 1
        assert lineage[0].id == "d-root"

    def test_lineage_of_leaf_returns_root_first(self) -> None:
        d1, d2, d3, ctx = self._build_lineage()
        lineage = ctx.get_decision_lineage("d-leaf")
        assert len(lineage) == 3
        assert lineage[0].id == "d-root"   # root first
        assert lineage[1].id == "d-mid"
        assert lineage[2].id == "d-leaf"

    def test_lineage_of_mid_returns_two(self) -> None:
        _, d2, _, ctx = self._build_lineage()
        lineage = ctx.get_decision_lineage("d-mid")
        assert len(lineage) == 2
        assert lineage[0].id == "d-root"
        assert lineage[1].id == "d-mid"

    def test_lineage_of_unknown_returns_empty(self) -> None:
        _, _, _, ctx = self._build_lineage()
        lineage = ctx.get_decision_lineage("does-not-exist")
        assert lineage == []

    def test_lineage_not_affected_by_unrelated_decisions(self) -> None:
        d1, d2, d3, ctx = self._build_lineage()
        # Add an unrelated decision
        unrelated = _decision("Use Sentry", "implementation", decision_id="d-unrelated")
        ctx.record_stage_output("implementation", {}, decisions=[unrelated])
        lineage = ctx.get_decision_lineage("d-leaf")
        lineage_ids = [d.id for d in lineage]
        assert "d-unrelated" not in lineage_ids


# ─── Risk queries ─────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestRiskQueries:
    def _populated_ctx(self) -> ExecutionContext:
        ctx = _ctx()
        ctx.record_stage_output(
            "design",
            {},
            risks=[
                _risk("No rate limiting", "design", RiskSeverity.HIGH),
                _risk("CORS misconfiguration", "design", RiskSeverity.CRITICAL),
                _risk("Missing docs", "design", RiskSeverity.LOW),
            ],
        )
        ctx.record_stage_output(
            "implementation",
            {},
            risks=[
                _risk("N+1 query", "implementation", RiskSeverity.MEDIUM),
            ],
        )
        return ctx

    def test_get_risks_by_severity_high(self) -> None:
        ctx = self._populated_ctx()
        high = ctx.get_risks_by_severity(RiskSeverity.HIGH)
        assert len(high) == 1
        assert high[0].title == "No rate limiting"

    def test_get_critical_risks_includes_critical_and_high(self) -> None:
        ctx = self._populated_ctx()
        critical = ctx.get_critical_risks()
        severities = {r.severity for r in critical}
        assert RiskSeverity.CRITICAL in severities
        assert RiskSeverity.HIGH in severities
        assert RiskSeverity.LOW not in severities

    def test_get_unmitigated_risks(self) -> None:
        ctx = _ctx()
        r1 = Risk(
            title="r1",
            description="d",
            severity=RiskSeverity.HIGH,
            stage="design",
            mitigation=None,
            accepted=False,
        )
        r2 = Risk(
            title="r2",
            description="d",
            severity=RiskSeverity.MEDIUM,
            stage="design",
            mitigation="Add caching",
        )
        r3 = Risk(
            title="r3",
            description="d",
            severity=RiskSeverity.LOW,
            stage="design",
            accepted=True,
        )
        ctx.record_stage_output("design", {}, risks=[r1, r2, r3])
        unmitigated = ctx.get_unmitigated_risks()
        titles = {r.title for r in unmitigated}
        assert "r1" in titles
        assert "r2" not in titles   # has mitigation
        assert "r3" not in titles   # accepted


# ─── Repr and summary ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestExecutionContextRepr:
    def test_repr_contains_workflow_id(self) -> None:
        ctx = ExecutionContext(workflow_id="wf-xyz")
        assert "wf-xyz" in repr(ctx)

    def test_repr_reflects_counts(self) -> None:
        ctx = _ctx()
        ctx.record_stage_output(
            "design",
            {},
            artifacts=[_artifact("a.py", "design")],
            decisions=[_decision("use pg", "design")],
            risks=[_risk("no cache", "design")],
        )
        r = repr(ctx)
        assert "artifacts=1" in r
        assert "decisions=1" in r
        assert "risks=1" in r
