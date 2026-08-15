"""
Unit tests for the WorkflowDefinition DAG model.

Covers: topology queries, parallel groups, synchronization barriers,
ready-stage computation, cycle detection, and graph validation.
No I/O, no network, no DB.
"""
from __future__ import annotations

import pytest

from orchestrator.core.graph import DependencyType, StageDependency, WorkflowDefinition


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _linear() -> WorkflowDefinition:
    """A → B → C → D (purely sequential)."""
    return WorkflowDefinition(
        name="linear",
        description="Linear sequential workflow",
        stages=["A", "B", "C", "D"],
        dependencies=[
            StageDependency(from_stage="A", to_stage="B"),
            StageDependency(from_stage="B", to_stage="C"),
            StageDependency(from_stage="C", to_stage="D"),
        ],
    )


def _diamond() -> WorkflowDefinition:
    """A → B, A → C, B → D, C → D (parallel B and C, converge at D)."""
    return WorkflowDefinition(
        name="diamond",
        description="Diamond with parallel branches",
        stages=["A", "B", "C", "D"],
        dependencies=[
            StageDependency(from_stage="A", to_stage="B"),
            StageDependency(from_stage="A", to_stage="C"),
            StageDependency(from_stage="B", to_stage="D"),
            StageDependency(from_stage="C", to_stage="D"),
        ],
    )


def _sdlc() -> WorkflowDefinition:
    """Full SDLC-like graph: Requirements → Design → Implement + Docs → Test → Release."""
    return WorkflowDefinition(
        name="sdlc",
        description="SDLC lifecycle",
        stages=["requirements", "design", "implement", "docs", "test", "release"],
        dependencies=[
            StageDependency(from_stage="requirements", to_stage="design"),
            StageDependency(from_stage="design", to_stage="implement"),
            StageDependency(from_stage="design", to_stage="docs"),
            StageDependency(from_stage="implement", to_stage="test"),
            StageDependency(from_stage="docs", to_stage="test"),
            StageDependency(from_stage="test", to_stage="release"),
        ],
    )


# ─── Construction and validation ──────────────────────────────────────────────


@pytest.mark.unit
class TestWorkflowDefinitionValidation:
    def test_valid_linear_has_no_errors(self) -> None:
        errors = _linear().validate_graph()
        assert errors == []

    def test_valid_diamond_has_no_errors(self) -> None:
        errors = _diamond().validate_graph()
        assert errors == []

    def test_valid_sdlc_has_no_errors(self) -> None:
        errors = _sdlc().validate_graph()
        assert errors == []

    def test_empty_stages_is_invalid(self) -> None:
        wf = WorkflowDefinition(name="empty", description="", stages=[], dependencies=[])
        errors = wf.validate_graph()
        assert any("at least one" in e for e in errors)

    def test_unknown_from_stage_is_invalid(self) -> None:
        wf = WorkflowDefinition(
            name="bad",
            description="",
            stages=["A", "B"],
            dependencies=[StageDependency(from_stage="UNKNOWN", to_stage="B")],
        )
        errors = wf.validate_graph()
        assert any("UNKNOWN" in e for e in errors)

    def test_unknown_to_stage_is_invalid(self) -> None:
        wf = WorkflowDefinition(
            name="bad",
            description="",
            stages=["A", "B"],
            dependencies=[StageDependency(from_stage="A", to_stage="GHOST")],
        )
        errors = wf.validate_graph()
        assert any("GHOST" in e for e in errors)

    def test_cycle_detected_in_validation(self) -> None:
        wf = WorkflowDefinition(
            name="cyclic",
            description="",
            stages=["A", "B", "C"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="C"),
                StageDependency(from_stage="C", to_stage="A"),  # cycle
            ],
        )
        errors = wf.validate_graph()
        assert any("cycle" in e.lower() for e in errors)

    def test_is_acyclic_true_for_valid_dag(self) -> None:
        assert _diamond().is_acyclic()

    def test_is_acyclic_false_for_cyclic_graph(self) -> None:
        wf = WorkflowDefinition(
            name="cyclic",
            description="",
            stages=["X", "Y"],
            dependencies=[
                StageDependency(from_stage="X", to_stage="Y"),
                StageDependency(from_stage="Y", to_stage="X"),
            ],
        )
        assert not wf.is_acyclic()

    def test_isolated_stage_no_dependencies_is_valid(self) -> None:
        wf = WorkflowDefinition(
            name="isolated",
            description="",
            stages=["A", "B"],  # B has no edges
            dependencies=[],
        )
        assert wf.validate_graph() == []


# ─── Topological order ────────────────────────────────────────────────────────


@pytest.mark.unit
class TestTopologicalOrder:
    def test_linear_order_is_a_b_c_d(self) -> None:
        order = _linear().topological_order()
        assert order == ["A", "B", "C", "D"]

    def test_diamond_a_is_first(self) -> None:
        order = _diamond().topological_order()
        assert order[0] == "A"

    def test_diamond_d_is_last(self) -> None:
        order = _diamond().topological_order()
        assert order[-1] == "D"

    def test_diamond_b_and_c_before_d(self) -> None:
        order = _diamond().topological_order()
        assert order.index("B") < order.index("D")
        assert order.index("C") < order.index("D")

    def test_sdlc_requirements_is_first(self) -> None:
        order = _sdlc().topological_order()
        assert order[0] == "requirements"

    def test_sdlc_release_is_last(self) -> None:
        order = _sdlc().topological_order()
        assert order[-1] == "release"

    def test_topological_order_raises_on_cycle(self) -> None:
        wf = WorkflowDefinition(
            name="c",
            description="",
            stages=["X", "Y"],
            dependencies=[
                StageDependency(from_stage="X", to_stage="Y"),
                StageDependency(from_stage="Y", to_stage="X"),
            ],
        )
        with pytest.raises(ValueError, match="cycle"):
            wf.topological_order()

    def test_single_stage_order(self) -> None:
        wf = WorkflowDefinition(name="s", description="", stages=["only"], dependencies=[])
        assert wf.topological_order() == ["only"]


# ─── Predecessors and successors ──────────────────────────────────────────────


@pytest.mark.unit
class TestPredecessorsAndSuccessors:
    def test_linear_b_predecessor_is_a(self) -> None:
        assert _linear().get_predecessors("B") == ["A"]

    def test_linear_a_has_no_predecessors(self) -> None:
        assert _linear().get_predecessors("A") == []

    def test_linear_d_has_no_successors(self) -> None:
        assert _linear().get_successors("D") == []

    def test_diamond_d_predecessors_are_b_and_c(self) -> None:
        preds = set(_diamond().get_predecessors("D"))
        assert preds == {"B", "C"}

    def test_diamond_a_successors_are_b_and_c(self) -> None:
        succs = set(_diamond().get_successors("A"))
        assert succs == {"B", "C"}

    def test_stages_that_must_precede_d_in_linear(self) -> None:
        ancestors = _linear().stages_that_must_precede("D")
        assert ancestors == {"A", "B", "C"}

    def test_stages_that_must_precede_b_in_linear(self) -> None:
        ancestors = _linear().stages_that_must_precede("B")
        assert ancestors == {"A"}

    def test_stages_reachable_from_a_in_linear(self) -> None:
        descendants = _linear().stages_reachable_from("A")
        assert descendants == {"B", "C", "D"}

    def test_stages_reachable_from_d_in_linear(self) -> None:
        descendants = _linear().stages_reachable_from("D")
        assert descendants == set()

    def test_sdlc_design_successors(self) -> None:
        succs = set(_sdlc().get_successors("design"))
        assert succs == {"implement", "docs"}


# ─── Ready stages ─────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestGetReadyStages:
    def test_only_root_ready_when_nothing_complete(self) -> None:
        ready = _linear().get_ready_stages(completed=set())
        assert ready == ["A"]

    def test_b_ready_when_a_complete(self) -> None:
        ready = _linear().get_ready_stages(completed={"A"})
        assert ready == ["B"]

    def test_nothing_ready_when_all_complete(self) -> None:
        ready = _linear().get_ready_stages(completed={"A", "B", "C", "D"})
        assert ready == []

    def test_diamond_b_and_c_both_ready_when_a_complete(self) -> None:
        ready = _diamond().get_ready_stages(completed={"A"})
        assert set(ready) == {"B", "C"}

    def test_diamond_d_not_ready_when_only_b_complete(self) -> None:
        ready = _diamond().get_ready_stages(completed={"A", "B"})
        # C is still needed; D should not be ready
        assert "D" not in ready

    def test_diamond_d_ready_when_b_and_c_complete(self) -> None:
        ready = _diamond().get_ready_stages(completed={"A", "B", "C"})
        assert "D" in ready

    def test_in_progress_stages_excluded(self) -> None:
        ready = _linear().get_ready_stages(
            completed=set(), in_progress={"A"}
        )
        assert "A" not in ready
        assert ready == []

    def test_sdlc_implement_and_docs_ready_after_design(self) -> None:
        ready = _sdlc().get_ready_stages(completed={"requirements", "design"})
        assert set(ready) == {"implement", "docs"}

    def test_sdlc_test_not_ready_until_both_implement_and_docs_done(self) -> None:
        ready = _sdlc().get_ready_stages(
            completed={"requirements", "design", "implement"}
        )
        assert "test" not in ready  # docs not done

    def test_isolated_stages_are_all_ready(self) -> None:
        wf = WorkflowDefinition(
            name="iso", description="", stages=["X", "Y", "Z"], dependencies=[]
        )
        ready = wf.get_ready_stages(completed=set())
        assert set(ready) == {"X", "Y", "Z"}


# ─── Parallel groups ──────────────────────────────────────────────────────────


@pytest.mark.unit
class TestParallelGroups:
    def test_linear_has_four_single_stage_groups(self) -> None:
        groups = _linear().get_parallel_groups()
        assert len(groups) == 4
        assert all(len(g) == 1 for g in groups)

    def test_linear_groups_in_order(self) -> None:
        groups = _linear().get_parallel_groups()
        flat = [g[0] for g in groups]
        assert flat == ["A", "B", "C", "D"]

    def test_diamond_three_groups(self) -> None:
        groups = _diamond().get_parallel_groups()
        assert len(groups) == 3

    def test_diamond_first_group_is_a(self) -> None:
        groups = _diamond().get_parallel_groups()
        assert groups[0] == ["A"]

    def test_diamond_second_group_is_b_and_c(self) -> None:
        groups = _diamond().get_parallel_groups()
        assert set(groups[1]) == {"B", "C"}

    def test_diamond_last_group_is_d(self) -> None:
        groups = _diamond().get_parallel_groups()
        assert groups[-1] == ["D"]

    def test_sdlc_implement_and_docs_in_same_group(self) -> None:
        groups = _sdlc().get_parallel_groups()
        parallel_stage_names = {s for g in groups for s in g if len(g) > 1}
        assert "implement" in parallel_stage_names
        assert "docs" in parallel_stage_names

    def test_single_stage_one_group(self) -> None:
        wf = WorkflowDefinition(name="s", description="", stages=["only"], dependencies=[])
        assert wf.get_parallel_groups() == [["only"]]

    def test_no_stages_returns_empty(self) -> None:
        wf = WorkflowDefinition(name="s", description="", stages=[], dependencies=[])
        assert wf.get_parallel_groups() == []


# ─── Synchronization points ───────────────────────────────────────────────────


@pytest.mark.unit
class TestSynchronizationPoints:
    def test_diamond_d_is_synchronization_point(self) -> None:
        points = _diamond().get_synchronization_points()
        assert "D" in points

    def test_diamond_a_is_not_synchronization_point(self) -> None:
        points = _diamond().get_synchronization_points()
        assert "A" not in points

    def test_linear_has_no_synchronization_points(self) -> None:
        points = _linear().get_synchronization_points()
        assert points == []

    def test_sdlc_test_is_synchronization_point(self) -> None:
        points = _sdlc().get_synchronization_points()
        assert "test" in points


# ─── Dependency edges ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestDependencyEdges:
    def test_get_dependency_returns_edge(self) -> None:
        dep = _linear().get_dependency("A", "B")
        assert dep is not None
        assert dep.from_stage == "A"
        assert dep.to_stage == "B"

    def test_get_dependency_returns_none_for_missing_edge(self) -> None:
        dep = _linear().get_dependency("A", "D")  # no direct edge
        assert dep is None

    def test_conditional_dependency_type_preserved(self) -> None:
        wf = WorkflowDefinition(
            name="cond",
            description="",
            stages=["A", "B"],
            dependencies=[
                StageDependency(
                    from_stage="A",
                    to_stage="B",
                    dependency_type=DependencyType.CONDITIONAL,
                    condition="A.output_data['success'] == True",
                )
            ],
        )
        dep = wf.get_dependency("A", "B")
        assert dep is not None
        assert dep.dependency_type == DependencyType.CONDITIONAL
        assert "success" in dep.condition
