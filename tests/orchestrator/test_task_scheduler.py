"""
Unit tests for TaskScheduler — intra-stage task dependency resolution.

Covers: validation, cycle detection, unknown dependency detection,
ready-task computation, and topological ordering.
No I/O, no network, no DB.
"""
from __future__ import annotations

import pytest

from orchestrator.core.models import Task, TaskStatus
from orchestrator.engine.task_scheduler import TaskScheduler


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _task(
    task_id: str,
    title: str = "",
    depends_on: list[str] | None = None,
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
    return Task(
        id=task_id,
        title=title or task_id,
        description=f"Task {task_id}",
        depends_on=depends_on or [],
        status=status,
    )


# ─── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestTaskSchedulerValidation:
    def test_no_tasks_is_valid(self) -> None:
        scheduler = TaskScheduler([])
        assert scheduler.validate() == []

    def test_single_task_no_deps_is_valid(self) -> None:
        scheduler = TaskScheduler([_task("t1")])
        assert scheduler.validate() == []

    def test_linear_chain_is_valid(self) -> None:
        tasks = [
            _task("t1"),
            _task("t2", depends_on=["t1"]),
            _task("t3", depends_on=["t2"]),
        ]
        scheduler = TaskScheduler(tasks)
        assert scheduler.validate() == []

    def test_unknown_dependency_detected(self) -> None:
        tasks = [_task("t1", depends_on=["GHOST"])]
        scheduler = TaskScheduler(tasks)
        errors = scheduler.validate()
        assert len(errors) == 1
        assert "GHOST" in errors[0]

    def test_multiple_unknown_dependencies_all_reported(self) -> None:
        tasks = [
            _task("t1", depends_on=["X", "Y"]),
        ]
        scheduler = TaskScheduler(tasks)
        errors = scheduler.validate()
        unknown = [e for e in errors if "X" in e or "Y" in e]
        assert len(unknown) == 2

    def test_simple_cycle_detected(self) -> None:
        tasks = [
            _task("t1", depends_on=["t2"]),
            _task("t2", depends_on=["t1"]),
        ]
        scheduler = TaskScheduler(tasks)
        errors = scheduler.validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_three_node_cycle_detected(self) -> None:
        tasks = [
            _task("t1", depends_on=["t3"]),
            _task("t2", depends_on=["t1"]),
            _task("t3", depends_on=["t2"]),
        ]
        scheduler = TaskScheduler(tasks)
        errors = scheduler.validate()
        assert any("cycle" in e.lower() for e in errors)

    def test_is_acyclic_true_for_valid_graph(self) -> None:
        scheduler = TaskScheduler([_task("t1"), _task("t2", depends_on=["t1"])])
        assert scheduler.is_acyclic()

    def test_is_acyclic_false_for_cyclic_graph(self) -> None:
        tasks = [
            _task("t1", depends_on=["t2"]),
            _task("t2", depends_on=["t1"]),
        ]
        scheduler = TaskScheduler(tasks)
        assert not scheduler.is_acyclic()


# ─── Ready task computation ───────────────────────────────────────────────────


@pytest.mark.unit
class TestGetReadyTasks:
    def test_task_with_no_deps_is_immediately_ready(self) -> None:
        scheduler = TaskScheduler([_task("t1"), _task("t2")])
        ready_ids = {t.id for t in scheduler.get_ready_tasks(set())}
        assert "t1" in ready_ids
        assert "t2" in ready_ids

    def test_dependent_task_not_ready_until_dep_complete(self) -> None:
        tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
        scheduler = TaskScheduler(tasks)
        ready = scheduler.get_ready_tasks(completed_ids=set())
        ready_ids = {t.id for t in ready}
        assert "t1" in ready_ids
        assert "t2" not in ready_ids

    def test_dependent_task_ready_after_dep_complete(self) -> None:
        tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
        scheduler = TaskScheduler(tasks)
        ready = scheduler.get_ready_tasks(completed_ids={"t1"})
        assert any(t.id == "t2" for t in ready)

    def test_task_requiring_multiple_deps_not_ready_until_all_complete(self) -> None:
        tasks = [
            _task("t1"),
            _task("t2"),
            _task("t3", depends_on=["t1", "t2"]),
        ]
        scheduler = TaskScheduler(tasks)
        # Only t1 complete
        ready = scheduler.get_ready_tasks(completed_ids={"t1"})
        assert not any(t.id == "t3" for t in ready)

    def test_task_requiring_multiple_deps_ready_when_all_complete(self) -> None:
        tasks = [
            _task("t1"),
            _task("t2"),
            _task("t3", depends_on=["t1", "t2"]),
        ]
        scheduler = TaskScheduler(tasks)
        ready = scheduler.get_ready_tasks(completed_ids={"t1", "t2"})
        assert any(t.id == "t3" for t in ready)

    def test_completed_task_not_in_ready(self) -> None:
        tasks = [_task("t1", status=TaskStatus.COMPLETED)]
        scheduler = TaskScheduler(tasks)
        ready = scheduler.get_ready_tasks(completed_ids={"t1"})
        assert ready == []

    def test_in_progress_task_not_in_ready(self) -> None:
        tasks = [_task("t1", status=TaskStatus.IN_PROGRESS)]
        scheduler = TaskScheduler(tasks)
        ready = scheduler.get_ready_tasks(completed_ids=set())
        assert ready == []

    def test_empty_task_list_returns_empty_ready(self) -> None:
        scheduler = TaskScheduler([])
        assert scheduler.get_ready_tasks(set()) == []

    def test_all_completed_returns_empty_ready(self) -> None:
        tasks = [_task("t1", status=TaskStatus.COMPLETED)]
        scheduler = TaskScheduler(tasks)
        assert scheduler.get_ready_tasks({"t1"}) == []


# ─── Topological ordering ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestTopologicalOrder:
    def test_linear_chain_ordered_correctly(self) -> None:
        tasks = [
            _task("t1"),
            _task("t2", depends_on=["t1"]),
            _task("t3", depends_on=["t2"]),
        ]
        scheduler = TaskScheduler(tasks)
        ordered = scheduler.topological_order()
        ids = [t.id for t in ordered]
        assert ids.index("t1") < ids.index("t2")
        assert ids.index("t2") < ids.index("t3")

    def test_single_task_returns_single_element(self) -> None:
        scheduler = TaskScheduler([_task("t1")])
        assert [t.id for t in scheduler.topological_order()] == ["t1"]

    def test_empty_returns_empty(self) -> None:
        scheduler = TaskScheduler([])
        assert scheduler.topological_order() == []

    def test_cycle_raises_value_error(self) -> None:
        tasks = [_task("t1", depends_on=["t2"]), _task("t2", depends_on=["t1"])]
        scheduler = TaskScheduler(tasks)
        with pytest.raises(ValueError, match="cycle"):
            scheduler.topological_order()


# ─── Graph queries ────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestTaskSchedulerGraphQueries:
    def test_get_predecessors(self) -> None:
        tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
        scheduler = TaskScheduler(tasks)
        assert scheduler.get_predecessors("t2") == ["t1"]

    def test_get_predecessors_empty_for_root(self) -> None:
        scheduler = TaskScheduler([_task("t1")])
        assert scheduler.get_predecessors("t1") == []

    def test_get_successors(self) -> None:
        tasks = [_task("t1"), _task("t2", depends_on=["t1"])]
        scheduler = TaskScheduler(tasks)
        assert scheduler.get_successors("t1") == ["t2"]

    def test_root_tasks_no_deps(self) -> None:
        tasks = [
            _task("t1"),  # root
            _task("t2", depends_on=["t1"]),  # not root
        ]
        scheduler = TaskScheduler(tasks)
        root_ids = {t.id for t in scheduler.root_tasks}
        assert "t1" in root_ids
        assert "t2" not in root_ids

    def test_all_task_ids(self) -> None:
        tasks = [_task("t1"), _task("t2"), _task("t3")]
        scheduler = TaskScheduler(tasks)
        assert scheduler.all_task_ids == {"t1", "t2", "t3"}

    def test_len(self) -> None:
        scheduler = TaskScheduler([_task("t1"), _task("t2")])
        assert len(scheduler) == 2

    def test_repr(self) -> None:
        scheduler = TaskScheduler([_task("t1"), _task("t2")])
        assert "TaskScheduler" in repr(scheduler)
        assert "2" in repr(scheduler)
