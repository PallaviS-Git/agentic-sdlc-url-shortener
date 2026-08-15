"""
Workflow topology — the dependency graph that drives SDLC execution.

A WorkflowDefinition is an immutable description of the SDLC lifecycle:
which stages exist, and which must complete before others can start.
It is the *template*; WorkflowState is the *runtime instance*.

Key design choices:
  - networkx DiGraph is the backing store (mature, battle-tested graph library).
  - The graph is built once at model initialisation and cached as a private attribute.
  - All topology queries (ready stages, parallel groups, reachability) are pure
    functions on the graph — no mutation, no side effects.
  - Parallel execution is expressed naturally: stages with no dependency edge
    between them belong to the same parallel group.
  - A `SynchronizationBarrier` is implicit: a stage whose predecessors span
    multiple parallel branches can only start after ALL of them complete.

Import chain: this module imports only pydantic, networkx, and stdlib.
"""
from __future__ import annotations

import enum
from typing import Any

import networkx as nx
from pydantic import BaseModel, Field, PrivateAttr, model_validator


# ─── Dependency ───────────────────────────────────────────────────────────────


class DependencyType(str, enum.Enum):
    """
    How a downstream stage relates to an upstream stage.

    SEQUENTIAL: downstream cannot start until upstream completes successfully.
    CONDITIONAL: downstream only activates if an expression evaluates to True
                 against the upstream stage's output_data.
    """

    SEQUENTIAL = "sequential"
    CONDITIONAL = "conditional"


class StageDependency(BaseModel):
    """
    A directed edge in the workflow DAG.

    Represents: `from_stage` must complete before `to_stage` may start.

    For CONDITIONAL edges, `condition` is a plain-English description of
    the condition (evaluated at runtime by the orchestrator, not by this model).
    """

    from_stage: str = Field(description="Upstream stage (must complete first)")
    to_stage: str = Field(description="Downstream stage (may start after from_stage)")
    dependency_type: DependencyType = DependencyType.SEQUENTIAL
    condition: str | None = Field(
        default=None,
        description="For CONDITIONAL edges: description of the activation condition",
    )

    def __repr__(self) -> str:
        arrow = "→" if self.dependency_type == DependencyType.SEQUENTIAL else "⇢"
        return f"StageDependency({self.from_stage} {arrow} {self.to_stage})"


# ─── WorkflowDefinition ───────────────────────────────────────────────────────


class WorkflowDefinition(BaseModel):
    """
    Immutable DAG describing the SDLC lifecycle template.

    The orchestrator consults this definition at every step to determine:
      - Which stages are currently ready to execute (all predecessors complete)
      - Which stages can execute in parallel (no dependency between them)
      - Which stages will be affected if an upstream output changes

    WorkflowDefinition is the *template*; it is shared across workflow runs.
    Per-run state lives in WorkflowState.
    """

    name: str
    description: str
    version: str = "1.0.0"
    stages: list[str] = Field(
        description="All stage names (DAG nodes). Order here does not imply execution order.",
    )
    dependencies: list[StageDependency] = Field(
        default_factory=list,
        description="DAG edges; absence of an edge means the stages are independent.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Cached networkx graph — built once at init, never mutated
    _graph: nx.DiGraph = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        self._graph = self._build_graph()

    # ── Internal graph construction ────────────────────────────────────────────

    def _build_graph(self) -> nx.DiGraph:
        g: nx.DiGraph = nx.DiGraph()
        g.add_nodes_from(self.stages)
        for dep in self.dependencies:
            g.add_edge(dep.from_stage, dep.to_stage, dependency=dep)
        return g

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_graph(self) -> list[str]:
        """
        Return a list of validation errors (empty = valid).

        Checks:
          - All stages referenced in dependencies are declared in `stages`.
          - The graph is acyclic (no circular dependencies).
          - There is at least one stage.
        """
        errors: list[str] = []
        stage_set = set(self.stages)

        if not self.stages:
            errors.append("WorkflowDefinition must contain at least one stage")

        for dep in self.dependencies:
            if dep.from_stage not in stage_set:
                errors.append(
                    f"Dependency references unknown stage '{dep.from_stage}' "
                    f"(not declared in stages)"
                )
            if dep.to_stage not in stage_set:
                errors.append(
                    f"Dependency references unknown stage '{dep.to_stage}' "
                    f"(not declared in stages)"
                )

        if not self.is_acyclic():
            cycle = self._find_cycle()
            errors.append(f"Workflow DAG contains a cycle: {' → '.join(cycle)}")

        return errors

    def is_acyclic(self) -> bool:
        """Return True if the dependency graph contains no cycles."""
        return nx.is_directed_acyclic_graph(self._graph)

    def _find_cycle(self) -> list[str]:
        """
        Return the first cycle found as a list of node names (for error reporting).

        nx.find_cycle returns a list of edge tuples (u, v[, key, data]).
        We extract node names so they can be joined into a human-readable string.
        """
        try:
            edge_tuples = nx.find_cycle(self._graph)
            nodes = [edge[0] for edge in edge_tuples]
            if edge_tuples:
                nodes.append(edge_tuples[-1][1])  # close the cycle
            return nodes
        except nx.NetworkXNoCycle:
            return []

    # ── Topology queries ──────────────────────────────────────────────────────

    def topological_order(self) -> list[str]:
        """
        Return all stages in a valid sequential execution order.

        If A must complete before B, A appears before B in the result.
        Parallel stages are interleaved arbitrarily — use `get_parallel_groups`
        to identify which can execute concurrently.

        Raises:
            ValueError: If the graph contains a cycle.
        """
        if not self.is_acyclic():
            raise ValueError(
                f"Cannot compute topological order: workflow '{self.name}' contains a cycle"
            )
        return list(nx.topological_sort(self._graph))

    def get_predecessors(self, stage_name: str) -> list[str]:
        """
        Return stages that must complete before `stage_name` may start.

        Only immediate predecessors (one hop). For transitive ancestors,
        use `stages_that_must_precede`.
        """
        return list(self._graph.predecessors(stage_name))

    def get_successors(self, stage_name: str) -> list[str]:
        """
        Return stages that depend on `stage_name` completing.

        Only immediate successors (one hop).
        """
        return list(self._graph.successors(stage_name))

    def stages_that_must_precede(self, stage_name: str) -> set[str]:
        """
        Return ALL stages (transitive) that must complete before this one.

        Includes indirect dependencies (ancestors in the DAG).
        Useful for impact analysis: "if I skip stage X, what downstream
        stages lose their required input?"
        """
        return set(nx.ancestors(self._graph, stage_name))

    def stages_reachable_from(self, stage_name: str) -> set[str]:
        """
        Return ALL stages (transitive) that depend on `stage_name` completing.

        Includes indirect dependants (descendants in the DAG).
        Useful for re-planning: "if stage X's output changed, which downstream
        stages might need to re-run?"
        """
        return set(nx.descendants(self._graph, stage_name))

    def get_ready_stages(
        self,
        completed: set[str],
        in_progress: set[str] | None = None,
    ) -> list[str]:
        """
        Return stages that are ready to execute right now.

        A stage is ready if:
          - All of its predecessors are in `completed`
          - It is not itself in `completed` or `in_progress`

        Multiple ready stages can be returned simultaneously — the caller
        is responsible for executing them in parallel or sequentially.

        Args:
            completed:   Stage names that have finished successfully.
            in_progress: Stage names currently executing (excluded from result).

        Returns:
            Sorted list of ready stage names (sorted for determinism).
        """
        excluded = completed | (in_progress or set())
        ready = []
        for stage in self.stages:
            if stage in excluded:
                continue
            preds = set(self._graph.predecessors(stage))
            if preds <= completed:
                ready.append(stage)
        return sorted(ready)

    def get_parallel_groups(self) -> list[list[str]]:
        """
        Partition all stages into groups that can execute concurrently.

        A stage belongs to group N if the longest path from any root to it
        has N edges. Stages in the same group have no dependency between them
        and can therefore execute in parallel.

        Example for A → B, A → C, B → D, C → D:
          [[A], [B, C], [D]]

        Returns:
            Ordered list of groups. Earlier groups must complete (as a whole)
            before any stage in a later group may start.
        """
        if not self.stages:
            return []

        # Assign each node to a level = max predecessor level + 1
        levels: dict[str, int] = {}
        for node in nx.topological_sort(self._graph):
            preds = list(self._graph.predecessors(node))
            levels[node] = 0 if not preds else max(levels[p] for p in preds) + 1

        max_level = max(levels.values())
        groups: list[list[str]] = []
        for lvl in range(max_level + 1):
            group = sorted(n for n, l in levels.items() if l == lvl)
            groups.append(group)
        return groups

    def get_synchronization_points(self) -> list[str]:
        """
        Return stages that act as synchronization barriers.

        A synchronization point is a stage with more than one predecessor
        (it must wait for multiple parallel branches to converge).
        """
        return sorted(
            stage
            for stage in self.stages
            if self._graph.in_degree(stage) > 1
        )

    def get_dependency(
        self, from_stage: str, to_stage: str
    ) -> StageDependency | None:
        """Return the dependency edge between two stages, or None."""
        if self._graph.has_edge(from_stage, to_stage):
            return self._graph[from_stage][to_stage].get("dependency")
        return None

    def __repr__(self) -> str:
        return (
            f"WorkflowDefinition(name={self.name!r}, "
            f"stages={len(self.stages)}, "
            f"dependencies={len(self.dependencies)})"
        )
