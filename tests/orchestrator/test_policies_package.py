"""Smoke tests for orchestrator.policies re-exports and package docs."""
from __future__ import annotations

import orchestrator.policies as policies
import orchestrator.stages as stages
from orchestrator.core.governance import PolicyEngine


def test_policies_reexports_policy_engine() -> None:
    assert policies.PolicyEngine is PolicyEngine
    engine = policies.PolicyEngine(policies=[policies.RequireSecurityScanForRelease()])
    assert engine is not None


def test_policies_reexports_builtin_policy_classes() -> None:
    assert policies.RequireSecurityScanForRelease is not None
    assert policies.ProtectPiiData is not None
    assert policies.RequireChangeTicket is not None
    assert policies.RequireRollbackPlan is not None


def test_stages_package_documents_scenario_location() -> None:
    assert "scenarios" in (stages.__doc__ or "").lower()
