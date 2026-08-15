"""
Tests for WorkflowLineage and build_lineage — context and decision lineage.

Required coverage:
  1.  Lineage built from a 3-stage workflow contains all expected records
  2.  Q1  — initiating_requirement returns the correct Requirement
  3.  Q2  — get_all_tasks() returns tasks from every stage
  4.  Q3  — get_task_rationale() returns the recorded rationale string
  5.  Q4  — get_artifact_for_task() resolves created_by_artifact_id
  6.  Q5  — get_decision_for_transition() resolves downstream_impacts fallback
  7.  Q6  — get_agent_for_task() returns assigned_agent name
  8.  Q7  — get_validations_for_stage() filters by stage attribution
  9.  Q8  — get_changes_for_stage() returns audit entries per stage
  10. Q9  — get_approvals() and get_approvals_for_stage() work correctly
  11. Decision lineage: trace_decision_lineage returns root-first 3-level chain
  12. build_lineage annotates task.stage when absent (None/empty)
  13. build_lineage annotates validation.stage when absent (None/empty)
  14. StageTransitions recorded for every executed stage
  15. Context propagation: each stage receives snapshot of predecessors' outputs
  16. summary() returns a dict answering all nine questions
  17. Unknown IDs return None from all lookup methods
  18. get_decision_for_transition() prefers explicit driving_decision_id
"""
from __future__ import annotations

import pytest

from orchestrator.core.base_stage import BaseStage
from orchestrator.core.graph import StageDependency, WorkflowDefinition
from orchestrator.core.lineage import WorkflowLineage, build_lineage
from orchestrator.core.models import (
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    StageTransition,
    Task,
    WorkflowState,
)
from orchestrator.core.results import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    Decision,
    DecisionType,
    ValidationResult,
    ValidationSeverity,
)
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── Fixed IDs (keep stable across all assertions) ────────────────────────────

REQ_DECISION_ID = "d-req-001"
DESIGN_DECISION_ID = "d-design-001"
IMPL_DECISION_ID = "d-impl-001"

REQ_ARTIFACT_ID = "art-req-001"
DESIGN_ARTIFACT_ID = "art-design-001"
IMPL_ARTIFACT_ID = "art-impl-001"

REQ_TASK_ID = "t-req-001"
DESIGN_TASK_ID = "t-design-001"
IMPL_TASK_ID = "t-impl-001"


# ─── Stage stubs ──────────────────────────────────────────────────────────────


class _PassthroughStage(BaseStage):
    """Abstract base for test stages with trivially-passing gates and no rollback."""

    stage_name: str = ""

    async def entry_gate(self, context: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_entry", passed=True)

    async def exit_gate(self, context: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_exit", passed=True)

    async def rollback(self, context: StageContext) -> StageContext:
        return context


class _RequirementsStage(_PassthroughStage):
    """
    Stage 1 — requirements.

    Creates the root decision, a task, an artifact, and a validation result.
    Propagates the requirement-document artifact ID in output_data so that
    downstream stages can reference it.
    """

    stage_name = "requirements"

    async def execute(self, ctx: StageContext) -> StageContext:
        # Root decision — sets downstream_impacts so the design stage
        # can be found via WorkflowLineage.get_decision_for_transition("design")
        ctx.decisions.append(
            Decision(
                id=REQ_DECISION_ID,
                decision_type=DecisionType.SCOPE,
                title="Build URL shortener with async architecture",
                description="Scope decision derived from raw requirement",
                rationale="Requirement explicitly asks for a URL shortener service",
                stage="requirements",
                downstream_impacts=["design"],
            )
        )

        ctx.tasks.append(
            Task(
                id=REQ_TASK_ID,
                title="Analyse requirements",
                description="Parse and normalise the raw requirement text",
                stage="requirements",
                rationale="Needed to produce a structured, unambiguous problem statement",
                assigned_agent="req_agent",
                created_by_decision_id=REQ_DECISION_ID,
            )
        )

        ctx.artifacts.append(
            Artifact(
                id=REQ_ARTIFACT_ID,
                name="requirements.md",
                artifact_type=ArtifactType.DOCUMENTATION,
                produced_by_stage="requirements",
                produced_by_agent="req_agent",
            )
        )

        ctx.validations.append(
            ValidationResult(
                rule_name="requirements_clarity",
                passed=True,
                severity=ValidationSeverity.ERROR,
                message="All requirement items are unambiguous",
                stage="requirements",
            )
        )

        ctx.output_data["req_decision_id"] = REQ_DECISION_ID
        ctx.output_data["requirements_doc_id"] = REQ_ARTIFACT_ID
        return ctx


class _DesignStage(_PassthroughStage):
    """
    Stage 2 — design.

    Creates a child decision (parent → REQ_DECISION_ID) and a task triggered
    by that decision.  The design artifact is placed in output_data so that
    the implementation stage can reference it via created_by_artifact_id.
    """

    stage_name = "design"

    async def execute(self, ctx: StageContext) -> StageContext:
        ctx.decisions.append(
            Decision(
                id=DESIGN_DECISION_ID,
                decision_type=DecisionType.ARCHITECTURAL,
                title="Use PostgreSQL as primary data store",
                description="Technology selection for persistence layer",
                rationale="Requires ACID guarantees; PostgreSQL is proven at scale",
                stage="design",
                parent_decision_id=REQ_DECISION_ID,        # lineage link
                downstream_impacts=["implementation"],     # enables Q5 fallback
            )
        )

        ctx.tasks.append(
            Task(
                id=DESIGN_TASK_ID,
                title="Design database schema",
                description="Produce SQL DDL for the URL shortener tables",
                stage="design",
                rationale="Database schema must exist before any code is written",
                assigned_agent="design_agent",
                created_by_decision_id=DESIGN_DECISION_ID,
            )
        )

        ctx.artifacts.append(
            Artifact(
                id=DESIGN_ARTIFACT_ID,
                name="schema.sql",
                artifact_type=ArtifactType.SCHEMA,
                produced_by_stage="design",
                produced_by_agent="design_agent",
            )
        )

        ctx.validations.append(
            ValidationResult(
                rule_name="schema_completeness",
                passed=True,
                severity=ValidationSeverity.ERROR,
                message="All required tables and columns are present",
                stage="design",
            )
        )

        ctx.output_data["schema_artifact_id"] = DESIGN_ARTIFACT_ID
        return ctx


class _ImplementationStage(_PassthroughStage):
    """
    Stage 3 — implementation.

    Creates a leaf decision (parent → DESIGN_DECISION_ID) and a task that is
    triggered by the schema artifact produced in the design stage.
    """

    stage_name = "implementation"

    async def execute(self, ctx: StageContext) -> StageContext:
        ctx.decisions.append(
            Decision(
                id=IMPL_DECISION_ID,
                decision_type=DecisionType.IMPLEMENTATION,
                title="Use SQLAlchemy 2 async ORM",
                description="ORM selection for database access layer",
                rationale="SQLAlchemy 2 provides native asyncio support for PostgreSQL",
                stage="implementation",
                parent_decision_id=DESIGN_DECISION_ID,    # lineage link
            )
        )

        ctx.tasks.append(
            Task(
                id=IMPL_TASK_ID,
                title="Implement URL service",
                description="Write url_service.py with create/resolve operations",
                stage="implementation",
                rationale="Core feature required by the requirement",
                assigned_agent="impl_agent",
                created_by_artifact_id=DESIGN_ARTIFACT_ID,  # triggered by schema
            )
        )

        ctx.artifacts.append(
            Artifact(
                id=IMPL_ARTIFACT_ID,
                name="url_service.py",
                artifact_type=ArtifactType.CODE,
                produced_by_stage="implementation",
                produced_by_agent="impl_agent",
            )
        )

        ctx.validations.append(
            ValidationResult(
                rule_name="code_quality",
                passed=True,
                severity=ValidationSeverity.ERROR,
                message="Code passes linting and type checks",
                stage="implementation",
            )
        )

        return ctx


# ─── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def requirement() -> Requirement:
    return Requirement(
        id="req-001",
        title="URL shortener service",
        raw_text="Build a production-grade URL shortener with analytics.",
        requirement_type=RequirementType.GREENFIELD,
    )


@pytest.fixture()
def linear_definition() -> WorkflowDefinition:
    """requirements → design → implementation."""
    return WorkflowDefinition(
        name="sdlc-lineage-test",
        description="Three-stage linear workflow for lineage testing",
        stages=["requirements", "design", "implementation"],
        dependencies=[
            StageDependency(from_stage="requirements", to_stage="design"),
            StageDependency(from_stage="design", to_stage="implementation"),
        ],
    )


@pytest.fixture()
async def completed_state(requirement, linear_definition) -> WorkflowState:
    """Run the 3-stage workflow end-to-end and return the final WorkflowState."""
    engine = WorkflowEngine(
        definition=linear_definition,
        stages={
            "requirements": _RequirementsStage(),
            "design": _DesignStage(),
            "implementation": _ImplementationStage(),
        },
    )
    return await engine.run(requirement)


@pytest.fixture()
def lineage(completed_state: WorkflowState) -> WorkflowLineage:
    """WorkflowLineage built from the completed 3-stage run."""
    return build_lineage(completed_state)


# ─── Build sanity ─────────────────────────────────────────────────────────────


class TestBuildLineage:
    """Sanity checks: build_lineage produces a complete, non-empty snapshot."""

    def test_workflow_id_matches(self, lineage: WorkflowLineage, completed_state: WorkflowState) -> None:
        assert lineage.workflow_id == completed_state.id

    def test_three_decisions(self, lineage: WorkflowLineage) -> None:
        assert len(lineage.decisions) == 3

    def test_three_tasks(self, lineage: WorkflowLineage) -> None:
        assert len(lineage.tasks) == 3

    def test_three_artifacts(self, lineage: WorkflowLineage) -> None:
        assert len(lineage.artifacts) == 3

    def test_three_validations(self, lineage: WorkflowLineage) -> None:
        assert len(lineage.validations) == 3

    def test_three_stage_transitions(self, lineage: WorkflowLineage) -> None:
        """Engine must record one transition per stage."""
        assert len(lineage.stage_transitions) == 3

    def test_stage_transition_names(self, lineage: WorkflowLineage) -> None:
        names = {t.stage_name for t in lineage.stage_transitions}
        assert names == {"requirements", "design", "implementation"}


# ─── Q1: What requirement initiated this workflow? ────────────────────────────


class TestQ1InitiatingRequirement:
    def test_requirement_title(self, lineage: WorkflowLineage) -> None:
        assert lineage.initiating_requirement.title == "URL shortener service"

    def test_requirement_id(self, lineage: WorkflowLineage, requirement: Requirement) -> None:
        assert lineage.initiating_requirement.id == requirement.id

    def test_requirement_type(self, lineage: WorkflowLineage) -> None:
        assert lineage.initiating_requirement.requirement_type == RequirementType.GREENFIELD


# ─── Q2: Which tasks were created? ───────────────────────────────────────────


class TestQ2TasksCreated:
    def test_get_all_tasks_count(self, lineage: WorkflowLineage) -> None:
        assert len(lineage.get_all_tasks()) == 3

    def test_get_all_tasks_ids(self, lineage: WorkflowLineage) -> None:
        ids = {t.id for t in lineage.get_all_tasks()}
        assert ids == {REQ_TASK_ID, DESIGN_TASK_ID, IMPL_TASK_ID}

    def test_get_tasks_for_stage(self, lineage: WorkflowLineage) -> None:
        design_tasks = lineage.get_tasks_for_stage("design")
        assert len(design_tasks) == 1
        assert design_tasks[0].id == DESIGN_TASK_ID

    def test_tasks_annotated_with_stage(self, lineage: WorkflowLineage) -> None:
        for task in lineage.tasks:
            assert task.stage, f"Task {task.id} has empty stage annotation"


# ─── Q3: Why was a task created? ─────────────────────────────────────────────


class TestQ3TaskRationale:
    def test_requirements_task_rationale(self, lineage: WorkflowLineage) -> None:
        rationale = lineage.get_task_rationale(REQ_TASK_ID)
        assert rationale == "Needed to produce a structured, unambiguous problem statement"

    def test_design_task_rationale(self, lineage: WorkflowLineage) -> None:
        rationale = lineage.get_task_rationale(DESIGN_TASK_ID)
        assert rationale == "Database schema must exist before any code is written"

    def test_impl_task_rationale(self, lineage: WorkflowLineage) -> None:
        rationale = lineage.get_task_rationale(IMPL_TASK_ID)
        assert rationale == "Core feature required by the requirement"

    def test_unknown_task_returns_none(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_task_rationale("nonexistent-task") is None

    def test_task_provenance_full_dict(self, lineage: WorkflowLineage) -> None:
        prov = lineage.get_task_provenance(DESIGN_TASK_ID)
        assert prov["task_id"] == DESIGN_TASK_ID
        assert prov["stage"] == "design"
        assert prov["rationale"] is not None
        assert prov["creating_decision"]["id"] == DESIGN_DECISION_ID
        assert prov["assigned_agent"] == "design_agent"
        assert prov["triggering_artifact"] is None  # design task was triggered by decision, not artifact

    def test_task_provenance_unknown_returns_empty(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_task_provenance("bad-id") == {}


# ─── Q4: Which artifact produced / triggered a task? ─────────────────────────


class TestQ4ArtifactForTask:
    def test_impl_task_triggered_by_schema(self, lineage: WorkflowLineage) -> None:
        artifact = lineage.get_artifact_for_task(IMPL_TASK_ID)
        assert artifact is not None
        assert artifact.id == DESIGN_ARTIFACT_ID
        assert artifact.name == "schema.sql"

    def test_req_task_has_no_triggering_artifact(self, lineage: WorkflowLineage) -> None:
        # req task was created_by_decision, not by artifact
        assert lineage.get_artifact_for_task(REQ_TASK_ID) is None

    def test_unknown_task_returns_none(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_artifact_for_task("bad-id") is None

    def test_get_artifacts_for_stage(self, lineage: WorkflowLineage) -> None:
        design_artifacts = lineage.get_artifacts_for_stage("design")
        assert len(design_artifacts) == 1
        assert design_artifacts[0].id == DESIGN_ARTIFACT_ID

    def test_get_artifact_by_id(self, lineage: WorkflowLineage) -> None:
        artifact = lineage.get_artifact_by_id(REQ_ARTIFACT_ID)
        assert artifact is not None
        assert artifact.name == "requirements.md"

    def test_unknown_artifact_returns_none(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_artifact_by_id("bad-id") is None


# ─── Q5: Which decision led to the next stage? ───────────────────────────────


class TestQ5DecisionForTransition:
    def test_requirements_is_root_no_predecessors(self, lineage: WorkflowLineage) -> None:
        transition = lineage.get_transition_to_stage("requirements")
        assert transition is not None
        assert transition.predecessor_stages == []

    def test_design_has_requirements_as_predecessor(self, lineage: WorkflowLineage) -> None:
        transition = lineage.get_transition_to_stage("design")
        assert transition is not None
        assert "requirements" in transition.predecessor_stages

    def test_implementation_has_design_as_predecessor(self, lineage: WorkflowLineage) -> None:
        transition = lineage.get_transition_to_stage("implementation")
        assert transition is not None
        assert "design" in transition.predecessor_stages

    def test_design_driven_by_req_decision_via_downstream_impacts(
        self, lineage: WorkflowLineage
    ) -> None:
        # d-req-001 has downstream_impacts=["design"] — engine didn't set
        # driving_decision_id, so get_decision_for_transition uses the fallback
        decision = lineage.get_decision_for_transition("design")
        assert decision is not None
        assert decision.id == REQ_DECISION_ID

    def test_implementation_driven_by_design_decision_via_downstream_impacts(
        self, lineage: WorkflowLineage
    ) -> None:
        # d-design-001 has downstream_impacts=["implementation"]
        decision = lineage.get_decision_for_transition("implementation")
        assert decision is not None
        assert decision.id == DESIGN_DECISION_ID

    def test_unknown_stage_returns_none(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_transition_to_stage("nonexistent") is None
        assert lineage.get_decision_for_transition("nonexistent") is None


# ─── Q6: Which agent executed a task? ────────────────────────────────────────


class TestQ6AgentForTask:
    def test_req_agent(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_agent_for_task(REQ_TASK_ID) == "req_agent"

    def test_design_agent(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_agent_for_task(DESIGN_TASK_ID) == "design_agent"

    def test_impl_agent(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_agent_for_task(IMPL_TASK_ID) == "impl_agent"

    def test_unknown_task_returns_none(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_agent_for_task("bad-id") is None


# ─── Q7: What validation was performed? ──────────────────────────────────────


class TestQ7Validations:
    def test_each_stage_has_one_validation(self, lineage: WorkflowLineage) -> None:
        for stage in ("requirements", "design", "implementation"):
            assert len(lineage.get_validations_for_stage(stage)) == 1

    def test_requirements_validation_rule_name(self, lineage: WorkflowLineage) -> None:
        vs = lineage.get_validations_for_stage("requirements")
        assert vs[0].rule_name == "requirements_clarity"

    def test_design_validation_rule_name(self, lineage: WorkflowLineage) -> None:
        vs = lineage.get_validations_for_stage("design")
        assert vs[0].rule_name == "schema_completeness"

    def test_impl_validation_rule_name(self, lineage: WorkflowLineage) -> None:
        vs = lineage.get_validations_for_stage("implementation")
        assert vs[0].rule_name == "code_quality"

    def test_all_validations_passed(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_failed_validations() == []

    def test_validations_annotated_with_stage(self, lineage: WorkflowLineage) -> None:
        for v in lineage.validations:
            assert v.stage, f"ValidationResult {v.rule_name} has empty stage annotation"


# ─── Q8: What changed? ───────────────────────────────────────────────────────


class TestQ8ChangeLog:
    def test_full_change_log_is_non_empty(self, lineage: WorkflowLineage) -> None:
        assert len(lineage.get_full_change_log()) > 0

    def test_change_log_is_ordered_by_timestamp(self, lineage: WorkflowLineage) -> None:
        log = lineage.get_full_change_log()
        timestamps = [e.timestamp for e in log]
        assert timestamps == sorted(timestamps)

    def test_per_stage_changes_exist(self, lineage: WorkflowLineage) -> None:
        for stage in ("requirements", "design", "implementation"):
            changes = lineage.get_changes_for_stage(stage)
            assert changes, f"Expected audit entries for stage '{stage}'"

    def test_requirements_audit_includes_stage_started(
        self, lineage: WorkflowLineage
    ) -> None:
        events = {e.event for e in lineage.get_changes_for_stage("requirements")}
        assert "stage_started" in events

    def test_implementation_audit_includes_stage_completed(
        self, lineage: WorkflowLineage
    ) -> None:
        events = {e.event for e in lineage.get_changes_for_stage("implementation")}
        assert "stage_completed" in events


# ─── Q9: What was approved? ──────────────────────────────────────────────────


class TestQ9Approvals:
    def test_no_approvals_in_standard_workflow(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_approvals() == []

    def test_get_approvals_for_stage_empty(self, lineage: WorkflowLineage) -> None:
        assert lineage.get_approvals_for_stage("requirements") == []

    def test_approvals_visible_when_recorded(
        self, completed_state: WorkflowState
    ) -> None:
        approval = Approval(
            workflow_id=completed_state.id,
            stage_name="requirements",
            summary="Approve scope",
            status=ApprovalStatus.APPROVED,
            approver="alice",
        )
        completed_state.add_approval(approval)
        lineage = build_lineage(completed_state)

        approvals = lineage.get_approvals()
        assert len(approvals) == 1
        assert approvals[0].stage_name == "requirements"
        assert approvals[0].status == ApprovalStatus.APPROVED

        stage_approvals = lineage.get_approvals_for_stage("requirements")
        assert len(stage_approvals) == 1


# ─── Decision lineage ─────────────────────────────────────────────────────────


class TestDecisionLineage:
    def test_three_level_chain(self, lineage: WorkflowLineage) -> None:
        """d-req-001 → d-design-001 → d-impl-001 should trace back to root."""
        chain = lineage.trace_decision_lineage(IMPL_DECISION_ID)
        assert len(chain) == 3
        assert chain[0].id == REQ_DECISION_ID    # root
        assert chain[1].id == DESIGN_DECISION_ID  # mid
        assert chain[2].id == IMPL_DECISION_ID    # leaf

    def test_two_level_chain(self, lineage: WorkflowLineage) -> None:
        chain = lineage.trace_decision_lineage(DESIGN_DECISION_ID)
        assert len(chain) == 2
        assert chain[0].id == REQ_DECISION_ID
        assert chain[1].id == DESIGN_DECISION_ID

    def test_root_decision_chain_length_one(self, lineage: WorkflowLineage) -> None:
        chain = lineage.trace_decision_lineage(REQ_DECISION_ID)
        assert len(chain) == 1
        assert chain[0].id == REQ_DECISION_ID

    def test_unknown_decision_returns_empty_chain(
        self, lineage: WorkflowLineage
    ) -> None:
        chain = lineage.trace_decision_lineage("nonexistent-decision")
        assert chain == []

    def test_get_decision_by_id(self, lineage: WorkflowLineage) -> None:
        decision = lineage.get_decision_by_id(REQ_DECISION_ID)
        assert decision is not None
        assert decision.title == "Build URL shortener with async architecture"

    def test_get_decisions_for_stage(self, lineage: WorkflowLineage) -> None:
        decisions = lineage.get_decisions_for_stage("design")
        assert len(decisions) == 1
        assert decisions[0].id == DESIGN_DECISION_ID


# ─── Stage transition details ─────────────────────────────────────────────────


class TestStageTransitions:
    def test_root_stage_has_no_predecessors(self, lineage: WorkflowLineage) -> None:
        transition = lineage.get_transition_to_stage("requirements")
        assert transition.predecessor_stages == []

    def test_root_stage_reason_mentions_no_predecessors(
        self, lineage: WorkflowLineage
    ) -> None:
        transition = lineage.get_transition_to_stage("requirements")
        assert "no predecessors" in transition.transition_reason.lower()

    def test_design_transition_reason_mentions_requirements(
        self, lineage: WorkflowLineage
    ) -> None:
        transition = lineage.get_transition_to_stage("design")
        assert "requirements" in transition.transition_reason

    def test_transitions_have_timestamps(self, lineage: WorkflowLineage) -> None:
        for t in lineage.stage_transitions:
            assert t.started_at is not None

    def test_explicit_driving_decision_id_takes_precedence(self) -> None:
        """
        When a StageTransition has an explicit driving_decision_id, that decision
        is returned directly without falling back to downstream_impacts scanning.
        """
        decision_a = Decision(
            id="da",
            decision_type=DecisionType.SCOPE,
            title="A",
            description="A",
            rationale="A",
            stage="alpha",
            downstream_impacts=["beta"],  # would match via fallback
        )
        decision_b = Decision(
            id="db",
            decision_type=DecisionType.ARCHITECTURAL,
            title="B",
            description="B",
            rationale="B",
            stage="alpha",
            downstream_impacts=[],
        )
        transition = StageTransition(
            stage_name="beta",
            predecessor_stages=["alpha"],
            driving_decision_id="db",  # explicit override
        )

        req = Requirement(
            title="T", raw_text="T", requirement_type=RequirementType.GREENFIELD
        )
        state = WorkflowState(requirement=req)
        state.stage_transitions.append(transition)

        # Fake a stage context that holds both decisions
        from orchestrator.core.models import StageContext, StageStatus
        sc = StageContext(stage_name="alpha", status=StageStatus.COMPLETED)
        sc.decisions.extend([decision_a, decision_b])
        state.stages["alpha"] = sc

        lin = build_lineage(state)
        found = lin.get_decision_for_transition("beta")
        assert found is not None
        assert found.id == "db"  # explicit, not "da" via downstream_impacts


# ─── build_lineage annotation ─────────────────────────────────────────────────


class TestBuildLineageAnnotation:
    def test_tasks_without_stage_get_annotated(self) -> None:
        req = Requirement(
            title="T", raw_text="T", requirement_type=RequirementType.GREENFIELD
        )
        state = WorkflowState(requirement=req)

        from orchestrator.core.models import StageContext, StageStatus
        sc = StageContext(stage_name="alpha", status=StageStatus.COMPLETED)
        sc.tasks.append(
            Task(id="t1", title="Task", description="D")  # stage="" (default)
        )
        state.stages["alpha"] = sc

        lin = build_lineage(state)
        assert lin.tasks[0].stage == "alpha"

    def test_tasks_with_existing_stage_not_overwritten(self) -> None:
        req = Requirement(
            title="T", raw_text="T", requirement_type=RequirementType.GREENFIELD
        )
        state = WorkflowState(requirement=req)

        from orchestrator.core.models import StageContext, StageStatus
        sc = StageContext(stage_name="alpha", status=StageStatus.COMPLETED)
        sc.tasks.append(
            Task(id="t1", title="Task", description="D", stage="custom-stage")
        )
        state.stages["alpha"] = sc

        lin = build_lineage(state)
        assert lin.tasks[0].stage == "custom-stage"

    def test_validations_without_stage_get_annotated(self) -> None:
        req = Requirement(
            title="T", raw_text="T", requirement_type=RequirementType.GREENFIELD
        )
        state = WorkflowState(requirement=req)

        from orchestrator.core.models import StageContext, StageStatus
        sc = StageContext(stage_name="alpha", status=StageStatus.COMPLETED)
        sc.validations.append(
            ValidationResult(rule_name="rule1", passed=True, message="ok")
            # stage="" (default)
        )
        state.stages["alpha"] = sc

        lin = build_lineage(state)
        assert lin.validations[0].stage == "alpha"

    def test_validations_with_existing_stage_not_overwritten(self) -> None:
        req = Requirement(
            title="T", raw_text="T", requirement_type=RequirementType.GREENFIELD
        )
        state = WorkflowState(requirement=req)

        from orchestrator.core.models import StageContext, StageStatus
        sc = StageContext(stage_name="alpha", status=StageStatus.COMPLETED)
        sc.validations.append(
            ValidationResult(
                rule_name="rule1", passed=True, message="ok", stage="custom-stage"
            )
        )
        state.stages["alpha"] = sc

        lin = build_lineage(state)
        assert lin.validations[0].stage == "custom-stage"


# ─── Context propagation ──────────────────────────────────────────────────────


class TestContextPropagation:
    """
    Each stage must receive the outputs of its predecessors.
    This tests that build_lineage correctly reflects the cross-stage
    information flow captured by ExecutionContext.
    """

    async def test_design_stage_receives_requirements_output(
        self, completed_state: WorkflowState
    ) -> None:
        design_ctx = completed_state.get_stage("design")
        assert design_ctx is not None
        # The requirements stage put "requirements_doc_id" in output_data.
        # ExecutionContext.snapshot_for_stage propagates this to design's input_data.
        assert "requirements_doc_id" in design_ctx.input_data
        assert design_ctx.input_data["requirements_doc_id"] == REQ_ARTIFACT_ID

    async def test_implementation_stage_receives_design_output(
        self, completed_state: WorkflowState
    ) -> None:
        impl_ctx = completed_state.get_stage("implementation")
        assert impl_ctx is not None
        assert "schema_artifact_id" in impl_ctx.input_data
        assert impl_ctx.input_data["schema_artifact_id"] == DESIGN_ARTIFACT_ID


# ─── summary() ────────────────────────────────────────────────────────────────


class TestSummary:
    def test_summary_contains_all_nine_answers(self, lineage: WorkflowLineage) -> None:
        s = lineage.summary()

        assert s["initiating_requirement"]["title"] == "URL shortener service"
        assert len(s["tasks_created"]) == 3
        assert len(s["stage_transitions"]) == 3
        assert s["validations_performed"] == 3
        assert s["failed_validations"] == 0
        assert s["approvals"] == []
        assert s["decisions_made"] == 3
        assert s["artifacts_produced"] == 3
        assert s["risks_identified"] == 0
        assert s["audit_events"] > 0

    def test_summary_task_entries_include_stage_and_agent(
        self, lineage: WorkflowLineage
    ) -> None:
        tasks = lineage.summary()["tasks_created"]
        task_map = {t["id"]: t for t in tasks}
        assert task_map[IMPL_TASK_ID]["agent"] == "impl_agent"
        assert task_map[IMPL_TASK_ID]["stage"] == "implementation"

    def test_summary_stage_transitions_include_predecessors(
        self, lineage: WorkflowLineage
    ) -> None:
        transitions = {
            t["stage"]: t for t in lineage.summary()["stage_transitions"]
        }
        assert transitions["requirements"]["predecessors"] == []
        assert "requirements" in transitions["design"]["predecessors"]
        assert "design" in transitions["implementation"]["predecessors"]
