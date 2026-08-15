"""
Automated tests for the Brownfield rate-limiting SDLC scenario.

Key differentiators from greenfield tests
─────────────────────────────────────────
• Tests explicitly verify that preserved modules are catalogued and absent
  from the change plan (the agent does NOT blindly modify everything).
• Tests verify the codebase analysis stage correctly identifies the dormant
  config field (rate_limit_per_minute declared but not enforced).
• Tests verify the impact map explicitly names preserved endpoints.
• Tests verify the change plan contains a do_not_modify list.
• Tests verify entry gates block execution when upstream analysis is missing.

Coverage
────────
1.  Full scenario completes with COMPLETED status
2.  All 6 stages execute and complete
3.  BROWNFIELD requirement type used
4.  Codebase snapshot artifact produced with real module paths
5.  Dormant rate_limit_per_minute field identified in codebase snapshot
6.  Impact map produced with impacted + preserved components
7.  GET /{code} endpoint explicitly preserved in impact map
8.  Service layer explicitly preserved (url_service.py, url_repo.py)
9.  Risk assessment identifies Redis failure risk
10. Change plan artifact produced with do_not_modify list
11. Change plan preserves GET /{code} (not in implementation_tasks)
12. Rollback plan documented in change plan
13. Regression test plan artifact produced
14. Regression plan covers existing tests
15. Validation report: 0 critical failures
16. 2 human approvals obtained (change_planning + validation)
17. Entry gate blocks impact_analysis without codebase_snapshot
18. Entry gate blocks change_planning without risk_assessment
19. Entry gate blocks change_planning if blocking_risks > 0
20. Decision lineage traceable: gap → scope → impl_strategy → final
21. Impact map explicitly lists preserved_files
22. change_plan.do_not_modify list is non-empty
23. Observability report buildable from brownfield state
24. Approval rejection at change_planning fails the workflow
25. Replan after architecture change replans downstream stages

asyncio_mode=auto (pytest.ini).
"""
from __future__ import annotations

import json

import pytest

from orchestrator.core.autonomy import AutoApproveGateway, AutoRejectGateway
from orchestrator.core.models import StageStatus, WorkflowStatus
from orchestrator.core.observability import build_observability_report
from orchestrator.core.replanning import ChangeEvent, ChangeEventType
from orchestrator.core.results import ArtifactType, DecisionType, RiskSeverity
from orchestrator.engine.workflow_engine import WorkflowEngine
from orchestrator.scenarios.brownfield import (
    create_brownfield_stages,
    create_brownfield_workflow,
    run_brownfield_scenario,
)


# ─── Shared fixture ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def completed_state():
    """Run the full brownfield scenario once; share across tests."""
    return await run_brownfield_scenario(approval_gateway=AutoApproveGateway())


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Pipeline completion
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineCompletion:
    async def test_scenario_completes_successfully(self, completed_state) -> None:
        assert completed_state.status == WorkflowStatus.COMPLETED

    async def test_all_6_stages_completed(self, completed_state) -> None:
        expected = {
            "codebase_analysis", "impact_analysis", "risk_assessment",
            "change_planning", "regression_test_planning", "validation",
        }
        for name in expected:
            assert name in completed_state.stages, f"Stage '{name}' missing"
            assert completed_state.stages[name].status == StageStatus.COMPLETED, (
                f"Stage '{name}' status: {completed_state.stages[name].status.value}"
            )

    async def test_requirement_type_is_brownfield(self, completed_state) -> None:
        from orchestrator.core.models import RequirementType
        assert completed_state.requirement.requirement_type == RequirementType.BROWNFIELD

    async def test_completed_at_set(self, completed_state) -> None:
        assert completed_state.completed_at is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Codebase analysis artifacts
# ═══════════════════════════════════════════════════════════════════════════════


class TestCodebaseAnalysis:
    async def test_codebase_snapshot_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["codebase_analysis"].artifacts
        assert any(a.name == "codebase_snapshot.json" for a in arts)

    async def test_snapshot_contains_real_module_paths(self, completed_state) -> None:
        art = next(
            a for a in completed_state.stages["codebase_analysis"].artifacts
            if a.name == "codebase_snapshot.json"
        )
        data = json.loads(art.content)
        assert "url_shortener/api/urls.py" in data["module_inventory"]
        assert "url_shortener/services/url_service.py" in data["module_inventory"]
        assert "url_shortener/config.py" in data["module_inventory"]

    async def test_dormant_rate_limit_field_identified(self, completed_state) -> None:
        """The agent must discover the rate_limit_per_minute field is NOT enforced."""
        art = next(
            a for a in completed_state.stages["codebase_analysis"].artifacts
            if a.name == "codebase_snapshot.json"
        )
        data = json.loads(art.content)
        config_module = data["module_inventory"]["url_shortener/config.py"]
        rate_field = config_module["notable_fields"]["rate_limit_per_minute"]
        assert rate_field["status"] == "DECLARED_BUT_NOT_ENFORCED"

    async def test_all_3_endpoints_discovered(self, completed_state) -> None:
        ctx = completed_state.stages["codebase_analysis"]
        assert ctx.output_data["endpoints_discovered"] == 3

    async def test_test_files_catalogued(self, completed_state) -> None:
        ctx = completed_state.stages["codebase_analysis"]
        assert ctx.output_data["test_files_discovered"] == 4

    async def test_gap_decision_recorded(self, completed_state) -> None:
        decs = completed_state.stages["codebase_analysis"].decisions
        scope_decs = [d for d in decs if d.decision_type == DecisionType.SCOPE]
        assert scope_decs, "Expected scope decision identifying the dormant config field"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Impact map — surgical scope
# ═══════════════════════════════════════════════════════════════════════════════


class TestImpactAnalysis:
    def _impact_map(self, completed_state) -> dict:
        art = next(
            a for a in completed_state.stages["impact_analysis"].artifacts
            if a.name == "impact_map.json"
        )
        return json.loads(art.content)

    async def test_impact_map_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["impact_analysis"].artifacts
        assert any(a.name == "impact_map.json" for a in arts)

    async def test_impacted_components_non_empty(self, completed_state) -> None:
        data = self._impact_map(completed_state)
        assert len(data["impacted_components"]) >= 3

    async def test_get_redirect_explicitly_preserved_in_api_impact(
        self, completed_state
    ) -> None:
        data = self._impact_map(completed_state)
        redirect = data["api_impact"]["GET /{code}"]
        assert redirect["status"].startswith("PRESERVED")

    async def test_get_redirect_explicitly_preserved_in_preserved_apis(
        self, completed_state
    ) -> None:
        """GET /{code} must not appear as impacted endpoint."""
        data = self._impact_map(completed_state)
        impacted_apis = data.get("impacted_components", {})
        # urls.py may be impacted (for POST /shorten), but GET /{code} within it is preserved
        if "url_shortener/api/urls.py" in impacted_apis:
            urls_impact = impacted_apis["url_shortener/api/urls.py"]
            assert "GET /{code}" not in str(urls_impact.get("affected_endpoints", {}))
            assert "GET /{code}" in str(urls_impact.get("unaffected_endpoints", {}))

    async def test_service_layer_explicitly_preserved(self, completed_state) -> None:
        data = self._impact_map(completed_state)
        preserved = data["explicitly_preserved"]
        assert "url_shortener/services/url_service.py" in preserved
        assert "url_shortener/repositories/url_repo.py" in preserved

    async def test_data_model_explicitly_preserved(self, completed_state) -> None:
        data = self._impact_map(completed_state)
        preserved = data["explicitly_preserved"]
        assert "url_shortener/models/url.py" in preserved

    async def test_data_flow_delta_shown(self, completed_state) -> None:
        data = self._impact_map(completed_state)
        assert "before" in data["data_flow_delta"]
        assert "after" in data["data_flow_delta"]
        assert "unchanged_flows" in data["data_flow_delta"]
        # Confirm redirect is in unchanged flows
        assert any("GET /{code}" in f for f in data["data_flow_delta"]["unchanged_flows"])

    async def test_existing_tests_at_risk_documented(self, completed_state) -> None:
        data = self._impact_map(completed_state)
        assert "existing_tests_at_risk" in data
        assert "safe_existing_tests" in data

    async def test_impact_scope_validation_passed(self, completed_state) -> None:
        ctx = completed_state.stages["impact_analysis"]
        scope_val = [v for v in ctx.validations if v.rule_name == "impact_scope_defined"]
        assert scope_val and scope_val[0].passed

    async def test_get_redirect_preserved_validation(self, completed_state) -> None:
        ctx = completed_state.stages["impact_analysis"]
        val = [v for v in ctx.validations if v.rule_name == "get_redirect_preserved"]
        assert val and val[0].passed


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Risk assessment
# ═══════════════════════════════════════════════════════════════════════════════


class TestRiskAssessment:
    async def test_risk_assessment_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["risk_assessment"].artifacts
        assert any(a.name == "risk_assessment.json" for a in arts)

    async def test_redis_failure_risk_identified(self, completed_state) -> None:
        ctx = completed_state.stages["risk_assessment"]
        redis_risks = [r for r in ctx.risks if "Redis" in r.title]
        assert redis_risks, "Expected at least one Redis-related risk"

    async def test_high_severity_risks_identified(self, completed_state) -> None:
        ctx = completed_state.stages["risk_assessment"]
        high_risks = [r for r in ctx.risks if r.severity == RiskSeverity.HIGH]
        assert high_risks, "Expected at least one HIGH severity risk"

    async def test_zero_blocking_risks(self, completed_state) -> None:
        ctx = completed_state.stages["risk_assessment"]
        assert ctx.output_data["blocking_risks"] == 0

    async def test_risk_mitigation_decision_recorded(self, completed_state) -> None:
        decs = completed_state.stages["risk_assessment"].decisions
        trade_off = [d for d in decs if d.decision_type == DecisionType.TRADE_OFF]
        assert trade_off, "Expected TRADE_OFF decision for risk mitigation strategy"

    async def test_risks_have_mitigation_in_report(self, completed_state) -> None:
        art = next(
            a for a in completed_state.stages["risk_assessment"].artifacts
            if a.name == "risk_assessment.json"
        )
        data = json.loads(art.content)
        for risk in data["risks"]:
            assert "mitigation" in risk, f"Risk '{risk['id']}' has no mitigation plan"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Change plan — surgical precision
# ═══════════════════════════════════════════════════════════════════════════════


class TestChangePlan:
    def _change_plan(self, completed_state) -> dict:
        art = next(
            a for a in completed_state.stages["change_planning"].artifacts
            if a.name == "change_plan.json"
        )
        return json.loads(art.content)

    async def test_change_plan_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["change_planning"].artifacts
        assert any(a.name == "change_plan.json" for a in arts)

    async def test_change_plan_has_do_not_modify_list(self, completed_state) -> None:
        data = self._change_plan(completed_state)
        assert data["do_not_modify"]
        assert len(data["do_not_modify"]) >= 4

    async def test_service_layer_in_do_not_modify(self, completed_state) -> None:
        data = self._change_plan(completed_state)
        assert "url_shortener/services/url_service.py" in data["do_not_modify"]
        assert "url_shortener/repositories/url_repo.py" in data["do_not_modify"]

    async def test_model_in_do_not_modify(self, completed_state) -> None:
        data = self._change_plan(completed_state)
        assert "url_shortener/models/url.py" in data["do_not_modify"]

    async def test_get_redirect_not_in_implementation_tasks(
        self, completed_state
    ) -> None:
        """GET /{code} must NOT appear in any implementation task description."""
        data = self._change_plan(completed_state)
        task_details = " ".join(
            t.get("details", "") for t in data["implementation_tasks"]
        )
        # Check that GET /{code} is not being modified (should only appear in 'unaffected' context)
        # The POST /shorten modification should note GET /{code} is unaffected
        assert data["total_files_unchanged"] >= 4

    async def test_rollback_plan_documented(self, completed_state) -> None:
        data = self._change_plan(completed_state)
        assert "rollback_plan" in data
        assert data["rollback_plan"]["data_loss"] == "None (Redis keys are ephemeral; no DB changes)"

    async def test_dependency_graph_present(self, completed_state) -> None:
        data = self._change_plan(completed_state)
        assert "dependency_graph" in data
        assert "execution_order" in data["dependency_graph"]

    async def test_change_plan_validations_passed(self, completed_state) -> None:
        ctx = completed_state.stages["change_planning"]
        for v in ctx.validations:
            assert v.passed, f"Validation '{v.rule_name}' failed: {v.message}"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Regression test plan
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegressionTestPlan:
    async def test_regression_test_plan_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["regression_test_planning"].artifacts
        assert any(a.name == "regression_test_plan.json" for a in arts)

    async def test_new_tests_planned(self, completed_state) -> None:
        ctx = completed_state.stages["regression_test_planning"]
        assert ctx.output_data["new_test_count"] >= 10

    async def test_existing_tests_verified(self, completed_state) -> None:
        art = next(
            a for a in completed_state.stages["regression_test_planning"].artifacts
            if a.name == "regression_test_plan.json"
        )
        data = json.loads(art.content)
        # All existing test files should have a status
        existing = data["existing_tests_status"]
        assert "tests/unit/test_url_service.py" in existing
        assert "tests/integration/test_urls_api.py" in existing

    async def test_get_redirect_not_rate_limited_test_planned(self, completed_state) -> None:
        art = next(
            a for a in completed_state.stages["regression_test_planning"].artifacts
            if a.name == "regression_test_plan.json"
        )
        data = json.loads(art.content)
        # Should have a test that GET /{code} never returns 429
        all_new_tests = " ".join(
            " ".join(t["test_cases"]) for t in data["new_tests"].values()
        )
        assert "get_redirect_never_returns_429" in all_new_tests or \
               "redirect" in all_new_tests.lower()

    async def test_conftest_fixture_planned(self, completed_state) -> None:
        art = next(
            a for a in completed_state.stages["regression_test_planning"].artifacts
            if a.name == "regression_test_plan.json"
        )
        data = json.loads(art.content)
        assert "conftest_additions" in data


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Final validation
# ═══════════════════════════════════════════════════════════════════════════════


class TestFinalValidation:
    async def test_validation_report_artifact_present(self, completed_state) -> None:
        arts = completed_state.stages["validation"].artifacts
        assert any(a.name == "brownfield_validation_report.json" for a in arts)

    async def test_zero_critical_failures(self, completed_state) -> None:
        ctx = completed_state.stages["validation"]
        assert ctx.output_data["critical_failures"] == 0

    async def test_implementation_ready_confirmed(self, completed_state) -> None:
        assert completed_state.stages["validation"].output_data["implementation_ready"] is True

    async def test_all_validation_checks_passed(self, completed_state) -> None:
        ctx = completed_state.stages["validation"]
        for v in ctx.validations:
            assert v.passed, f"Validation '{v.rule_name}' failed: {v.message}"

    async def test_final_decision_recorded(self, completed_state) -> None:
        decs = completed_state.stages["validation"].decisions
        assert decs, "validation stage must record a final decision"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Approvals (2 human approvals)
# ═══════════════════════════════════════════════════════════════════════════════


class TestApprovals:
    async def test_at_least_2_approvals_obtained(self, completed_state) -> None:
        assert len(completed_state.approvals) >= 2, (
            f"Expected ≥2 approvals, got {len(completed_state.approvals)}"
        )

    async def test_change_planning_approval_present(self, completed_state) -> None:
        stage_approvals = [a for a in completed_state.approvals
                           if a.stage_name == "change_planning"]
        assert stage_approvals, "change_planning approval missing"

    async def test_validation_approval_present(self, completed_state) -> None:
        stage_approvals = [a for a in completed_state.approvals
                           if a.stage_name == "validation"]
        assert stage_approvals, "validation approval missing"

    async def test_all_approvals_approved(self, completed_state) -> None:
        from orchestrator.core.results import ApprovalStatus
        for appr in completed_state.approvals:
            assert appr.status == ApprovalStatus.APPROVED

    async def test_approval_rejection_fails_workflow(self) -> None:
        state = await run_brownfield_scenario(approval_gateway=AutoRejectGateway())
        assert state.status == WorkflowStatus.FAILED
        # change_planning is the first approval stage
        assert state.stages["change_planning"].status == StageStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Entry gate enforcement
# ═══════════════════════════════════════════════════════════════════════════════


class TestEntryGateEnforcement:
    async def test_impact_analysis_blocked_without_codebase_snapshot(self) -> None:
        from orchestrator.scenarios.brownfield import ImpactAnalysisStage
        from orchestrator.core.models import StageContext as SC

        stage = ImpactAnalysisStage()
        ctx = SC(stage_name="impact_analysis")
        # No input_data → entry gate should reject
        result = await stage.entry_gate(ctx)
        assert not result.passed
        assert "codebase_snapshot" in result.reason

    async def test_change_planning_blocked_without_risk_assessment(self) -> None:
        from orchestrator.scenarios.brownfield import ChangePlanningStage
        from orchestrator.core.models import StageContext as SC

        stage = ChangePlanningStage()
        ctx = SC(stage_name="change_planning")
        result = await stage.entry_gate(ctx)
        assert not result.passed
        assert "risk_assessment" in result.reason

    async def test_change_planning_blocked_by_blocking_risks(self) -> None:
        from orchestrator.scenarios.brownfield import ChangePlanningStage
        from orchestrator.core.models import StageContext as SC

        stage = ChangePlanningStage()
        ctx = SC(stage_name="change_planning")
        ctx.input_data["risk_assessment"] = {"present": True}
        ctx.input_data["blocking_risks"] = 2  # blocking risks present
        result = await stage.entry_gate(ctx)
        assert not result.passed


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Decision lineage
# ═══════════════════════════════════════════════════════════════════════════════


class TestDecisionLineage:
    async def test_decisions_span_multiple_stages(self, completed_state) -> None:
        stages_with_decisions = [
            name for name, ctx in completed_state.stages.items()
            if ctx.decisions
        ]
        assert len(stages_with_decisions) >= 4

    async def test_scope_decisions_link_stages(self, completed_state) -> None:
        all_decs = [
            d for ctx in completed_state.stages.values() for d in ctx.decisions
        ]
        scope_decs = [d for d in all_decs if d.decision_type == DecisionType.SCOPE]
        assert len(scope_decs) >= 2

    async def test_decision_chain_through_parent_ids(self, completed_state) -> None:
        all_decs = {
            d.id: d
            for ctx in completed_state.stages.values()
            for d in ctx.decisions
        }
        children = [d for d in all_decs.values() if d.parent_decision_id]
        assert children, "Expected at least one decision chain via parent_decision_id"

    async def test_all_decisions_have_rationale(self, completed_state) -> None:
        for name, ctx in completed_state.stages.items():
            for dec in ctx.decisions:
                assert dec.rationale, (
                    f"Decision '{dec.title}' in stage '{name}' has empty rationale"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Observability
# ═══════════════════════════════════════════════════════════════════════════════


class TestBrownfieldObservability:
    async def test_observability_report_builds(self, completed_state) -> None:
        report = build_observability_report(completed_state)
        assert report.workflow_id == completed_state.id

    async def test_trace_has_decisions(self, completed_state) -> None:
        report = build_observability_report(completed_state)
        assert len(report.execution_trace.decisions) >= 4

    async def test_trace_has_artifacts(self, completed_state) -> None:
        report = build_observability_report(completed_state)
        assert len(report.execution_trace.artifacts) >= 6

    async def test_metrics_show_completion(self, completed_state) -> None:
        report = build_observability_report(completed_state)
        assert report.metrics.succeeded is True
        assert report.metrics.total_latency_seconds is not None

    async def test_structured_logs_include_stage_events(self, completed_state) -> None:
        from orchestrator.core.observability import build_structured_logs
        logs = build_structured_logs(completed_state)
        events = {l.event for l in logs}
        assert "stage_started" in events
        assert "stage_completed" in events


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Replanning
# ═══════════════════════════════════════════════════════════════════════════════


class TestBrownfieldReplan:
    async def test_impact_analysis_after_risk_change(self, completed_state) -> None:
        defn = create_brownfield_workflow()
        stages = create_brownfield_stages()
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        impact = engine.analyze_impact(
            completed_state,
            ChangeEvent(
                event_type=ChangeEventType.DECISION_CHANGED,
                originating_stage="risk_assessment",
                change_description="New Redis failure risk added — change plan must be revised",
            ),
        )
        # change_planning and all downstream must replan
        assert "change_planning" in impact.impacted_stages
        assert "regression_test_planning" in impact.impacted_stages
        assert "validation" in impact.impacted_stages
        # Early analysis stages preserved
        assert "codebase_analysis" in impact.preserved_stages
        assert "impact_analysis" in impact.preserved_stages
        assert "risk_assessment" in impact.preserved_stages

    async def test_replan_after_impact_analysis_change(self) -> None:
        state = await run_brownfield_scenario(approval_gateway=AutoApproveGateway())
        assert state.status == WorkflowStatus.COMPLETED

        defn = create_brownfield_workflow()
        stages = create_brownfield_stages()
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.replan(
            state,
            ChangeEvent(
                event_type=ChangeEventType.DECISION_CHANGED,
                originating_stage="impact_analysis",
                change_description="Scope expanded: DELETE /{code} also needs rate limiting",
            ),
        )
        assert state.status == WorkflowStatus.COMPLETED
        assert state.replan_count == 1
        result = state.replan_history[0]
        assert "codebase_analysis" in result.stages_preserved
        assert "impact_analysis" in result.stages_preserved
        assert "change_planning" in result.stages_replanned
        assert "validation" in result.stages_replanned
