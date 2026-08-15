"""
Workflow context and decision lineage — unified provenance query model.

WorkflowLineage is a read-only projection built from a completed (or
in-progress) WorkflowState.  It answers nine concrete provenance questions
without requiring callers to navigate nested model structures or rely on
hidden global state:

  1. What requirement initiated this workflow?
  2. Which tasks were created?
  3. Why was a task created?
  4. Which artifact produced / triggered a task?
  5. Which decision led to the next stage?
  6. Which agent executed a task?
  7. What validation was performed?
  8. What changed? (audit trail)
  9. What was approved?

Usage::

    from orchestrator.core.lineage import build_lineage

    lineage = build_lineage(workflow_state)

    # Q1
    req = lineage.initiating_requirement

    # Q5
    decision = lineage.get_decision_for_transition("implementation")

    # Full chain from a leaf decision back to the root
    chain = lineage.trace_decision_lineage("d-impl-001")

Import chain: lineage.py → models.py → results.py.
No circular dependencies.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from orchestrator.core.models import (
    AuditEntry,
    Requirement,
    StageTransition,
    Task,
    WorkflowState,
)
from orchestrator.core.results import (
    Approval,
    Artifact,
    Decision,
    Risk,
    ValidationResult,
)


# ─── WorkflowLineage ──────────────────────────────────────────────────────────


class WorkflowLineage(BaseModel):
    """
    Complete, queryable provenance record for a workflow run.

    Build via :func:`build_lineage`.  All collections are flattened from
    their per-stage homes so that queries are a single method call rather
    than nested iteration across StageContexts.

    The model is intentionally *read-only* (no mutation helpers).
    Modify WorkflowState and rebuild to reflect new information.
    """

    workflow_id: str
    requirement: Requirement

    # ── Flattened collections (populated by build_lineage) ────────────────────
    decisions: list[Decision] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    stage_transitions: list[StageTransition] = Field(default_factory=list)
    audit_trail: list[AuditEntry] = Field(default_factory=list)

    # ═════════════════════════════════════════════════════════════════════════
    # Q1 — What requirement initiated this workflow?
    # ═════════════════════════════════════════════════════════════════════════

    @property
    def initiating_requirement(self) -> Requirement:
        """The requirement that started this workflow run."""
        return self.requirement

    # ═════════════════════════════════════════════════════════════════════════
    # Q2 — Which tasks were created?
    # ═════════════════════════════════════════════════════════════════════════

    def get_all_tasks(self) -> list[Task]:
        """All tasks created during this workflow, across every stage."""
        return self.tasks

    def get_tasks_for_stage(self, stage_name: str) -> list[Task]:
        """Tasks that belong to *stage_name*."""
        return [t for t in self.tasks if t.stage == stage_name]

    # ═════════════════════════════════════════════════════════════════════════
    # Q3 — Why was a task created?
    # ═════════════════════════════════════════════════════════════════════════

    def get_task_rationale(self, task_id: str) -> str | None:
        """
        Return the rationale text recorded on *task_id*, or None if the
        task does not exist.
        """
        task = self._task_by_id(task_id)
        return task.rationale if task else None

    def get_task_provenance(self, task_id: str) -> dict[str, Any]:
        """
        Return a dict summarising every provenance dimension of *task_id*:
        rationale, decision that created it, artifact that triggered it,
        assigned agent.  Values are None when not set.
        """
        task = self._task_by_id(task_id)
        if task is None:
            return {}
        creating_decision = (
            self._decision_by_id(task.created_by_decision_id)
            if task.created_by_decision_id
            else None
        )
        triggering_artifact = (
            self._artifact_by_id(task.created_by_artifact_id)
            if task.created_by_artifact_id
            else None
        )
        return {
            "task_id": task.id,
            "title": task.title,
            "stage": task.stage,
            "rationale": task.rationale or None,
            "creating_decision": (
                {"id": creating_decision.id, "title": creating_decision.title}
                if creating_decision
                else None
            ),
            "triggering_artifact": (
                {"id": triggering_artifact.id, "name": triggering_artifact.name}
                if triggering_artifact
                else None
            ),
            "assigned_agent": task.assigned_agent,
        }

    # ═════════════════════════════════════════════════════════════════════════
    # Q4 — Which artifact produced / triggered a task?
    # ═════════════════════════════════════════════════════════════════════════

    def get_artifact_for_task(self, task_id: str) -> Artifact | None:
        """
        Return the Artifact that triggered the creation of *task_id*
        (i.e. the artifact stored in Task.created_by_artifact_id), or None.
        """
        task = self._task_by_id(task_id)
        if task and task.created_by_artifact_id:
            return self._artifact_by_id(task.created_by_artifact_id)
        return None

    def get_artifacts_for_stage(self, stage_name: str) -> list[Artifact]:
        """All artifacts produced by *stage_name*."""
        return [a for a in self.artifacts if a.produced_by_stage == stage_name]

    def get_artifact_by_id(self, artifact_id: str) -> Artifact | None:
        """Look up an artifact by its ID."""
        return self._artifact_by_id(artifact_id)

    # ═════════════════════════════════════════════════════════════════════════
    # Q5 — Which decision led to the next stage?
    # ═════════════════════════════════════════════════════════════════════════

    def get_transition_to_stage(self, stage_name: str) -> StageTransition | None:
        """The StageTransition record for when *stage_name* started."""
        return next(
            (t for t in self.stage_transitions if t.stage_name == stage_name), None
        )

    def get_decision_for_transition(self, stage_name: str) -> Decision | None:
        """
        Return the Decision that most directly drove the start of *stage_name*.

        Resolution order:
        1. The StageTransition.driving_decision_id set explicitly on the
           transition (by agent implementations).
        2. Any Decision whose downstream_impacts list includes *stage_name*
           (regardless of which stage produced it).

        Returns None when no decision can be identified.
        """
        transition = self.get_transition_to_stage(stage_name)
        if transition and transition.driving_decision_id:
            return self._decision_by_id(transition.driving_decision_id)

        # Fallback: scan all decisions for one that lists this stage as a
        # downstream impact. The first match wins.
        for decision in self.decisions:
            if stage_name in (decision.downstream_impacts or []):
                return decision
        return None

    # ═════════════════════════════════════════════════════════════════════════
    # Q6 — Which agent executed a task?
    # ═════════════════════════════════════════════════════════════════════════

    def get_agent_for_task(self, task_id: str) -> str | None:
        """Return the assigned_agent name for *task_id*, or None."""
        task = self._task_by_id(task_id)
        return task.assigned_agent if task else None

    # ═════════════════════════════════════════════════════════════════════════
    # Q7 — What validation was performed?
    # ═════════════════════════════════════════════════════════════════════════

    def get_validations_for_stage(self, stage_name: str) -> list[ValidationResult]:
        """ValidationResult records attributed to *stage_name*."""
        return [v for v in self.validations if v.stage == stage_name]

    def get_all_validations(self) -> list[ValidationResult]:
        """All validation results across every stage."""
        return self.validations

    def get_failed_validations(self) -> list[ValidationResult]:
        """Validation results where the check did not pass."""
        return [v for v in self.validations if not v.passed]

    # ═════════════════════════════════════════════════════════════════════════
    # Q8 — What changed?
    # ═════════════════════════════════════════════════════════════════════════

    def get_changes_for_stage(self, stage_name: str) -> list[AuditEntry]:
        """Audit entries scoped to *stage_name* (ordered by timestamp)."""
        entries = [e for e in self.audit_trail if e.stage == stage_name]
        return sorted(entries, key=lambda e: e.timestamp)

    def get_full_change_log(self) -> list[AuditEntry]:
        """All audit entries across the workflow, ordered chronologically."""
        return sorted(self.audit_trail, key=lambda e: e.timestamp)

    # ═════════════════════════════════════════════════════════════════════════
    # Q9 — What was approved?
    # ═════════════════════════════════════════════════════════════════════════

    def get_approvals(self) -> list[Approval]:
        """All approval records for this workflow run."""
        return self.approvals

    def get_approvals_for_stage(self, stage_name: str) -> list[Approval]:
        """Approvals whose stage_name matches *stage_name*."""
        return [a for a in self.approvals if a.stage_name == stage_name]

    # ═════════════════════════════════════════════════════════════════════════
    # Decision lineage
    # ═════════════════════════════════════════════════════════════════════════

    def trace_decision_lineage(self, decision_id: str) -> list[Decision]:
        """
        Walk the parent_decision_id chain from *decision_id* all the way
        back to the root decision, then return the chain in root-first order.

        Example: if d3.parent=d2 and d2.parent=d1, calling with d3 returns
        [d1, d2, d3].

        Terminates on a missing parent or a cycle (guards against infinite
        loops with a seen-set).
        """
        chain: list[Decision] = []
        current_id: str | None = decision_id
        seen: set[str] = set()

        while current_id and current_id not in seen:
            seen.add(current_id)
            decision = self._decision_by_id(current_id)
            if decision is None:
                break
            chain.append(decision)
            current_id = decision.parent_decision_id

        chain.reverse()
        return chain

    def get_decision_by_id(self, decision_id: str) -> Decision | None:
        """Look up a decision by its ID."""
        return self._decision_by_id(decision_id)

    def get_decisions_for_stage(self, stage_name: str) -> list[Decision]:
        """All decisions made during *stage_name*."""
        return [d for d in self.decisions if d.stage == stage_name]

    # ═════════════════════════════════════════════════════════════════════════
    # Summary
    # ═════════════════════════════════════════════════════════════════════════

    def summary(self) -> dict[str, Any]:
        """
        Return a compact dict that directly answers all nine provenance
        questions.  Useful for logging, reporting, and display.
        """
        return {
            "workflow_id": self.workflow_id,
            # Q1
            "initiating_requirement": {
                "id": self.requirement.id,
                "title": self.requirement.title,
                "type": self.requirement.requirement_type.value,
            },
            # Q2
            "tasks_created": [
                {
                    "id": t.id,
                    "title": t.title,
                    "stage": t.stage,
                    "agent": t.assigned_agent,
                    "rationale": t.rationale or None,
                }
                for t in self.tasks
            ],
            # Q3 — covered per-task via get_task_rationale / get_task_provenance
            # Q4 — covered per-task via get_artifact_for_task
            # Q5
            "stage_transitions": [
                {
                    "stage": t.stage_name,
                    "predecessors": t.predecessor_stages,
                    "driving_decision_id": t.driving_decision_id,
                    "reason": t.transition_reason,
                }
                for t in self.stage_transitions
            ],
            # Q6 — covered per-task via get_agent_for_task
            # Q7
            "validations_performed": len(self.validations),
            "failed_validations": len(self.get_failed_validations()),
            # Q8
            "audit_events": len(self.audit_trail),
            # Q9
            "approvals": [
                {"id": a.id, "stage": a.stage_name, "status": a.status.value}
                for a in self.approvals
            ],
            # aggregates
            "decisions_made": len(self.decisions),
            "artifacts_produced": len(self.artifacts),
            "risks_identified": len(self.risks),
        }

    # ═════════════════════════════════════════════════════════════════════════
    # Private helpers
    # ═════════════════════════════════════════════════════════════════════════

    def _task_by_id(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def _decision_by_id(self, decision_id: str) -> Decision | None:
        return next((d for d in self.decisions if d.id == decision_id), None)

    def _artifact_by_id(self, artifact_id: str) -> Artifact | None:
        return next((a for a in self.artifacts if a.id == artifact_id), None)


# ─── Factory ──────────────────────────────────────────────────────────────────


def build_lineage(state: WorkflowState) -> WorkflowLineage:
    """
    Build a WorkflowLineage from a WorkflowState.

    This function performs two normalisation steps:

    * Tasks that carry an empty ``stage`` field are annotated with the stage
      name derived from their owning StageContext.
    * ValidationResults that carry an empty ``stage`` field are annotated
      with the stage name derived from their owning StageContext.

    The original WorkflowState is never mutated; copies are used where
    annotation is needed.
    """
    all_tasks: list[Task] = []
    all_validations: list[ValidationResult] = []

    for stage_name, ctx in state.stages.items():
        # Annotate tasks with stage name when absent
        for task in ctx.tasks:
            if task.stage:
                all_tasks.append(task)
            else:
                all_tasks.append(task.model_copy(update={"stage": stage_name}))

        # Annotate validations with stage name when absent
        for v in ctx.validations:
            if v.stage:
                all_validations.append(v)
            else:
                all_validations.append(v.model_copy(update={"stage": stage_name}))

    return WorkflowLineage(
        workflow_id=state.id,
        requirement=state.requirement,
        decisions=state.all_decisions,
        tasks=all_tasks,
        artifacts=state.all_artifacts,
        validations=all_validations,
        risks=state.all_risks,
        approvals=state.approvals,
        stage_transitions=state.stage_transitions,
        audit_trail=state.audit_trail,
    )
