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

from orchestrator.core.models import GateResult, StageContext


class BaseStage(ABC):
    """
    Abstract contract every SDLC stage must satisfy.

    The orchestrator calls methods in this order:
        entry_gate → [approval?] → execute → exit_gate → [rollback if failed]
    """

    #: Unique name matching the node label in the DAG.
    #: Subclasses MUST set this as a class attribute.
    stage_name: str

    #: When True, the orchestrator pauses and requests human approval before
    #: calling execute(). High-impact stages (e.g. 'release') set this to True.
    requires_approval: bool = False

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
