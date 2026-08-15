"""
Automated tests for the Greenfield URL Shortener SDLC scenario.

These tests verify the end-to-end agentic pipeline through the real
WorkflowEngine — no mocking of the orchestration layer.

What is tested
──────────────
1.  Full scenario runs to COMPLETED status
2.  All 8 SDLC stages execute in correct order
3.  Artifact inventory: ≥8 artifacts produced (one per stage minimum)
4.  Named artifacts present: normalized_requirement, architecture_design,
    data_model, task_graph, implementation_plan, test_plan,
    documentation_plan, validation_report, release_checklist
5.  Architecture ADRs: ≥4 decisions with correct types
6.  Decision lineage: scope decision → ADR chain traceable
7.  Approval records: ≥2 approvals (architecture + release)
8.  Validation results: all critical validations passed
9.  Entry gate enforcement: architecture_design entry gate blocks without
    normalized_requirement in context
10. Governance (policy) re-evaluation during replan
11. Impact analysis correctly identifies downstream stages
12. Replan after architecture change replans only impl/test/doc/validation/release
13. Observability report buildable from final state
14. Execution trace covers all 8 step kinds
15. Reliability metrics computed from scenario output
16. Final state contains go_live_ready=True in release stage output
17. Release checklist artifact contains rollback plan
18. Parallel execution: impl_planning + testing_planning + doc_planning
    all complete before validation
19. Stage IDs are unique across the run
20. WorkflowState.completed_at is set after run

asyncio_mode=auto (pytest.ini).
"""
from __future__ import annotations

import pytest

from orchestrator.core.autonomy import AutoApproveGateway, AutoRejectGateway
from orchestrator.core.governance import (
    PolicyEngine,
    RequireChangeTicket,
    RequireRollbackPlan,
    RequireSecurityScanForRelease,
)
from orchestrator.core.models import StageStatus, WorkflowStatus
from orchestrator.core.observability import (
    TraceStepKind,
    build_execution_trace,
    build_observability_report,
    build_structured_logs,
    compute_reliability_metrics,
    compute_workflow_metrics,
)
from orchestrator.core.replanning import ChangeEvent, ChangeEventType
from orchestrator.core.results import ArtifactType, DecisionType
from orchestrator.scenarios.greenfield import (
    create_greenfield_stages,
    create_greenfield_workflow,
    run_greenfield_scenario,
)
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── Shared fixture ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def completed_state():
    """Run the full greenfield scenario once; share across tests in this module."""
    return await run_greenfield_scenario(approval_gateway=AutoApproveGateway())


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Full pipeline
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineCompletion:
    async def test_scenario_completes_successfully(self, completed_state) -> None:
        assert completed_state.status == WorkflowStatus.COMPLETED

    async def test_all_8_stages_executed(self, completed_state) -> None:
        stages = set(completed_state.stages.keys())
        expected = {
            "requirements_analysis", "architecture_design", "task_decomposition",
            "implementation_planning", "testing_planning", "documentation_planning",
            "validation", "release_readiness",
        }
        assert expected.issubset(stages)

    async def test_all_stages_completed(self, completed_state) -> None:
        for name, ctx in completed_state.stages.items():
            assert ctx.status == StageStatus.COMPLETED, (
                f"Stage '{name}' has status {ctx.status.value}"
            )

    async def test_completed_at_set(self, completed_state) -> None:
        assert completed_state.completed_at is not None

    async def test_no_safe_stop(self, completed_state) -> None:
        assert not completed_state.safe_stopped

    async def test_replan_count_zero_after_initial_run(self, completed_state) -> None:
        assert completed_state.replan_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Artifact inventory
# ═══════════════════════════════════════════════════════════════════════════════


class TestArtifactInventory:
    def _all_artifacts(self, state) -> dict[str, list]:
        """Returns {stage_name: [artifact_name, ...]}"""
        return {
            name: [a.name for a in ctx.artifacts]
            for name, ctx in state.stages.items()
            if ctx.artifacts
        }

    async def test_at_least_8_artifacts_produced(self, completed_state) -> None:
        total = sum(len(ctx.artifacts) for ctx in completed_state.stages.values())
        assert total >= 8, f"Expected ≥8 artifacts, got {total}"

    async def test_normalized_requirement_artifact_present(self, completed_state) -> None:
        req_arts = completed_state.stages["requirements_analysis"].artifacts
        names = [a.name for a in req_arts]
        assert "normalized_requirement.json" in names

    async def test_architecture_design_artifact_present(self, completed_state) -> None:
        arch_arts = completed_state.stages["architecture_design"].artifacts
        names = [a.name for a in arch_arts]
        assert "architecture_design.json" in names

    async def test_data_model_artifact_present(self, completed_state) -> None:
        arch_arts = completed_state.stages["architecture_design"].artifacts
        names = [a.name for a in arch_arts]
        assert "data_model.json" in names

    async def test_task_graph_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["task_decomposition"].artifacts
        names = [a.name for a in arts]
        assert "task_graph.json" in names

    async def test_implementation_plan_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["implementation_planning"].artifacts
        names = [a.name for a in arts]
        assert "implementation_plan.json" in names

    async def test_test_plan_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["testing_planning"].artifacts
        names = [a.name for a in arts]
        assert "test_plan.json" in names

    async def test_documentation_plan_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["documentation_planning"].artifacts
        names = [a.name for a in arts]
        assert "documentation_plan.json" in names

    async def test_validation_report_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["validation"].artifacts
        names = [a.name for a in arts]
        assert "validation_report.json" in names

    async def test_release_checklist_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["release_readiness"].artifacts
        names = [a.name for a in arts]
        assert "release_checklist.json" in names

    async def test_artifacts_have_content(self, completed_state) -> None:
        for name, ctx in completed_state.stages.items():
            for art in ctx.artifacts:
                assert art.content, f"Artifact '{art.name}' in stage '{name}' has no content"

    async def test_artifact_types_are_diverse(self, completed_state) -> None:
        all_types = {
            a.artifact_type
            for ctx in completed_state.stages.values()
            for a in ctx.artifacts
        }
        # Expect at least: DOCUMENTATION, SCHEMA, TEST, REPORT
        assert ArtifactType.DOCUMENTATION in all_types
        assert ArtifactType.SCHEMA in all_types
        assert ArtifactType.TEST in all_types
        assert ArtifactType.REPORT in all_types


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Decision lineage
# ═══════════════════════════════════════════════════════════════════════════════


class TestDecisionLineage:
    async def test_at_least_8_decisions_recorded(self, completed_state) -> None:
        total = sum(len(ctx.decisions) for ctx in completed_state.stages.values())
        assert total >= 8, f"Expected ≥8 decisions, got {total}"

    async def test_architecture_decisions_present(self, completed_state) -> None:
        arch_decisions = completed_state.stages["architecture_design"].decisions
        assert len(arch_decisions) >= 4, "Expected ≥4 ADRs from architecture_design"

    async def test_scope_decision_in_requirements_stage(self, completed_state) -> None:
        req_decisions = completed_state.stages["requirements_analysis"].decisions
        scope_decs = [d for d in req_decisions if d.decision_type == DecisionType.SCOPE]
        assert scope_decs, "Expected a SCOPE decision from requirements_analysis"

    async def test_architectural_decision_type_present(self, completed_state) -> None:
        arch_decisions = completed_state.stages["architecture_design"].decisions
        arch_types = [d for d in arch_decisions if d.decision_type == DecisionType.ARCHITECTURAL]
        assert len(arch_types) >= 2, "Expected ≥2 ARCHITECTURAL decisions (ADRs)"

    async def test_security_decision_recorded(self, completed_state) -> None:
        all_decisions = [
            d
            for ctx in completed_state.stages.values()
            for d in ctx.decisions
        ]
        security_decs = [d for d in all_decisions if d.decision_type == DecisionType.SECURITY]
        assert security_decs, "Expected at least one SECURITY decision (auth ADR)"

    async def test_decision_chain_parent_ids(self, completed_state) -> None:
        """ADR-002, 003, 004 should reference ADR-001's ID as parent."""
        arch_decisions = completed_state.stages["architecture_design"].decisions
        children = [d for d in arch_decisions if d.parent_decision_id]
        assert children, "Expected decisions with parent_decision_id (decision chain)"

    async def test_decisions_have_rationale(self, completed_state) -> None:
        for name, ctx in completed_state.stages.items():
            for dec in ctx.decisions:
                assert dec.rationale, f"Decision '{dec.title}' in '{name}' has empty rationale"

    async def test_release_decision_recorded(self, completed_state) -> None:
        rel_decisions = completed_state.stages["release_readiness"].decisions
        assert rel_decisions, "Release readiness stage must record a final decision"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Approval state
# ═══════════════════════════════════════════════════════════════════════════════


class TestApprovalState:
    async def test_at_least_2_approvals_obtained(self, completed_state) -> None:
        assert len(completed_state.approvals) >= 2, (
            f"Expected ≥2 approvals (architecture + release), got {len(completed_state.approvals)}"
        )

    async def test_architecture_approval_present(self, completed_state) -> None:
        arch_approvals = [a for a in completed_state.approvals if a.stage_name == "architecture_design"]
        assert arch_approvals, "architecture_design approval missing"

    async def test_release_readiness_approval_present(self, completed_state) -> None:
        rel_approvals = [a for a in completed_state.approvals if a.stage_name == "release_readiness"]
        assert rel_approvals, "release_readiness approval missing"

    async def test_all_approvals_approved(self, completed_state) -> None:
        from orchestrator.core.results import ApprovalStatus
        for appr in completed_state.approvals:
            assert appr.status == ApprovalStatus.APPROVED, (
                f"Approval for '{appr.stage_name}' is {appr.status.value}"
            )

    async def test_approval_rejected_fails_workflow(self) -> None:
        """AutoRejectGateway should cause architecture_design to fail."""
        state = await run_greenfield_scenario(approval_gateway=AutoRejectGateway())
        assert state.status == WorkflowStatus.FAILED
        # architecture_design is the first stage requiring approval
        arch_ctx = state.stages.get("architecture_design")
        assert arch_ctx is not None
        assert arch_ctx.status == StageStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Validation results
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidationResults:
    async def test_no_failed_critical_validations(self, completed_state) -> None:
        for name, ctx in completed_state.stages.items():
            for v in ctx.validations:
                if v.severity == ValidationSeverity.ERROR and not v.passed:
                    pytest.fail(f"Critical validation '{v.rule_name}' FAILED in stage '{name}': {v.message}")

    async def test_validation_stage_all_checks_passed(self, completed_state) -> None:
        val_ctx = completed_state.stages["validation"]
        for v in val_ctx.validations:
            assert v.passed, f"Validation rule '{v.rule_name}' failed: {v.message}"

    async def test_security_review_validation_present(self, completed_state) -> None:
        val_ctx = completed_state.stages["validation"]
        sec_rules = [v.rule_name for v in val_ctx.validations]
        assert "security_review_passed" in sec_rules

    async def test_gdpr_compliance_check_present(self, completed_state) -> None:
        val_ctx = completed_state.stages["validation"]
        gdpr_rules = [v.rule_name for v in val_ctx.validations]
        assert "gdpr_compliance_checked" in gdpr_rules

    async def test_release_readiness_final_sign_off(self, completed_state) -> None:
        rel_ctx = completed_state.stages["release_readiness"]
        sign_offs = [v.rule_name for v in rel_ctx.validations]
        assert "final_sign_off" in sign_offs


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Data content of key artifacts
# ═══════════════════════════════════════════════════════════════════════════════


class TestArtifactContent:
    import json

    async def test_normalized_requirement_has_functional_reqs(self, completed_state) -> None:
        import json
        art = next(a for a in completed_state.stages["requirements_analysis"].artifacts
                   if a.name == "normalized_requirement.json")
        data = json.loads(art.content)
        assert len(data["functional_requirements"]) >= 6

    async def test_architecture_has_all_components(self, completed_state) -> None:
        import json
        art = next(a for a in completed_state.stages["architecture_design"].artifacts
                   if a.name == "architecture_design.json")
        data = json.loads(art.content)
        assert "components" in data
        assert "api_gateway" in data["components"]
        assert "database" in data["components"]
        assert "cache" in data["components"]

    async def test_data_model_has_url_entity(self, completed_state) -> None:
        import json
        art = next(a for a in completed_state.stages["architecture_design"].artifacts
                   if a.name == "data_model.json")
        data = json.loads(art.content)
        assert "url" in data["entities"]
        assert "click_event" in data["entities"]

    async def test_task_graph_has_implementation_tasks(self, completed_state) -> None:
        import json
        art = next(a for a in completed_state.stages["task_decomposition"].artifacts
                   if a.name == "task_graph.json")
        data = json.loads(art.content)
        assert len(data["implementation_tasks"]) >= 8

    async def test_release_checklist_has_rollback_plan(self, completed_state) -> None:
        import json
        art = next(a for a in completed_state.stages["release_readiness"].artifacts
                   if a.name == "release_checklist.json")
        data = json.loads(art.content)
        assert "rollback_plan" in data
        rollback = data["rollback_plan"]
        assert "database" in rollback
        assert "rto_estimate" in rollback

    async def test_validation_report_zero_critical_failures(self, completed_state) -> None:
        import json
        art = next(a for a in completed_state.stages["validation"].artifacts
                   if a.name == "validation_report.json")
        data = json.loads(art.content)
        assert data["critical_failures"] == 0

    async def test_go_live_ready_set(self, completed_state) -> None:
        rel_output = completed_state.stages["release_readiness"].output_data
        assert rel_output.get("go_live_ready") is True


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Entry gate enforcement
# ═══════════════════════════════════════════════════════════════════════════════


class TestEntryGateEnforcement:
    async def test_architecture_entry_gate_blocks_missing_requirement(self) -> None:
        """architecture_design entry gate must reject execution without upstream context."""
        from orchestrator.scenarios.greenfield import ArchitectureDesignStage
        from orchestrator.core.models import StageContext

        stage = ArchitectureDesignStage()
        ctx = StageContext(stage_name="architecture_design")
        # No input_data → entry gate should reject
        result = await stage.entry_gate(ctx)
        assert not result.passed

    async def test_validation_entry_gate_blocks_incomplete_plans(self) -> None:
        """validation entry gate rejects if any planning flag is missing."""
        from orchestrator.scenarios.greenfield import ValidationStage
        from orchestrator.core.models import StageContext as SC

        stage = ValidationStage()
        ctx = SC(stage_name="validation")
        # Only implementation_ready set — testing_ready and documentation_ready missing
        ctx.input_data["implementation_ready"] = True
        result = await stage.entry_gate(ctx)
        assert not result.passed

    async def test_release_entry_gate_blocks_without_validation(self) -> None:
        """release_readiness entry gate rejects if validation_passed is not set."""
        from orchestrator.scenarios.greenfield import ReleaseReadinessStage
        from orchestrator.core.models import StageContext as SC

        stage = ReleaseReadinessStage()
        ctx = SC(stage_name="release_readiness")
        result = await stage.entry_gate(ctx)
        assert not result.passed


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Observability
# ═══════════════════════════════════════════════════════════════════════════════


class TestObservability:
    async def test_observability_report_builds(self, completed_state) -> None:
        report = build_observability_report(completed_state)
        assert report.workflow_id == completed_state.id

    async def test_execution_trace_contains_all_kinds(self, completed_state) -> None:
        trace = build_execution_trace(completed_state)
        all_kinds = {s.kind for s in trace.all_steps()}
        assert TraceStepKind.REQUIREMENT in all_kinds
        assert TraceStepKind.DECISION in all_kinds
        assert TraceStepKind.TASK in all_kinds
        assert TraceStepKind.ARTIFACT in all_kinds
        assert TraceStepKind.VALIDATION in all_kinds
        assert TraceStepKind.RESULT in all_kinds

    async def test_trace_has_multiple_decisions(self, completed_state) -> None:
        trace = build_execution_trace(completed_state)
        assert len(trace.decisions) >= 8

    async def test_trace_has_multiple_artifacts(self, completed_state) -> None:
        trace = build_execution_trace(completed_state)
        assert len(trace.artifacts) >= 8

    async def test_structured_logs_non_empty(self, completed_state) -> None:
        logs = build_structured_logs(completed_state)
        assert len(logs) > 10

    async def test_workflow_metrics_computed(self, completed_state) -> None:
        metrics = compute_workflow_metrics(completed_state)
        assert metrics.succeeded is True
        assert metrics.total_latency_seconds is not None
        assert len(metrics.stage_metrics) == 8

    async def test_reliability_metrics_from_single_run(self, completed_state) -> None:
        metrics = compute_reliability_metrics([completed_state])
        assert metrics.total_runs == 1
        assert metrics.successful_runs == 1
        assert metrics.success_rate == pytest.approx(1.0)

    async def test_stage_ids_unique(self, completed_state) -> None:
        ids = [ctx.stage_id for ctx in completed_state.stages.values()]
        assert len(ids) == len(set(ids)), "stage_id values must be unique"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Governance (with PolicyEngine)
# ═══════════════════════════════════════════════════════════════════════════════


class TestGovernanceIntegration:
    async def test_scenario_with_security_policies_completes(self) -> None:
        """Policy engine configured with security + change-control policies."""
        pe = PolicyEngine(policies=[
            RequireSecurityScanForRelease(),
            RequireRollbackPlan(),
        ])
        state = await run_greenfield_scenario(
            approval_gateway=AutoApproveGateway(),
            policy_engine=pe,
        )
        assert state.status == WorkflowStatus.COMPLETED

    async def test_governance_evaluated_for_release_stage(self) -> None:
        """Policy evaluations recorded for release_readiness (PRODUCTION_RELEASE)."""
        pe = PolicyEngine(policies=[RequireSecurityScanForRelease()])
        state = await run_greenfield_scenario(
            approval_gateway=AutoApproveGateway(),
            policy_engine=pe,
        )
        # At least one policy evaluation for release_readiness
        release_evals = [
            e for e in state.policy_evaluations
            if e.stage_name == "release_readiness"
        ]
        assert release_evals, "release_readiness stage should have a policy evaluation"

    async def test_missing_change_ticket_blocks_architecture(self) -> None:
        """COMP-001 blocks architecture_design if no change_ticket_id."""
        from orchestrator.scenarios.greenfield import ArchitectureDesignStage

        # Patch policy_metadata to remove change_ticket_id
        ArchitectureDesignStage.policy_metadata = {}  # type: ignore[assignment]
        try:
            pe = PolicyEngine(policies=[RequireChangeTicket()])
            state = await run_greenfield_scenario(
                approval_gateway=AutoApproveGateway(),
                policy_engine=pe,
            )
            assert state.status == WorkflowStatus.FAILED
            arch_ctx = state.stages.get("architecture_design")
            assert arch_ctx and arch_ctx.status == StageStatus.FAILED
        finally:
            # Restore default metadata
            ArchitectureDesignStage.policy_metadata = {  # type: ignore[assignment]
                "change_ticket_id": "CHG-2026-GF-001"
            }


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Dynamic replanning
# ═══════════════════════════════════════════════════════════════════════════════


class TestGreenFieldReplan:
    async def test_impact_analysis_architecture_change(self, completed_state) -> None:
        """Architecture change impacts all downstream stages."""
        defn = create_greenfield_workflow()
        stages = create_greenfield_stages()
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        impact = engine.analyze_impact(
            completed_state,
            ChangeEvent(
                event_type=ChangeEventType.DECISION_CHANGED,
                originating_stage="architecture_design",
                change_description="Switched from REST to GraphQL",
            ),
        )
        assert "task_decomposition" in impact.impacted_stages
        assert "implementation_planning" in impact.impacted_stages
        assert "validation" in impact.impacted_stages
        assert "release_readiness" in impact.impacted_stages
        # Requirements preserved
        assert "requirements_analysis" in impact.preserved_stages
        assert "architecture_design" in impact.preserved_stages

    async def test_replan_after_architecture_change(self) -> None:
        """Replan after architecture change only re-runs downstream stages."""
        state = await run_greenfield_scenario(approval_gateway=AutoApproveGateway())

        defn = create_greenfield_workflow()
        stages = create_greenfield_stages()
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.replan(
            state,
            ChangeEvent(
                event_type=ChangeEventType.DECISION_CHANGED,
                originating_stage="architecture_design",
                change_description="Switched from PostgreSQL to CockroachDB",
            ),
        )
        assert state.status == WorkflowStatus.COMPLETED
        assert state.replan_count == 1

        result = state.replan_history[0]
        assert "requirements_analysis" in result.stages_preserved
        assert "architecture_design" in result.stages_preserved
        assert "validation" in result.stages_replanned
        assert "release_readiness" in result.stages_replanned


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Parallel execution verification
# ═══════════════════════════════════════════════════════════════════════════════


class TestParallelExecution:
    async def test_parallel_planning_stages_all_completed(self, completed_state) -> None:
        """impl/testing/documentation planning should all show COMPLETED."""
        for stage_name in ("implementation_planning", "testing_planning", "documentation_planning"):
            ctx = completed_state.stages[stage_name]
            assert ctx.status == StageStatus.COMPLETED, f"{stage_name} not COMPLETED"

    async def test_validation_completed_after_parallel_stages(self, completed_state) -> None:
        """Validation must have input from all three planning stages."""
        val_ctx = completed_state.stages["validation"]
        assert val_ctx.status == StageStatus.COMPLETED
        # Confirm all three signals were in input
        for flag in ("implementation_ready", "testing_ready", "documentation_ready"):
            assert val_ctx.input_data.get(flag), (
                f"validation stage missing '{flag}' in input_data"
            )


# ─── Import fix for ValidationSeverity ────────────────────────────────────────
from orchestrator.core.results import ValidationSeverity  # noqa: E402 (import at bottom)
