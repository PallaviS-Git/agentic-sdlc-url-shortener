"""
Abstract base class for the agentic SDLC orchestration engine.

The concrete implementation (added in Step 4) will use a networkx DAG
to drive stage execution. This interface is kept here so that:
  - Tests can mock the orchestrator without importing the concrete engine
  - The three scenario modules (greenfield, brownfield, ambiguous) depend
    only on this stable interface
  - Alternative orchestrator implementations can be swapped in without
    touching any calling code
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from orchestrator.core.models import Requirement, WorkflowState


class BaseOrchestrator(ABC):
    """
    Abstract contract for the Agentic SDLC orchestration engine.

    Lifecycle of a workflow:
        run() → [pause() → resume()]* → [stop()] → get_state()

    All methods are async to allow the engine to perform I/O (state
    persistence, agent calls, approval polling) without blocking.
    """

    @abstractmethod
    async def run(self, requirement: Requirement) -> WorkflowState:
        """
        Execute the full SDLC lifecycle for a requirement.

        The engine traverses the DAG of stages, evaluating entry/exit gates,
        coordinating agents, handling retries, and requesting approvals where
        required. Execution may be non-linear (parallel branches, dynamic
        re-planning when upstream outputs change).

        Args:
            requirement: Normalized requirement to drive the lifecycle.

        Returns:
            Final WorkflowState (status COMPLETED, FAILED, or STOPPED).
        """

    @abstractmethod
    async def pause(self, workflow_id: str) -> WorkflowState:
        """
        Suspend execution at the next synchronization point.

        The engine finishes the currently running stage, then halts.
        State is persisted so resume() can continue from the same point.

        Args:
            workflow_id: ID of the workflow to pause.

        Returns:
            WorkflowState with status PAUSED.
        """

    @abstractmethod
    async def resume(self, workflow_id: str) -> WorkflowState:
        """
        Resume a paused workflow from its last safe checkpoint.

        Args:
            workflow_id: ID of the paused workflow.

        Returns:
            Final WorkflowState after resuming to completion (or next pause/stop).
        """

    @abstractmethod
    async def stop(self, workflow_id: str) -> WorkflowState:
        """
        Safe-stop: halt the workflow and preserve full state for audit.

        Unlike pause(), a stopped workflow cannot be resumed. Triggers
        rollback of the current in-progress stage.

        Args:
            workflow_id: ID of the workflow to stop.

        Returns:
            WorkflowState with status STOPPED.
        """

    @abstractmethod
    async def get_state(self, workflow_id: str) -> WorkflowState:
        """
        Return the current persisted state of a workflow.

        Args:
            workflow_id: ID of the workflow to inspect.

        Returns:
            Most recent WorkflowState snapshot.

        Raises:
            KeyError: If workflow_id is not found.
        """

    @abstractmethod
    async def request_approval(
        self,
        workflow_id: str,
        stage_name: str,
        context: dict[str, Any],
    ) -> bool:
        """
        Block until a human approves or rejects a high-impact action.

        The approval request is logged to the audit trail regardless of outcome.
        Times out after Settings.approval_timeout_seconds and defaults to
        rejection (fail-safe behaviour).

        Args:
            workflow_id: ID of the workflow awaiting approval.
            stage_name:  Name of the stage requesting approval.
            context:     Summary of the action requiring approval.

        Returns:
            True if approved, False if rejected or timed out.
        """
