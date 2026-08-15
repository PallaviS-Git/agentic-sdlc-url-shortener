"""
Cross-stage execution context propagation.

The ExecutionContext solves the "how does stage C get the outputs of stage A
and stage B?" problem without hard-coding data flow paths.

Design:
  - One ExecutionContext per workflow run (shared, accumulated).
  - Each stage reads a *snapshot* of the context (input_data dict) built
    from the outputs of its specific predecessors.
  - When a stage completes, its outputs are *merged* into the context.
  - Artifacts, decisions, and risks accumulate across the full lifecycle.
  - Decision lineage is traversable: given a decision, you can walk the
    `parent_decision_id` chain back to the root decision.

Import chain: this module imports only pydantic, stdlib, and results.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from orchestrator.core.results import (
    Artifact,
    ArtifactType,
    Decision,
    DecisionType,
    Risk,
    RiskSeverity,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ExecutionContext(BaseModel):
    """
    Accumulated cross-stage state for a single workflow run.

    The orchestrator maintains one ExecutionContext throughout a run.
    It grows as stages complete, never shrinks. Downstream stages receive
    a *snapshot* of the relevant portion (their predecessors' outputs)
    rather than the full context — isolating each stage from unrelated data.

    Key responsibilities:
      1. Context propagation  — route A's outputs to B's inputs
      2. Artifact registry    — track all produced artifacts by type
      3. Decision lineage     — trace why a decision was made
      4. Risk accumulation    — surface risks to the final summary
    """

    workflow_id: str
    artifacts: list[Artifact] = Field(
        default_factory=list,
        description="All artifacts produced across all stages, in production order",
    )
    decisions: list[Decision] = Field(
        default_factory=list,
        description="All decisions made, in chronological order",
    )
    risks: list[Risk] = Field(
        default_factory=list,
        description="All risks identified, in chronological order",
    )
    stage_outputs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="stage_name → output_data dict for each completed stage",
    )
    created_at: datetime = Field(default_factory=_now)
    last_updated_at: datetime = Field(default_factory=_now)

    # ── Stage output management ────────────────────────────────────────────────

    def record_stage_output(
        self,
        stage_name: str,
        output_data: dict[str, Any],
        artifacts: list[Artifact] | None = None,
        decisions: list[Decision] | None = None,
        risks: list[Risk] | None = None,
    ) -> None:
        """
        Merge a completed stage's outputs into the context.

        This is the primary mutation method. After a stage completes:
          1. Its output_data is stored under its stage name.
          2. Its artifacts, decisions, and risks are appended to the
             respective accumulation lists.

        Args:
            stage_name:  Name of the stage that just completed.
            output_data: Structured outputs from the stage.
            artifacts:   Artifacts produced during the stage.
            decisions:   Decisions made during the stage.
            risks:       Risks identified during the stage.
        """
        self.stage_outputs[stage_name] = output_data
        if artifacts:
            self.artifacts.extend(artifacts)
        if decisions:
            self.decisions.extend(decisions)
        if risks:
            self.risks.extend(risks)
        self.last_updated_at = _now()

    def snapshot_for_stage(self, predecessors: list[str]) -> dict[str, Any]:
        """
        Build the input_data dict for a stage from its predecessors' outputs.

        Only outputs from the given predecessors are included, preventing
        stages from accidentally depending on unrelated upstream data.

        For a stage with multiple predecessors (a synchronization point),
        outputs are merged in predecessor order. Later predecessors overwrite
        earlier ones on key conflicts — document this as a known trade-off.

        Args:
            predecessors: Stage names whose outputs should be available.

        Returns:
            Merged dict of all relevant predecessor outputs.
        """
        merged: dict[str, Any] = {}
        for pred in predecessors:
            merged.update(self.stage_outputs.get(pred, {}))
        return merged

    def get_output(self, stage_name: str, key: str, default: Any = None) -> Any:
        """
        Retrieve a specific key from a completed stage's output.

        Args:
            stage_name: The stage whose output to query.
            key:        The output key.
            default:    Value returned if key or stage not found.
        """
        return self.stage_outputs.get(stage_name, {}).get(key, default)

    # ── Artifact queries ──────────────────────────────────────────────────────

    def get_artifacts_by_type(self, artifact_type: ArtifactType) -> list[Artifact]:
        """Return all artifacts of the given type, in production order."""
        return [a for a in self.artifacts if a.artifact_type == artifact_type]

    def get_artifacts_by_stage(self, stage_name: str) -> list[Artifact]:
        """Return all artifacts produced by the given stage."""
        return [a for a in self.artifacts if a.produced_by_stage == stage_name]

    def get_artifact_by_id(self, artifact_id: str) -> Artifact | None:
        """Return the artifact with the given id, or None."""
        return next((a for a in self.artifacts if a.id == artifact_id), None)

    # ── Decision queries and lineage ──────────────────────────────────────────

    def get_decisions_by_type(self, decision_type: DecisionType) -> list[Decision]:
        """Return all decisions of the given type, in chronological order."""
        return [d for d in self.decisions if d.decision_type == decision_type]

    def get_decisions_by_stage(self, stage_name: str) -> list[Decision]:
        """Return all decisions made during the given stage."""
        return [d for d in self.decisions if d.stage == stage_name]

    def get_decision_by_id(self, decision_id: str) -> Decision | None:
        """Return the decision with the given id, or None."""
        return next((d for d in self.decisions if d.id == decision_id), None)

    def get_decision_lineage(self, decision_id: str) -> list[Decision]:
        """
        Trace the full ancestry chain of a decision.

        Walks the `parent_decision_id` chain from the given decision back to
        the root (a decision with no parent). Returns the chain in root-first
        order so callers can read it as "A → B → C → given_decision".

        Args:
            decision_id: ID of the decision whose lineage to trace.

        Returns:
            List of decisions from root ancestor to the given decision.
            Returns an empty list if the decision_id is not found.
        """
        chain: list[Decision] = []
        current_id: str | None = decision_id

        # Guard against corrupted data with an explicit iteration limit
        max_depth = len(self.decisions) + 1
        iterations = 0

        while current_id is not None and iterations < max_depth:
            decision = self.get_decision_by_id(current_id)
            if decision is None:
                break
            chain.append(decision)
            current_id = decision.parent_decision_id
            iterations += 1

        chain.reverse()  # root-first order
        return chain

    # ── Risk queries ──────────────────────────────────────────────────────────

    def get_risks_by_severity(self, severity: RiskSeverity) -> list[Risk]:
        """Return all risks at the given severity level."""
        return [r for r in self.risks if r.severity == severity]

    def get_unmitigated_risks(self) -> list[Risk]:
        """Return risks that have no mitigation and have not been accepted."""
        return [r for r in self.risks if not r.mitigation and not r.accepted]

    def get_critical_risks(self) -> list[Risk]:
        """Return all CRITICAL and HIGH severity risks."""
        return [
            r for r in self.risks
            if r.severity in (RiskSeverity.CRITICAL, RiskSeverity.HIGH)
        ]

    # ── Summary ───────────────────────────────────────────────────────────────

    @property
    def completed_stages(self) -> list[str]:
        """Stage names that have recorded output (i.e. completed)."""
        return list(self.stage_outputs.keys())

    @property
    def total_artifacts(self) -> int:
        return len(self.artifacts)

    @property
    def total_decisions(self) -> int:
        return len(self.decisions)

    @property
    def total_risks(self) -> int:
        return len(self.risks)

    def __repr__(self) -> str:
        return (
            f"ExecutionContext("
            f"workflow={self.workflow_id!r}, "
            f"stages_done={len(self.completed_stages)}, "
            f"artifacts={self.total_artifacts}, "
            f"decisions={self.total_decisions}, "
            f"risks={self.total_risks})"
        )
