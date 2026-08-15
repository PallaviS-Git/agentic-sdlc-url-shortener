"""
Abstract base class for all SDLC pipeline stages.

A stage wraps one or more agents and enforces the gate-execute-gate pattern:
  1. entry_gate  → must pass before execution begins
  2. execute     → delegates to one or more BaseAgent instances
  3. exit_gate   → must pass before the stage is marked complete

Stages may optionally require human approval before the exit gate is evaluated.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from orchestrator.core.autonomy import ActionImpact, HighImpactActionType
from orchestrator.core.failure import DEFAULT_RETRY_POLICY, RetryPolicy
from orchestrator.core.models import GateResult, StageContext


class BaseStage(ABC):
    """
    Abstract contract every SDLC stage must satisfy.

    The orchestrator calls methods in this order:
        entry_gate → [CRITICAL block?] → [approval checkpoint?]
                   → execute → exit_gate → [rollback if failed]

    Subclasses declare their autonomy requirements via class attributes:
      - ``requires_approval``: override to True to mandate human sign-off
      - ``action_impact``:     semantic impact level (drives policy evaluation)
      - ``high_impact_action_type``: well-known category for approver context
    """

    #: Unique name matching the node label in the DAG.
    #: Subclasses MUST set this as a class attribute.
    stage_name: str

    #: When True, the orchestrator pauses and requests human approval before
    #: calling execute(). High-impact stages (e.g. 'release') set this to True.
    #: The engine also derives this automatically when action_impact is at or
    #: above the policy's approval_threshold.
    requires_approval: bool = False

    #: Semantic impact level of the work this stage performs.
    #: The active ApprovalPolicy maps this to an AutonomyLevel at runtime.
    #: Defaults to ROUTINE (no approval required under the default policy).
    action_impact: ActionImpact = ActionImpact.ROUTINE

    #: Optional well-known action category shown to approvers in the request.
    #: Set this alongside a HIGH_IMPACT action_impact to give approvers
    #: specific context (e.g. PRODUCTION_RELEASE, SCHEMA_MIGRATION).
    high_impact_action_type: HighImpactActionType | None = None

    #: Per-stage retry and recovery configuration.
    #: Overrides the engine-level default_retry_policy when set.
    #: Set max_attempts > 1 to enable retries; infinite retries are never allowed.
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY

    #: Metadata made available to the PolicyEngine when the governance gate runs.
    #: Override in subclasses to declare preconditions and context that policies
    #: use to make enforcement decisions.
    #:
    #: Examples (keys are policy-specific)::
    #:
    #:     policy_metadata = {
    #:         "security_scan_passed": True,     # satisfies SEC-001
    #:         "change_ticket_id": "CHG-1234",   # satisfies COMP-001
    #:         "rollback_plan_documented": True,  # satisfies CC-002
    #:     }
    #:
    #: The engine reads this dict but never mutates it.
    #: Override as a class attribute (not an instance attribute) when all
    #: instances of the stage share the same metadata.
    #: Override as an instance attribute when metadata varies per run.
    policy_metadata: dict[str, Any] = {}

    @abstractmethod
    async def entry_gate(self, context: StageContext) -> GateResult:
        """
        Evaluate preconditions that must hold before this stage may start.

        Typical checks: required upstream artifacts exist, policy constraints
        satisfied, dependent stages completed successfully.

        Args:
            context: Stage context containing input_data from upstream stages.

        Returns:
            GateResult with passed=True if execution may proceed.
        """

    @abstractmethod
    async def execute(self, context: StageContext) -> StageContext:
        """
        Run the stage's primary work by coordinating one or more agents.

        The orchestrator calls this only after entry_gate passes (and, if
        requires_approval=True, after a human has approved).

        Args:
            context: Stage context; status will be IN_PROGRESS when called.

        Returns:
            Updated context with output_data and tasks populated.
        """

    @abstractmethod
    async def exit_gate(self, context: StageContext) -> GateResult:
        """
        Evaluate quality criteria that must hold for the stage to complete.

        Typical checks: required output artifacts present, no critical errors,
        test coverage meets threshold, security scan passed.

        Args:
            context: Stage context after execute() has returned.

        Returns:
            GateResult with passed=True if the stage output is acceptable.
        """

    @abstractmethod
    async def rollback(self, context: StageContext) -> StageContext:
        """
        Revert artifacts and side effects produced during execute().

        Called by the orchestrator when the exit gate fails after all retries
        are exhausted, or when an upstream stage initiates a cascade rollback.

        Args:
            context: Stage context at the point requiring rollback.

        Returns:
            Updated context with rollback_performed=True.
        """

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"stage={getattr(self, 'stage_name', 'unnamed')}, "
            f"requires_approval={self.requires_approval})"
        )
