"""
Dynamic workflow replanning domain models.

When an upstream artifact, decision, requirement, or validation result changes,
the WorkflowEngine uses these types to:
  1. Classify the change (ChangeEvent)
  2. Determine which downstream stages are impacted (ImpactAnalysis)
  3. Re-execute only those stages, preserving unaffected work
  4. Record the full replan history for audit and lineage (ReplanResult)

Design principle
────────────────
"Do NOT restart the entire workflow blindly."

Only stages that TRANSITIVELY depend on the changed output are invalidated.
All other completed stages remain COMPLETED and their outputs are preserved.

Terminology
───────────
  originating_stage   The stage whose output changed and triggered the replan.
                      None indicates a root-level (requirement) change that
                      impacts all stages.
  impacted_stages     Stages that must re-execute because they (directly or
                      transitively) consume the originating stage's output.
  preserved_stages    Stages unaffected by the change; their results are safe
                      to reuse without re-execution.

Import chain: stdlib + pydantic only. No orchestrator imports here.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())


# ─── Change classification ────────────────────────────────────────────────────


class ChangeEventType(str, enum.Enum):
    """
    The kind of change that triggered a replan.

    Used by the impact analysis to determine whether to invalidate all
    stages (requirement change) or only transitively dependent stages.
    """

    REQUIREMENT_CHANGE = "requirement_change"
    """
    The original requirement was revised.
    Typically impacts ALL downstream stages.
    """

    ARTIFACT_CHANGED = "artifact_changed"
    """
    A produced artifact (code, schema, migration, …) was updated externally
    or failed validation and was corrected.
    """

    DECISION_CHANGED = "decision_changed"
    """
    An architecture or scope decision was revised.
    Impacts all stages that directly or transitively consumed the decision.
    """

    VALIDATION_FAILED = "validation_failed"
    """
    A validation result for an upstream stage flipped from PASS to FAIL,
    invalidating work that assumed the previous result.
    """

    EXTERNAL_DEPENDENCY_CHANGED = "external_dependency_changed"
    """
    An external API contract, library version, or external schema changed,
    requiring affected implementation and test stages to re-run.
    """


# ─── Change event ─────────────────────────────────────────────────────────────


class ChangeEvent(BaseModel):
    """
    Describes a single change that requires the workflow to replan.

    Operators or agents create a ChangeEvent when they detect that an
    upstream output has changed and downstream stages may now be stale.

    ``originating_stage``
        The stage whose output changed and is now stale.
        Set to ``None`` for requirement-level changes that invalidate all
        stages (``event_type=REQUIREMENT_CHANGE``).

    Example — architecture decision changed::

        ChangeEvent(
            event_type=ChangeEventType.DECISION_CHANGED,
            originating_stage="architecture",
            change_description="Switched from REST to GraphQL",
            rationale="Performance requirements revised",
        )

    Example — requirement changed (invalidates everything)::

        ChangeEvent(
            event_type=ChangeEventType.REQUIREMENT_CHANGE,
            originating_stage=None,
            change_description="Scope expanded to include analytics",
        )
    """

    id: str = Field(default_factory=_uuid)
    event_type: ChangeEventType
    originating_stage: str | None = Field(
        default=None,
        description=(
            "Stage whose output changed. None = requirement-level change "
            "(all stages are impacted)."
        ),
    )
    change_description: str = Field(
        description="Human-readable summary of what changed and why"
    )
    changed_artifact_id: str | None = Field(
        default=None,
        description="ID of the specific artifact that changed (optional, for traceability)",
    )
    changed_decision_id: str | None = Field(
        default=None,
        description="ID of the specific decision that changed (optional, for traceability)",
    )
    rationale: str = Field(
        default="",
        description="Why this change was made / why replanning is needed",
    )
    detected_at: datetime = Field(
        default_factory=_now,
        description="When this change was detected (for audit)",
    )


# ─── Impact analysis ──────────────────────────────────────────────────────────


class ImpactAnalysis(BaseModel):
    """
    Read-only result of analyzing which stages are impacted by a ChangeEvent.

    Produced by ``WorkflowEngine.analyze_impact()`` before replanning begins.
    Exposes the full impact scope so that operators can review it (and choose
    whether to proceed) before committing to a replan.

    A stage is ``impacted`` when it transitively depends on the
    ``originating_stage``'s output and therefore produces stale results if
    the originating output changes.

    A stage is ``preserved`` when it has no dependency path from the
    originating stage and therefore does not need to re-execute.
    """

    change_event: ChangeEvent
    impacted_stages: list[str] = Field(
        description="Stages that must re-execute (sorted for determinism)"
    )
    preserved_stages: list[str] = Field(
        description="Stages whose results are still valid (sorted for determinism)"
    )
    invalidated_artifact_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of artifacts produced by impacted stages that are now stale. "
            "These artifacts should not be consumed by downstream systems until "
            "the replan completes successfully."
        ),
    )
    invalidated_decision_ids: list[str] = Field(
        default_factory=list,
        description="IDs of decisions made in impacted stages that are now stale.",
    )
    analysis_rationale: str = Field(
        description="Human-readable explanation of why these stages are impacted"
    )

    @property
    def has_impact(self) -> bool:
        """True when at least one stage needs replanning."""
        return bool(self.impacted_stages)


# ─── Replan result ────────────────────────────────────────────────────────────


class ReplanResult(BaseModel):
    """
    Immutable record of one replan cycle.

    Stored in ``WorkflowState.replan_history`` (append-only). Provides full
    audit-grade evidence for every replan that occurred in the workflow run:
    what changed, what was impacted, what was preserved, and what governance
    and approval actions were taken.
    """

    id: str = Field(default_factory=_uuid)
    change_event: ChangeEvent
    impact_analysis: ImpactAnalysis

    replan_cycle: int = Field(
        description=(
            "1-indexed replan cycle number. "
            "Increments on every call to WorkflowEngine.replan()."
        )
    )
    stages_replanned: list[str] = Field(
        description="Stages that were successfully re-executed in this cycle"
    )
    stages_preserved: list[str] = Field(
        description="Stages whose previous results were reused without re-execution"
    )

    # Governance and approval tracking for the replan cycle
    governance_reevaluations: list[str] = Field(
        default_factory=list,
        description=(
            "Stage names for which the PolicyEngine ran during this replan cycle. "
            "Populated only when WorkflowEngine has a policy_engine configured."
        ),
    )
    approvals_rerequested: list[str] = Field(
        default_factory=list,
        description=(
            "Stage names that requested fresh human approval during this replan cycle."
        ),
    )

    final_status: str = Field(
        description="WorkflowStatus value at the end of this replan cycle"
    )
    replanned_at: datetime = Field(default_factory=_now)
