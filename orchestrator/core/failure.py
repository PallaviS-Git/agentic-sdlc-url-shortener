"""
Failure-handling domain types for the Agentic SDLC orchestration layer.

This module defines how the WorkflowEngine classifies, retries, rolls back,
and safe-stops in response to stage failures.

  Failure classification
  ──────────────────────
    TRANSIENT   → retry (within max_attempts)
    PERMANENT   → stop immediately, attempt fallback/rollback
    CRITICAL    → safe-stop the whole workflow, preserve state for investigation

  Recovery decisions (one per attempt)
  ─────────────────────────────────────
    RETRY          → another attempt will follow
    FALLBACK       → retries exhausted; falling back to preset or skip
    ROLLBACK       → calling stage.rollback(); stage will be ROLLED_BACK
    SAFE_STOP      → halting workflow immediately; no rollback
    FAIL_IMMEDIATE → stopping without further recovery

  Retry policy (per-stage, falls back to engine default)
  ────────────────────────────────────────────────────────
    RetryPolicy.max_attempts ≥ 1 — infinite retries are never allowed.
    Exception class names drive classification (exact type OR base class).

Import chain: stdlib + pydantic only. No orchestrator imports here.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ─── Failure classification ───────────────────────────────────────────────────


class FailureClassification(str, enum.Enum):
    """
    How the engine categorises a stage execution failure.

    The classification drives the recovery decision at each attempt.
    """

    TRANSIENT = "transient"
    """
    Temporary condition expected to resolve on retry.

    Examples: network timeout, database connection pool exhausted,
    rate-limit exceeded, lock contention.
    """

    PERMANENT = "permanent"
    """
    Failure will not resolve by retrying.

    Examples: invalid input data, schema validation error,
    business-rule violation, missing required dependency.
    Retries are skipped; the engine moves directly to fallback/rollback.
    """

    CRITICAL = "critical"
    """
    Failure so severe that continuing would cause harm.

    Examples: security breach detected, data-corruption risk,
    production credentials leak, invariant violation.
    Triggers immediate SAFE_STOP — no retries, no rollback.
    The workflow is frozen for operator investigation.
    """


# ─── Recovery decisions ───────────────────────────────────────────────────────


class RecoveryDecision(str, enum.Enum):
    """
    What the engine decided to do after classifying a failure.

    One RecoveryDecision is stored in each StageAttemptRecord so that
    the full failure history is traceable in WorkflowLineage.
    """

    RETRY = "retry"
    """Another execution attempt will follow."""

    FALLBACK = "fallback"
    """Retries exhausted; engine will apply the configured fallback action."""

    ROLLBACK = "rollback"
    """Engine will call stage.rollback(); stage will be ROLLED_BACK."""

    SAFE_STOP = "safe_stop"
    """Workflow halted immediately; state preserved for investigation."""

    FAIL_IMMEDIATE = "fail_immediate"
    """
    Stopping without further recovery.

    Used for PERMANENT failures and for TRANSIENT failures after
    max_attempts is reached without triggering a fallback/rollback.
    """


# ─── Fallback behavior ────────────────────────────────────────────────────────


class FallbackBehavior(str, enum.Enum):
    """
    What the engine does when retries are exhausted.

    The active RetryPolicy selects one of these strategies.
    """

    FAIL = "fail"
    """Default: fail the stage; dependents are BLOCKED."""

    SKIP = "skip"
    """
    Mark the stage as SKIPPED and continue the workflow.

    Downstream stages receive an empty output snapshot from this stage.
    Use only for optional stages whose absence does not break the pipeline.
    """

    USE_PRESET = "use_preset"
    """
    Use the RetryPolicy.fallback_output dict as the stage's output and
    mark the stage COMPLETED (with StageContext.fallback_used=True).

    Downstream stages see the preset values as if the stage succeeded.
    Use when a safe default output is preferable to pipeline failure.
    """


# ─── Retry policy ─────────────────────────────────────────────────────────────


class RetryPolicy(BaseModel):
    """
    Per-stage retry and recovery configuration.

    Declare as a class attribute on a BaseStage subclass to override the
    engine-level default::

        class ReleaseStage(BaseStage):
            retry_policy = RetryPolicy(
                max_attempts=3,
                non_retryable_error_types=["ValueError"],
                rollback_on_failure=True,
            )

    The engine resolves the effective policy at run time:
      1. Use ``stage_impl.retry_policy`` if set.
      2. Fall back to ``WorkflowEngine.default_retry_policy``.
    """

    max_attempts: int = Field(
        default=1,
        ge=1,
        le=10,
        description=(
            "Total number of attempts (1 = no retries). "
            "Infinite retries are never allowed."
        ),
    )
    non_retryable_error_types: list[str] = Field(
        default_factory=list,
        description=(
            "Exception class names (exact or base class) that bypass retries "
            "and classify as PERMANENT immediately."
        ),
    )
    safe_stop_error_types: list[str] = Field(
        default_factory=list,
        description=(
            "Exception class names that trigger a workflow safe-stop "
            "(classification=CRITICAL, no retry, no rollback)."
        ),
    )
    exit_gate_failure_retryable: bool = Field(
        default=True,
        description=(
            "When True a failed exit gate triggers a retry of execute+exit_gate "
            "rather than immediate failure. "
            "When False, exit gate failure = PERMANENT."
        ),
    )
    fallback_behavior: FallbackBehavior = Field(
        default=FallbackBehavior.FAIL,
        description="Strategy when all retry attempts are exhausted.",
    )
    fallback_output: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Output data injected as the stage's result when "
            "fallback_behavior=USE_PRESET and retries are exhausted."
        ),
    )
    rollback_on_failure: bool = Field(
        default=False,
        description=(
            "When True, stage.rollback() is called after retries are exhausted "
            "(unless a fallback was applied or safe-stop was triggered)."
        ),
    )

    def classify(self, exc: Exception) -> FailureClassification:
        """
        Classify an exception according to this policy.

        Checks the exception's MRO against ``safe_stop_error_types``
        then ``non_retryable_error_types``. Anything else is TRANSIENT.
        """
        mro_names = {cls.__name__ for cls in type(exc).__mro__}
        if mro_names & set(self.safe_stop_error_types):
            return FailureClassification.CRITICAL
        if mro_names & set(self.non_retryable_error_types):
            return FailureClassification.PERMANENT
        return FailureClassification.TRANSIENT


# ─── Attempt record ───────────────────────────────────────────────────────────


class StageAttemptRecord(BaseModel):
    """
    Immutable record of a single execution attempt within a stage.

    One record is appended to ``StageContext.attempt_records`` for every
    attempt that fails. Successful attempts are recorded indirectly by
    the absence of additional records after the last (successful) attempt.

    This is the primary evidence for the requirement:
      "Recovery decisions must be traceable."
    """

    attempt: int = Field(
        description="0-indexed attempt number within this stage's retry cycle"
    )
    error: str = Field(description="str(exception) from the failed attempt")
    error_type: str = Field(
        default="",
        description="Exception class name (e.g. 'ValueError', 'TimeoutError')",
    )
    classification: FailureClassification
    recovery_decision: RecoveryDecision | None = Field(
        default=None,
        description="What the engine decided to do after this attempt",
    )
    timestamp: datetime = Field(default_factory=_now)


# ─── Default policy ───────────────────────────────────────────────────────────

DEFAULT_RETRY_POLICY = RetryPolicy(
    max_attempts=1,
    rollback_on_failure=False,
    fallback_behavior=FallbackBehavior.FAIL,
)
"""
Conservative engine-wide default: single attempt, no retries, no rollback.

Stages opt-in to retries by overriding ``retry_policy`` on the class.
This ensures existing behaviour is preserved unless explicitly changed.
"""
