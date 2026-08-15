"""
Agent autonomy levels, approval policies, and the approval gateway protocol.

This module defines the full controlled-autonomy model:

  Agent autonomy is *not* binary. It is a spectrum:

    FULL_AUTO  ──► SUPERVISED  ──► APPROVAL_REQUIRED  ──► HUMAN_ONLY

  Every agent action has an *impact level* that maps to an autonomy
  requirement under the active policy:

    ROUTINE     → FULL_AUTO      (agents execute freely)
    SIGNIFICANT → SUPERVISED     (agents execute; action is logged for review)
    HIGH_IMPACT → APPROVAL_REQUIRED (human must approve before agent executes)
    CRITICAL    → HUMAN_ONLY    (agent may recommend; only a human may execute)

  The WorkflowEngine enforces this at runtime via the ApprovalGateway:

    - CRITICAL stages are rejected immediately (no gateway call needed).
    - HIGH_IMPACT stages pause the workflow (AWAITING_APPROVAL), call
      gateway.request_approval(), then proceed or fail based on the decision.
    - Approval cannot be bypassed: absent a gateway, protected stages fail.

Import chain: stdlib + pydantic only. No orchestrator imports here.
"""
from __future__ import annotations

import asyncio
import enum
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ─── Autonomy levels ──────────────────────────────────────────────────────────


class AutonomyLevel(str, enum.Enum):
    """
    How much autonomy an agent has for a given action.

    Ordered from most to least autonomous. The WorkflowEngine enforces
    the autonomy level dictated by the active ApprovalPolicy.
    """

    FULL_AUTO = "full_auto"
    """Agent executes immediately, no human oversight required."""

    SUPERVISED = "supervised"
    """Agent executes but the action is logged for post-hoc review."""

    APPROVAL_REQUIRED = "approval_required"
    """A human must explicitly approve before the agent executes."""

    HUMAN_ONLY = "human_only"
    """Only a human may perform this action; agents are completely blocked."""


# ─── Action impact ────────────────────────────────────────────────────────────


class ActionImpact(str, enum.Enum):
    """
    Potential impact of an agent action on the system.

    The default ApprovalPolicy maps these to AutonomyLevel requirements:
      ROUTINE     → FULL_AUTO
      SIGNIFICANT → SUPERVISED
      HIGH_IMPACT → APPROVAL_REQUIRED
      CRITICAL    → HUMAN_ONLY
    """

    ROUTINE = "routine"
    """Safe, easily reversible, low blast radius. E.g. reading configuration."""

    SIGNIFICANT = "significant"
    """Noteworthy but manageable if wrong. E.g. updating a feature flag."""

    HIGH_IMPACT = "high_impact"
    """
    Potentially harmful if executed incorrectly. Requires human approval.

    Examples:
      - Production / release actions
      - Schema / data migrations
      - Security-sensitive configuration changes
      - High-risk code changes touching critical paths
      - Destructive changes that delete or overwrite data
    """

    CRITICAL = "critical"
    """
    Irreversible, catastrophic, or security-critical.

    Agents may RECOMMEND the action but may NEVER execute it.
    Only a verified human operator may perform CRITICAL actions.
    """


# Ordinal for policy comparisons (higher = more impactful)
_IMPACT_ORDER: dict[ActionImpact, int] = {
    ActionImpact.ROUTINE: 0,
    ActionImpact.SIGNIFICANT: 1,
    ActionImpact.HIGH_IMPACT: 2,
    ActionImpact.CRITICAL: 3,
}


# ─── High-impact action types ─────────────────────────────────────────────────


class HighImpactActionType(str, enum.Enum):
    """
    Well-known categories of high-impact actions.

    Stage implementations declare which category their high-impact
    action belongs to, so the approval request gives the human
    approver specific context about what they are approving.
    """

    PRODUCTION_RELEASE = "production_release"
    """Deploying code or infrastructure to a production environment."""

    DESTRUCTIVE_CHANGE = "destructive_change"
    """Deleting data, dropping tables, or removing infrastructure."""

    SECURITY_CHANGE = "security_change"
    """Modifying auth, encryption keys, access policies, or secrets."""

    SCHEMA_MIGRATION = "schema_migration"
    """Altering database schemas or running migration scripts."""

    HIGH_RISK_CODE = "high_risk_code"
    """Modifying payment, auth, data-integrity, or safety-critical paths."""

    DATA_DELETION = "data_deletion"
    """Bulk-deleting or archiving user or system data."""

    CONFIG_CHANGE = "config_change"
    """Modifying production configuration that affects live traffic."""

    EXTERNAL_SERVICE_CALL = "external_service_call"
    """Invoking an irreversible or billed external API."""


# ─── Agent autonomy mode ──────────────────────────────────────────────────────


class AgentAutonomyMode(str, enum.Enum):
    """
    Whether an agent is executing an action or merely recommending it.

    Agents operating on HIGH_IMPACT actions must execute only after
    approval. Agents operating on CRITICAL actions must only RECOMMEND
    — a human carries out the actual execution.
    """

    EXECUTE = "execute"
    """Agent performs the action autonomously (after any required approval)."""

    RECOMMEND = "recommend"
    """Agent proposes the action; a human decides whether to execute it."""


# ─── Agent action ─────────────────────────────────────────────────────────────


class AgentAction(BaseModel):
    """
    A specific action an agent wants to perform, with full impact classification.

    AgentAction is the primary input to the approval system. The engine
    attaches it to an ApprovalRequest so approvers understand exactly
    what they are authorising.
    """

    id: str = Field(default_factory=_uuid)
    title: str = Field(description="Short human-readable action name")
    description: str = Field(description="Detailed description of what will happen")
    impact: ActionImpact
    action_type: HighImpactActionType | None = Field(
        default=None,
        description="Optional well-known category for richer approver context",
    )
    agent_name: str = Field(description="Name of the agent proposing the action")
    autonomy_mode: AgentAutonomyMode = AgentAutonomyMode.EXECUTE
    is_destructive: bool = False
    is_security_sensitive: bool = False
    is_irreversible: bool = False
    proposed_at: datetime = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ─── Approval policy ──────────────────────────────────────────────────────────


class ApprovalPolicy(BaseModel):
    """
    Rules that map ActionImpact → required AutonomyLevel.

    The default policy requires approval for HIGH_IMPACT and CRITICAL,
    and blocks agent execution entirely for CRITICAL.  Override the
    threshold fields to make policies more or less restrictive.

    Enforcement by the WorkflowEngine:
      1. action_impact >= critical_threshold  → HUMAN_ONLY (instant block)
      2. action_impact >= approval_threshold  → APPROVAL_REQUIRED (request gateway)
      3. action_impact >= supervised_threshold → SUPERVISED (execute + log)
      4. otherwise                            → FULL_AUTO
    """

    # The lowest impact level that requires human approval
    approval_threshold: ActionImpact = ActionImpact.HIGH_IMPACT

    # The lowest impact level that blocks agent execution entirely
    critical_threshold: ActionImpact = ActionImpact.CRITICAL

    def required_autonomy(self, impact: ActionImpact) -> AutonomyLevel:
        """Return the autonomy level required for the given impact."""
        rank = _IMPACT_ORDER[impact]
        if rank >= _IMPACT_ORDER[self.critical_threshold]:
            return AutonomyLevel.HUMAN_ONLY
        if rank >= _IMPACT_ORDER[self.approval_threshold]:
            return AutonomyLevel.APPROVAL_REQUIRED
        if impact == ActionImpact.SIGNIFICANT:
            return AutonomyLevel.SUPERVISED
        return AutonomyLevel.FULL_AUTO

    def requires_human_approval(self, impact: ActionImpact) -> bool:
        """True when the impact level requires human approval before execution."""
        autonomy = self.required_autonomy(impact)
        return autonomy in (AutonomyLevel.APPROVAL_REQUIRED, AutonomyLevel.HUMAN_ONLY)

    def allows_agent_execution(self, impact: ActionImpact) -> bool:
        """
        True when an agent may execute the action (with or without approval).
        False for HUMAN_ONLY — agents are blocked even if a human approves.
        """
        return self.required_autonomy(impact) != AutonomyLevel.HUMAN_ONLY


# ─── Approval request / decision ─────────────────────────────────────────────


class ApprovalRequest(BaseModel):
    """
    A formal request for human approval of a high-impact stage or action.

    Created by the engine when it reaches an approval checkpoint.
    Passed to ApprovalGateway.request_approval() and stored in
    WorkflowState so the audit trail is complete even if the request
    times out or is escalated.
    """

    id: str = Field(default_factory=_uuid)
    workflow_id: str
    stage_name: str
    requesting_agent: str = Field(
        default="orchestrator",
        description="Stage implementation or agent requesting approval",
    )
    action: AgentAction | None = Field(
        default=None,
        description="The specific AgentAction being approved (None = stage-level approval)",
    )
    stage_summary: str = Field(
        description="Plain-text summary of what the stage is about to do"
    )
    risk_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Risks, affected systems, rollback plan for the approver",
    )
    upstream_artifact_ids: list[str] = Field(
        default_factory=list,
        description="IDs of artifacts from upstream stages relevant to this decision",
    )
    escalation_level: int = Field(
        default=0,
        description="0=initial; 1=first escalation; 2=senior escalation",
    )
    requested_at: datetime = Field(default_factory=_now)


class ApprovalDecision(BaseModel):
    """
    A human's response to an ApprovalRequest.

    Returned synchronously by ApprovalGateway.request_approval().
    The engine converts this into an Approval audit record (results.py)
    and stores it in WorkflowState.approvals.
    """

    request_id: str
    approved: bool
    approver: str = Field(description="Identity of the approver (human or system)")
    rationale: str = Field(description="Why this decision was made")
    is_override: bool = Field(
        default=False,
        description="True when the approver overrides an agent's recommendation",
    )
    override_reason: str | None = Field(
        default=None,
        description="Explanation for the override (required when is_override=True)",
    )
    escalation_level: int = Field(
        default=0,
        description="Escalation level at which this decision was made",
    )
    decided_at: datetime = Field(default_factory=_now)


# ─── Approval gateway ─────────────────────────────────────────────────────────


class ApprovalGateway(ABC):
    """
    Abstract interface for obtaining human approval decisions.

    The engine calls request_approval() when it reaches a checkpoint.
    In production this would be backed by a ticketing system, Slack bot,
    or web UI. In tests, use one of the provided concrete implementations.

    Implementations MUST NOT allow CRITICAL (HUMAN_ONLY) actions to be
    approved for agent execution — that enforcement lives in the engine,
    but gateways should document the same policy.
    """

    @abstractmethod
    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """
        Submit an approval request and return the decision.

        In a synchronous test gateway this returns immediately.
        In a production gateway this may await an external event
        (webhook, database poll, etc.).

        Args:
            request: Full context needed for the approver to decide.

        Returns:
            ApprovalDecision with approved=True or False.
        """


class AutoApproveGateway(ApprovalGateway):
    """
    Always approves every request.

    Use for testing happy-path scenarios where human approval is a
    formality. NEVER use in production.
    """

    def __init__(self, approver: str = "auto-approve") -> None:
        self.approver = approver

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            request_id=request.id,
            approved=True,
            approver=self.approver,
            rationale=f"Auto-approved stage '{request.stage_name}'",
            escalation_level=request.escalation_level,
        )


class AutoRejectGateway(ApprovalGateway):
    """
    Always rejects every request.

    Use for testing rejection and fail-safe scenarios.
    NEVER use in production.
    """

    def __init__(self, approver: str = "auto-reject", reason: str = "") -> None:
        self.approver = approver
        self.reason = reason or "Auto-rejected for testing"

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            request_id=request.id,
            approved=False,
            approver=self.approver,
            rationale=self.reason,
            escalation_level=request.escalation_level,
        )


class PresetApprovalGateway(ApprovalGateway):
    """
    Returns pre-configured decisions keyed by stage name.

    Use for testing specific approval/rejection scenarios per stage.

    Example::

        gateway = PresetApprovalGateway({
            "release": True,       # approve release
            "schema_migration": False,  # reject migration
            "*": True,             # approve everything else
        })
    """

    def __init__(self, decisions: dict[str, bool]) -> None:
        """
        Args:
            decisions: Map of stage_name → approved (True/False).
                       "*" is a wildcard default. If a stage is not
                       found and no wildcard exists, the request is rejected.
        """
        self._decisions = decisions

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        approved = self._decisions.get(
            request.stage_name,
            self._decisions.get("*", False),
        )
        return ApprovalDecision(
            request_id=request.id,
            approved=approved,
            approver="preset-gateway",
            rationale=(
                f"Preset decision for stage '{request.stage_name}': "
                f"{'approved' if approved else 'rejected'}"
            ),
            escalation_level=request.escalation_level,
        )


class EscalatingApprovalGateway(ApprovalGateway):
    """
    Tries each delegate gateway in order, escalating on rejection.

    Escalation flow:
      level 0 → first gateway decides
        → if rejected: level 1 → second gateway decides
          → if rejected: level 2 → third gateway decides
            → final decision returned (even if all reject)

    The ApprovalDecision carries the escalation_level at which the
    decision was made, which is stored in the Approval audit record.

    Example (QA lead decides first; VP engineering decides if rejected)::

        gateway = EscalatingApprovalGateway([
            PresetApprovalGateway({"release": False}),  # QA lead rejects
            AutoApproveGateway("vp-engineering"),        # VP approves on escalation
        ])
    """

    def __init__(self, gateways: list[ApprovalGateway]) -> None:
        if not gateways:
            raise ValueError(
                "EscalatingApprovalGateway requires at least one gateway"
            )
        self._gateways = gateways

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        final_decision = ApprovalDecision(
            request_id=request.id,
            approved=False,
            approver="escalation-chain-exhausted",
            rationale="All escalation levels rejected the request",
            escalation_level=len(self._gateways) - 1,
        )

        for level, gateway in enumerate(self._gateways):
            escalated_request = request.model_copy(
                update={"escalation_level": level}
            )
            decision = await gateway.request_approval(escalated_request)
            final_decision = decision.model_copy(
                update={"escalation_level": level}
            )
            if decision.approved:
                return final_decision

        return final_decision


class HumanApprovalGateway(ApprovalGateway):
    """
    CLI human-in-the-loop approval gateway for live demos and operator use.

    Prints the approval request to stdout and blocks until the operator
    answers ``y`` / ``n`` (or equivalent). Inject ``input_fn`` / ``output_fn``
    in tests to avoid real stdin.

    Example::

        gateway = HumanApprovalGateway(approver="alice")
        engine = WorkflowEngine(..., approval_gateway=gateway)
    """

    def __init__(
        self,
        *,
        approver: str = "human-operator",
        input_fn: Callable[..., str] | None = None,
        output_fn: Callable[..., None] | None = None,
    ) -> None:
        self.approver = approver
        self._input: Callable[..., str] = input_fn if input_fn is not None else input
        self._output: Callable[..., None] = (
            output_fn if output_fn is not None else print
        )

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self._output("")
        self._output("=" * 60)
        self._output("APPROVAL REQUIRED")
        self._output("=" * 60)
        self._output(f"  Workflow : {request.workflow_id}")
        self._output(f"  Stage    : {request.stage_name}")
        self._output(f"  Agent    : {request.requesting_agent}")
        self._output(f"  Summary  : {request.stage_summary}")
        if request.risk_context:
            self._output(f"  Risk     : {request.risk_context}")
        if request.action is not None:
            self._output(f"  Action   : {request.action}")
        self._output("=" * 60)

        while True:
            raw = await asyncio.to_thread(
                self._input, "Approve this stage? [y/N]: "
            )
            answer = (raw or "").strip().lower()
            if answer in {"y", "yes"}:
                return ApprovalDecision(
                    request_id=request.id,
                    approved=True,
                    approver=self.approver,
                    rationale=f"Human approved stage '{request.stage_name}'",
                    escalation_level=request.escalation_level,
                )
            if answer in {"n", "no", ""}:
                return ApprovalDecision(
                    request_id=request.id,
                    approved=False,
                    approver=self.approver,
                    rationale=f"Human rejected stage '{request.stage_name}'",
                    escalation_level=request.escalation_level,
                )
            self._output("Please answer 'y' or 'n'.")


# ─── Default policy ───────────────────────────────────────────────────────────

DEFAULT_APPROVAL_POLICY = ApprovalPolicy(
    approval_threshold=ActionImpact.HIGH_IMPACT,
    critical_threshold=ActionImpact.CRITICAL,
)
"""
Standard policy used by WorkflowEngine when no custom policy is supplied.

  ROUTINE     → FULL_AUTO      (no approval)
  SIGNIFICANT → SUPERVISED     (no approval, logged)
  HIGH_IMPACT → APPROVAL_REQUIRED
  CRITICAL    → HUMAN_ONLY     (agent execution blocked)
"""
