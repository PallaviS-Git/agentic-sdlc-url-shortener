"""
Governance and policy guardrail layer for the Agentic SDLC orchestration system.

All high-impact agent actions pass through the PolicyEngine before execution.
A policy violation that produces BLOCK or REQUIRE_APPROVAL prevents or pauses
the action; the enforcement decision is always recorded in the audit trail.

Design principles
─────────────────
  Explicit     Each policy is a named object with a single, documented rule.
  Testable     Policies accept an ActionContext value; no I/O or side effects.
  Deterministic Given the same ActionContext, the same decision is returned.
               (FreezeWindowPolicy accepts an injected clock for testability.)
  Auditable    Every evaluation produces a PolicyEvaluationRecord that the
               engine stores on WorkflowState and emits to the audit trail.
  Extensible   Implement Policy ABC and register it with PolicyEngine.

Domain taxonomy
───────────────
  SECURITY       Controls that protect system integrity and confidentiality.
  COMPLIANCE     Regulatory and organisational obligations.
  CHANGE_CONTROL Change management requirements before production modification.

Enforcement decisions (worst wins when aggregating)
────────────────────────────────────────────────────
  ALLOW              No restriction; execution proceeds.
  WARN               Proceed but record a warning in the audit trail.
  REQUIRE_APPROVAL   Pause for human approval before executing.
  BLOCK              Prevent execution entirely; stage fails.

Import chain: orchestrator.core.autonomy + stdlib + pydantic only.
"""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field

from orchestrator.core.autonomy import ActionImpact, HighImpactActionType


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


# ─── Enumerations ─────────────────────────────────────────────────────────────


class PolicyDomain(str, enum.Enum):
    """High-level category of a policy."""

    SECURITY = "security"
    COMPLIANCE = "compliance"
    CHANGE_CONTROL = "change_control"


class EnforcementDecision(str, enum.Enum):
    """
    What the policy engine decides to do when a policy fires.

    When aggregating multiple violations the *worst* decision wins::

        BLOCK > REQUIRE_APPROVAL > WARN > ALLOW
    """

    ALLOW = "allow"
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


_DECISION_PRIORITY: dict[EnforcementDecision, int] = {
    EnforcementDecision.ALLOW: 0,
    EnforcementDecision.WARN: 1,
    EnforcementDecision.REQUIRE_APPROVAL: 2,
    EnforcementDecision.BLOCK: 3,
}


def _aggregate_decision(violations: list[PolicyViolation]) -> EnforcementDecision:
    """Return the most severe EnforcementDecision across all violations."""
    if not violations:
        return EnforcementDecision.ALLOW
    return max(violations, key=lambda v: _DECISION_PRIORITY[v.decision]).decision


# ─── Data models ──────────────────────────────────────────────────────────────


class ActionContext(BaseModel):
    """
    Everything the policy engine knows about the action under evaluation.

    The WorkflowEngine populates this from the stage implementation's
    class attributes (``action_impact``, ``high_impact_action_type``,
    ``policy_metadata``) immediately before the governance gate runs.
    """

    workflow_id: str = Field(description="ID of the running workflow")
    stage_name: str = Field(description="Name of the stage being evaluated")
    action_impact: ActionImpact = Field(
        description="Semantic impact level declared by the stage"
    )
    action_type: HighImpactActionType | None = Field(
        default=None,
        description="Well-known action category (e.g. PRODUCTION_RELEASE, SCHEMA_MIGRATION)",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Stage-level metadata for policy decisions. "
            "Populated from BaseStage.policy_metadata. "
            "Keys are policy-specific: e.g. 'security_scan_passed', "
            "'change_ticket_id', 'rollback_plan_documented'."
        ),
    )
    evaluated_at: datetime = Field(default_factory=_now)


class PolicyViolation(BaseModel):
    """
    Record of a single policy rule that fired during evaluation.

    One violation is created per policy that returns a non-ALLOW decision.
    WARN violations are recorded but do not stop execution.
    """

    policy_id: str = Field(description="Machine-readable policy identifier, e.g. 'SEC-001'")
    domain: PolicyDomain
    message: str = Field(description="Human-readable explanation of the violation")
    decision: EnforcementDecision = Field(
        description="Enforcement action recommended by this policy"
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Supporting data used to reach the decision (metadata keys, impact level, etc.)",
    )


class PolicyEvaluationRecord(BaseModel):
    """
    Aggregate result of evaluating all registered policies against one action.

    One record is created per governance gate evaluation and stored on
    ``WorkflowState.policy_evaluations``. It forms the auditable evidence
    that guardrails were checked before the action ran.
    """

    id: str = Field(default_factory=_uuid)
    workflow_id: str
    stage_name: str
    action_impact: str = Field(description="ActionImpact value (stored as string)")
    action_type: str | None = None
    violations: list[PolicyViolation] = Field(default_factory=list)
    final_decision: EnforcementDecision
    evaluated_at: datetime = Field(default_factory=_now)
    evaluated_by: str = Field(
        default="policy_engine",
        description="Identity of the component that produced this record",
    )


# ─── Policy ABC ───────────────────────────────────────────────────────────────


class Policy(ABC):
    """
    Single named guardrail evaluated against an ActionContext.

    Contract
    ────────
    • ``evaluate`` MUST be side-effect-free and deterministic.
    • ``evaluate`` MUST NOT raise — exceptions propagate to PolicyEngine,
      which treats them as BLOCK (fail-safe).
    • Return ``None`` to allow the action; return a ``PolicyViolation``
      to trigger the enforcement decision embedded in the violation.

    Class attributes to override
    ────────────────────────────
    • ``policy_id``    unique machine-readable identifier (e.g. 'SEC-001')
    • ``domain``       PolicyDomain that categorises this guardrail
    • ``description``  one-line human-readable summary

    Example::

        class NoSecretsInMetadata(Policy):
            policy_id   = "SEC-003"
            domain      = PolicyDomain.SECURITY
            description = "Reject stages that expose potential secrets in policy metadata."

            _SUSPECT_KEYS = frozenset({"password", "secret", "token", "api_key"})

            def evaluate(self, context: ActionContext) -> PolicyViolation | None:
                found = [k for k in context.metadata if k.lower() in self._SUSPECT_KEYS]
                if found:
                    return PolicyViolation(
                        policy_id=self.policy_id,
                        domain=self.domain,
                        message="Potential secrets detected in stage metadata.",
                        decision=EnforcementDecision.BLOCK,
                        evidence={"suspect_keys": found},
                    )
    """

    policy_id: str
    domain: PolicyDomain
    description: str

    @abstractmethod
    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        """Return None to allow, PolicyViolation to restrict."""
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(policy_id={self.policy_id!r})"


# ─── Policy engine ────────────────────────────────────────────────────────────


class PolicyEngine:
    """
    Evaluates all registered policies against an ActionContext and returns
    a single PolicyEvaluationRecord with the aggregate enforcement decision.

    Fail-safe behaviour
    ───────────────────
    If a policy's ``evaluate()`` raises an exception, the exception is caught
    and treated as a BLOCK violation. A broken guardrail never permits
    execution — it always prevents it.

    Usage::

        engine = PolicyEngine(policies=[
            RequireSecurityScanForRelease(),
            RequireChangeTicket(),
            RequireApprovalForProduction(),
        ])
        record = engine.evaluate(action_ctx)
        if record.final_decision == EnforcementDecision.BLOCK:
            # reject the action
    """

    def __init__(self, policies: list[Policy]) -> None:
        self.policies = policies

    def evaluate(self, context: ActionContext) -> PolicyEvaluationRecord:
        """
        Run every policy, aggregate violations, and return the evaluation record.
        """
        violations: list[PolicyViolation] = []

        for policy in self.policies:
            try:
                violation = policy.evaluate(context)
            except Exception as exc:
                # Fail-safe: treat evaluation failure as BLOCK
                violation = PolicyViolation(
                    policy_id=policy.policy_id,
                    domain=policy.domain,
                    message=(
                        f"Policy evaluation raised an unexpected exception: {exc}. "
                        "Execution is blocked as a fail-safe measure."
                    ),
                    decision=EnforcementDecision.BLOCK,
                    evidence={"error": str(exc), "error_type": type(exc).__name__},
                )

            if violation is not None:
                violations.append(violation)

        final_decision = _aggregate_decision(violations)

        return PolicyEvaluationRecord(
            workflow_id=context.workflow_id,
            stage_name=context.stage_name,
            action_impact=context.action_impact.value,
            action_type=context.action_type.value if context.action_type else None,
            violations=violations,
            final_decision=final_decision,
            evaluated_at=context.evaluated_at,
        )

    def __repr__(self) -> str:
        return f"PolicyEngine(policies={[p.policy_id for p in self.policies]})"


# ─── Built-in security policies ───────────────────────────────────────────────


class RequireSecurityScanForRelease(Policy):
    """
    SEC-001  SECURITY
    Production releases must pass a security scan before the stage executes.

    Metadata key required::
        policy_metadata = {"security_scan_passed": True}
    """

    policy_id = "SEC-001"
    domain = PolicyDomain.SECURITY
    description = (
        "Production releases require a passing security scan. "
        "Set metadata['security_scan_passed'] = True to satisfy this policy."
    )

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if context.action_type != HighImpactActionType.PRODUCTION_RELEASE:
            return None
        if context.metadata.get("security_scan_passed") is True:
            return None
        return PolicyViolation(
            policy_id=self.policy_id,
            domain=self.domain,
            message=(
                "A security scan must pass before a production release. "
                "Set policy_metadata['security_scan_passed'] = True."
            ),
            decision=EnforcementDecision.BLOCK,
            evidence={
                "action_type": context.action_type.value,
                "security_scan_passed": context.metadata.get("security_scan_passed"),
            },
        )


class ProtectPiiData(Policy):
    """
    SEC-002  SECURITY
    Stages that access PII data must carry explicit compliance sign-off.

    Metadata keys::
        policy_metadata = {"pii_data_access": True, "pii_approved": True}

    Accessing PII without approval → BLOCK.
    """

    policy_id = "SEC-002"
    domain = PolicyDomain.SECURITY
    description = (
        "Stages accessing PII data require explicit compliance approval. "
        "Set metadata['pii_approved'] = True after obtaining sign-off."
    )

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if not context.metadata.get("pii_data_access"):
            return None
        if context.metadata.get("pii_approved") is True:
            return None
        return PolicyViolation(
            policy_id=self.policy_id,
            domain=self.domain,
            message=(
                "This stage accesses PII data but has not been approved to do so. "
                "Set policy_metadata['pii_approved'] = True after obtaining compliance sign-off."
            ),
            decision=EnforcementDecision.BLOCK,
            evidence={"pii_data_access": True, "pii_approved": False},
        )


class WarnOnHighRiskAction(Policy):
    """
    SEC-003  SECURITY
    Emit a WARN-level violation for actions flagged as high-risk.

    Does NOT block execution — increases audit visibility only.

    Metadata key::
        policy_metadata = {"high_risk_action": True}
    """

    policy_id = "SEC-003"
    domain = PolicyDomain.SECURITY
    description = (
        "High-risk actions are logged with a warning to increase audit visibility. "
        "Execution is not blocked."
    )

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if not context.metadata.get("high_risk_action"):
            return None
        return PolicyViolation(
            policy_id=self.policy_id,
            domain=self.domain,
            message=(
                "This action has been flagged as high-risk. "
                "It is allowed but recorded as a warning in the audit trail."
            ),
            decision=EnforcementDecision.WARN,
            evidence={"high_risk_action": True},
        )


# ─── Built-in compliance policies ─────────────────────────────────────────────


class RequireChangeTicket(Policy):
    """
    COMP-001  COMPLIANCE
    Significant and higher-impact actions must reference a change ticket.

    Metadata key required::
        policy_metadata = {"change_ticket_id": "CHG-1234"}

    Applies to: SIGNIFICANT, HIGH_IMPACT, CRITICAL.
    ROUTINE actions are excluded.
    """

    policy_id = "COMP-001"
    domain = PolicyDomain.COMPLIANCE
    description = (
        "Significant and higher-impact actions must reference a change ticket ID. "
        "Set metadata['change_ticket_id'] to the approved ticket number."
    )

    _APPLICABLE = frozenset(
        {ActionImpact.SIGNIFICANT, ActionImpact.HIGH_IMPACT, ActionImpact.CRITICAL}
    )

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if context.action_impact not in self._APPLICABLE:
            return None
        if context.metadata.get("change_ticket_id"):
            return None
        return PolicyViolation(
            policy_id=self.policy_id,
            domain=self.domain,
            message=(
                f"A change ticket ID is required for {context.action_impact.value} actions. "
                "Set policy_metadata['change_ticket_id'] to the approved ticket number."
            ),
            decision=EnforcementDecision.BLOCK,
            evidence={
                "action_impact": context.action_impact.value,
                "change_ticket_id": context.metadata.get("change_ticket_id"),
            },
        )


class EnforceDataRetentionPolicy(Policy):
    """
    COMP-002  COMPLIANCE
    Stages that delete data must confirm compliance with the retention policy.

    Metadata key required::
        policy_metadata = {"data_deletion": True, "retention_policy_checked": True}

    Deleting data without confirmation → BLOCK.
    """

    policy_id = "COMP-002"
    domain = PolicyDomain.COMPLIANCE
    description = (
        "Data deletion actions require explicit confirmation that the retention "
        "policy has been checked. "
        "Set metadata['retention_policy_checked'] = True."
    )

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if not context.metadata.get("data_deletion"):
            return None
        if context.metadata.get("retention_policy_checked") is True:
            return None
        return PolicyViolation(
            policy_id=self.policy_id,
            domain=self.domain,
            message=(
                "This stage deletes data but has not confirmed the retention policy. "
                "Set policy_metadata['retention_policy_checked'] = True."
            ),
            decision=EnforcementDecision.BLOCK,
            evidence={
                "data_deletion": True,
                "retention_policy_checked": context.metadata.get("retention_policy_checked"),
            },
        )


# ─── Built-in change-control policies ─────────────────────────────────────────


class RequireApprovalForProduction(Policy):
    """
    CC-001  CHANGE_CONTROL
    All production releases require explicit human approval before execution.

    Returns REQUIRE_APPROVAL (not BLOCK) — the approval checkpoint handles the
    rest. If no ApprovalGateway is configured, the engine will fail the stage.
    """

    policy_id = "CC-001"
    domain = PolicyDomain.CHANGE_CONTROL
    description = (
        "All production releases require explicit human approval before execution. "
        "The approval checkpoint is enforced by the policy engine."
    )

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if context.action_type != HighImpactActionType.PRODUCTION_RELEASE:
            return None
        return PolicyViolation(
            policy_id=self.policy_id,
            domain=self.domain,
            message=(
                "Production releases require human approval before execution. "
                "An approver must review and approve the release via the approval gateway."
            ),
            decision=EnforcementDecision.REQUIRE_APPROVAL,
            evidence={"action_type": context.action_type.value},
        )


class RequireRollbackPlan(Policy):
    """
    CC-002  CHANGE_CONTROL
    HIGH_IMPACT and CRITICAL actions must document a rollback plan.

    Metadata key required::
        policy_metadata = {"rollback_plan_documented": True}
    """

    policy_id = "CC-002"
    domain = PolicyDomain.CHANGE_CONTROL
    description = (
        "High-impact and critical actions require a documented rollback plan. "
        "Set metadata['rollback_plan_documented'] = True."
    )

    _APPLICABLE = frozenset({ActionImpact.HIGH_IMPACT, ActionImpact.CRITICAL})

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if context.action_impact not in self._APPLICABLE:
            return None
        if context.metadata.get("rollback_plan_documented") is True:
            return None
        return PolicyViolation(
            policy_id=self.policy_id,
            domain=self.domain,
            message=(
                f"A documented rollback plan is required for "
                f"{context.action_impact.value} actions. "
                "Set policy_metadata['rollback_plan_documented'] = True."
            ),
            decision=EnforcementDecision.BLOCK,
            evidence={
                "action_impact": context.action_impact.value,
                "rollback_plan_documented": context.metadata.get("rollback_plan_documented"),
            },
        )


class FreezeWindowPolicy(Policy):
    """
    CC-003  CHANGE_CONTROL
    Blocks changes that fall inside a configured freeze window.

    Freeze windows are half-open intervals: [start, end].
    By default, applies to SIGNIFICANT and higher-impact actions.

    Constructor args
    ────────────────
    freeze_windows  list of (start_utc, end_utc) tuples.
    minimum_impact  Only actions at or above this level are restricted.
                    Default: SIGNIFICANT.
    now_fn          Callable[[], datetime] used to get the current time.
                    Inject a fixed clock in tests for determinism.

    Example::

        FreezeWindowPolicy(
            freeze_windows=[
                (datetime(2026, 12, 25, tzinfo=timezone.utc),
                 datetime(2027,  1,  5, tzinfo=timezone.utc)),
            ]
        )
    """

    policy_id = "CC-003"
    domain = PolicyDomain.CHANGE_CONTROL
    description = "Blocks changes during configured freeze windows."

    _IMPACT_ORDER = [
        ActionImpact.ROUTINE,
        ActionImpact.SIGNIFICANT,
        ActionImpact.HIGH_IMPACT,
        ActionImpact.CRITICAL,
    ]

    def __init__(
        self,
        freeze_windows: list[tuple[datetime, datetime]],
        minimum_impact: ActionImpact = ActionImpact.SIGNIFICANT,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.freeze_windows = freeze_windows
        self.minimum_impact = minimum_impact
        self._now_fn: Callable[[], datetime] = now_fn or (
            lambda: datetime.now(tz=timezone.utc)
        )

    def evaluate(self, context: ActionContext) -> PolicyViolation | None:
        if self._IMPACT_ORDER.index(context.action_impact) < self._IMPACT_ORDER.index(
            self.minimum_impact
        ):
            return None  # below the minimum impact threshold

        now = self._now_fn()
        for start, end in self.freeze_windows:
            if start <= now <= end:
                return PolicyViolation(
                    policy_id=self.policy_id,
                    domain=self.domain,
                    message=(
                        f"Changes are frozen from {start.isoformat()} to {end.isoformat()}. "
                        "This action is blocked until the freeze window ends."
                    ),
                    decision=EnforcementDecision.BLOCK,
                    evidence={
                        "freeze_start": start.isoformat(),
                        "freeze_end": end.isoformat(),
                        "now": now.isoformat(),
                    },
                )
        return None
