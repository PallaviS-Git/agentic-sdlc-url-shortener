"""
Intra-stage task dependency resolution.

Manages the ordering and concurrent scheduling of Tasks within a Stage.
Tasks have `depends_on: list[str]` referencing other task IDs within the
same stage. This mirrors WorkflowDefinition at the task level:
  - WorkflowDefinition drives inter-stage scheduling
  - TaskScheduler drives intra-stage task scheduling

The orchestrator uses this scheduler when executing a stage that contains
multiple tasks with declared dependencies between them.

Import chain: networkx, pydantic models only — no engine imports.
"""
from __future__ import annotations

import networkx as nx

from orchestrator.core.models import Task, TaskStatus


class TaskScheduler:
    """
    Resolves intra-stage task dependencies using a directed acyclic graph.

    Usage pattern:
        scheduler = TaskScheduler(stage_ctx.tasks)
        errors = scheduler.validate()
        if errors:
            raise ValueError(errors)

        completed_ids: set[str] = set()
        while ready := scheduler.get_ready_tasks(completed_ids):
            # execute tasks in ready (possibly in parallel)
            for task in ready:
                ...
                completed_ids.add(task.id)

    Validation:
        Detects unknown dependency IDs and circular dependencies before
        any task is executed. Mirrors WorkflowDefinition.validate_graph().
    """

    def __init__(self, tasks: list[Task]) -> None:
        self._tasks: dict[str, Task] = {t.id: t for t in tasks}
        self._graph: nx.DiGraph = self._build_graph()

    # ── Internal graph construction ────────────────────────────────────────────

    def _build_graph(self) -> nx.DiGraph:
        g: nx.DiGraph = nx.DiGraph()
        g.add_nodes_from(self._tasks.keys())
        for task in self._tasks.values():
            for dep_id in task.depends_on:
                # Edge: dep_id → task.id  (dep must complete before task)
                g.add_edge(dep_id, task.id)
        return g

    # ── Validation ────────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """
        Return a list of validation errors (empty = valid).

        Checks:
          - No task references an unknown task ID in depends_on.
          - The dependency graph is acyclic (no circular dependencies).
        """
        errors: list[str] = []
        known = set(self._tasks.keys())

        for task in self._tasks.values():
            for dep_id in task.depends_on:
                if dep_id not in known:
                    errors.append(
                        f"Task '{task.id}' ({task.title!r}) depends on "
                        f"unknown task ID '{dep_id}'"
                    )

        if not nx.is_directed_acyclic_graph(self._graph):
            errors.append(
                "Task dependency graph contains a cycle — "
                "no valid sequential execution order exists"
            )

        return errors

    def is_acyclic(self) -> bool:
        """True if the task dependency graph contains no cycles."""
        return nx.is_directed_acyclic_graph(self._graph)

    # ── Scheduling queries ────────────────────────────────────────────────────

    def get_ready_tasks(self, completed_ids: set[str]) -> list[Task]:
        """
        Return tasks that are ready to execute right now.

        A task is ready if:
          - Its status is PENDING (not yet started or completed)
          - All IDs in its `depends_on` list are in `completed_ids`

        Multiple ready tasks can be executed concurrently.

        Args:
            completed_ids: IDs of tasks that have finished successfully.

        Returns:
            List of ready tasks (order is implementation-defined; callers
            that need determinism should sort the result by task.id).
        """
        return [
            t for t in self._tasks.values()
            if t.status == TaskStatus.PENDING
            and set(t.depends_on) <= completed_ids
        ]

    def topological_order(self) -> list[Task]:
        """
        Return all tasks in a valid sequential execution order.

        Raises:
            ValueError: If the task graph contains a cycle.
        """
        if not self.is_acyclic():
            raise ValueError(
                "Task graph contains a cycle; cannot compute topological order"
            )
        return [
            self._tasks[tid]
            for tid in nx.topological_sort(self._graph)
            if tid in self._tasks
        ]

    def get_predecessors(self, task_id: str) -> list[str]:
        """Return task IDs that must complete before the given task."""
        return [
            tid for tid in self._graph.predecessors(task_id)
            if tid in self._tasks
        ]

    def get_successors(self, task_id: str) -> list[str]:
        """Return task IDs that depend on the given task."""
        return [
            tid for tid in self._graph.successors(task_id)
            if tid in self._tasks
        ]

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def all_task_ids(self) -> set[str]:
        """Set of all task IDs managed by this scheduler."""
        return set(self._tasks.keys())

    @property
    def root_tasks(self) -> list[Task]:
        """Tasks with no dependencies (can run immediately)."""
        return [
            t for t in self._tasks.values()
            if not t.depends_on
        ]

    def __len__(self) -> int:
        return len(self._tasks)

    def __repr__(self) -> str:
        return f"TaskScheduler(tasks={len(self._tasks)})"
