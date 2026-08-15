"""
Abstract base class for all SDLC agents.

Every agent that participates in the orchestration pipeline must subclass
BaseAgent and implement all four abstract methods. This contract ensures:
  - Agents are independently testable (execute / validate / rollback)
  - The orchestrator can call any agent uniformly without knowing its type
  - Rollback is always available if a stage needs to be unwound
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from orchestrator.core.models import StageContext


class BaseAgent(ABC):
    """
    Abstract contract every SDLC agent must satisfy.

    Agents are stateless workers. All execution state lives in the
    StageContext passed to and returned from each method.
    """

    #: Unique, human-readable name; used in audit logs and task assignment.
    #: Subclasses MUST set this as a class attribute.
    name: str

    @abstractmethod
    async def execute(self, context: StageContext) -> StageContext:
        """
        Perform the agent's primary work for a stage.

        Reads from context.input_data, writes results to context.output_data.
        Must not mutate context.status directly — the orchestrator owns that.

        Args:
            context: Current stage execution state.

        Returns:
            Updated context with output_data populated.

        Raises:
            Exception: Any unhandled exception triggers the orchestrator's
                       retry → fallback → rollback chain.
        """

    @abstractmethod
    async def validate_input(self, context: StageContext) -> bool:
        """
        Verify that context.input_data satisfies this agent's preconditions.

        Called by the orchestrator as part of the entry gate evaluation.
        Should be side-effect-free.

        Returns:
            True if input is valid and execution can proceed.
        """

    @abstractmethod
    async def validate_output(self, context: StageContext) -> bool:
        """
        Verify that context.output_data satisfies this agent's exit criteria.

        Called by the orchestrator as part of the exit gate evaluation.
        Should be side-effect-free.

        Returns:
            True if output meets quality criteria and the stage can complete.
        """

    @abstractmethod
    async def rollback(self, context: StageContext) -> StageContext:
        """
        Undo any side effects produced during execute().

        Called by the orchestrator when a stage fails after exceeding retries
        or when an operator-initiated safe-stop requires cleanup.

        Args:
            context: Stage context at the point of failure.

        Returns:
            Updated context with rollback_performed=True and any
            cleanup details recorded in output_data.
        """

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={getattr(self, 'name', 'unnamed')})"
