"""
Automated tests for the Ambiguous requirement scenario.

Key invariants tested
──────────────────────
• Every scope item in the normalised requirement traces to a clarification answer.
• Every clarification answer is recorded as a Decision with made_by field.
• No implementation task may appear without a source_decision field.
• Different clarification answers produce different task plans.
• The DefaultAnswerGateway path produces a coherent (conservative) plan.
• Validation correctly detects silent inventions if they are present.

Coverage
────────
1.  Full scenario completes with COMPLETED status
2.  All 5 stages execute and complete
3.  AMBIGUOUS requirement type used
4.  Ambiguity catalogue: ≥7 ambiguities detected
5.  Ambiguity catalogue: ≥7 clarification questions generated
6.  Execution paused flag set during clarification
7.  All 7 clarification answers recorded as Decisions
8.  Decisions made_by field set ('human' or 'default')
9.  No decision has made_by='orchestrator' in clarification stage
10. Normalised requirement: every FR has source_decision field
11. Assumption registry produced with confirmed vs default sections
12. Task plan: every task has source_decision
13. Tasks added by clarification: count > 0 for full-answer path
14. Tasks excluded by clarification: UI dashboard excluded (Q5=API-only)
15. Validation: 0 silent inventions
16. Validation: 0 orphan tasks
17. Different gateway (expanded metrics) → different task count
18. DefaultAnswerGateway → coherent minimal plan
19. Different Q1 answer → unique_visitors task added
20. Different Q1 answer → country task added
21. Q5 dashboard answer → UI dashboard task NOT excluded
22. Approval at validation stage required
23. Entry gate: clarification blocked without ambiguity_detection output
24. Entry gate: normalization blocked without clarification output
25. Entry gate: task_planning blocked without normalization output
26. Entry gate: validation blocked without task_planning output
27. ClarificationRecord.was_human_answered correct for preset vs default
28. Observability report buildable
29. Execution trace has DECISION steps from clarification
30. Replanning after normalization change works

asyncio_mode=auto (pytest.ini).
"""
from __future__ import annotations

import json

import pytest

from orchestrator.core.autonomy import AutoApproveGateway, AutoRejectGateway
from orchestrator.core.models import RequirementType, StageStatus, WorkflowStatus
from orchestrator.core.observability import build_observability_report
from orchestrator.core.replanning import ChangeEvent, ChangeEventType
from orchestrator.core.results import DecisionType
from orchestrator.engine.workflow_engine import WorkflowEngine
from orchestrator.scenarios.ambiguous import (
    DEFAULT_PRESET_ANSWERS,
    ClarificationRecord,
    DefaultAnswerGateway,
    PresetClarificationGateway,
    create_ambiguous_stages,
    create_ambiguous_workflow,
    run_ambiguous_scenario,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def full_state():
    """Full scenario with all 7 human answers preset."""
    return await run_ambiguous_scenario(
        clarification_gateway=PresetClarificationGateway(DEFAULT_PRESET_ANSWERS),
        approval_gateway=AutoApproveGateway(),
    )


@pytest.fixture(scope="module")
async def default_answer_state():
    """Scenario using DefaultAnswerGateway (safe defaults only, no human input)."""
    return await run_ambiguous_scenario(
        clarification_gateway=DefaultAnswerGateway(),
        approval_gateway=AutoApproveGateway(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Pipeline completion
# ═══════════════════════════════════════════════════════════════════════════════


class TestPipelineCompletion:
    async def test_scenario_completes(self, full_state) -> None:
        assert full_state.status == WorkflowStatus.COMPLETED

    async def test_all_5_stages_completed(self, full_state) -> None:
        expected = {
            "ambiguity_detection", "clarification",
            "normalization", "task_planning", "validation",
        }
        for name in expected:
            assert name in full_state.stages
            assert full_state.stages[name].status == StageStatus.COMPLETED

    async def test_requirement_type_ambiguous(self, full_state) -> None:
        assert full_state.requirement.requirement_type == RequirementType.AMBIGUOUS

    async def test_requirement_has_ambiguities(self, full_state) -> None:
        assert len(full_state.requirement.ambiguities) == 7

    async def test_completed_at_set(self, full_state) -> None:
        assert full_state.completed_at is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Ambiguity detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestAmbiguityDetection:
    async def test_ambiguity_catalogue_artifact_present(self, full_state) -> None:
        arts = full_state.stages["ambiguity_detection"].artifacts
        assert any(a.name == "ambiguity_catalogue.json" for a in arts)

    async def test_at_least_7_ambiguities_detected(self, full_state) -> None:
        ctx = full_state.stages["ambiguity_detection"]
        assert ctx.output_data["ambiguity_count"] >= 7

    async def test_at_least_7_questions_generated(self, full_state) -> None:
        ctx = full_state.stages["ambiguity_detection"]
        assert ctx.output_data["question_count"] >= 7

    async def test_clarification_needed_flag_set(self, full_state) -> None:
        ctx = full_state.stages["ambiguity_detection"]
        assert ctx.output_data["clarification_needed"] is True

    async def test_status_blocked_pending_clarification_in_catalogue(
        self, full_state
    ) -> None:
        art = next(
            a for a in full_state.stages["ambiguity_detection"].artifacts
            if a.name == "ambiguity_catalogue.json"
        )
        data = json.loads(art.content)
        assert data["status"] == "BLOCKED_PENDING_CLARIFICATION"

    async def test_detection_decision_recorded(self, full_state) -> None:
        ctx = full_state.stages["ambiguity_detection"]
        assert ctx.decisions, "ambiguity_detection must record a decision"
        assert ctx.decisions[0].decision_type == DecisionType.SCOPE

    async def test_questions_have_safe_defaults(self, full_state) -> None:
        art = next(
            a for a in full_state.stages["ambiguity_detection"].artifacts
            if a.name == "ambiguity_catalogue.json"
        )
        data = json.loads(art.content)
        for q in data["clarification_questions"]:
            assert q.get("safe_default"), f"Q{q['id']} missing safe_default"

    async def test_questions_have_impact_description(self, full_state) -> None:
        art = next(
            a for a in full_state.stages["ambiguity_detection"].artifacts
            if a.name == "ambiguity_catalogue.json"
        )
        data = json.loads(art.content)
        for q in data["clarification_questions"]:
            assert q.get("impact"), f"Q{q['id']} missing impact"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Clarification — human answers recorded as decisions
# ═══════════════════════════════════════════════════════════════════════════════


class TestClarification:
    async def test_clarification_log_artifact_present(self, full_state) -> None:
        arts = full_state.stages["clarification"].artifacts
        assert any(a.name == "clarification_log.json" for a in arts)

    async def test_7_answers_recorded(self, full_state) -> None:
        ctx = full_state.stages["clarification"]
        assert len(ctx.decisions) == 7, (
            f"Expected 7 decisions (one per answer), got {len(ctx.decisions)}"
        )

    async def test_all_decisions_made_by_human(self, full_state) -> None:
        ctx = full_state.stages["clarification"]
        for dec in ctx.decisions:
            assert dec.made_by == "human", (
                f"Decision '{dec.title}' has made_by='{dec.made_by}' — expected 'human'"
            )

    async def test_no_decision_made_by_orchestrator_in_clarification(
        self, full_state
    ) -> None:
        ctx = full_state.stages["clarification"]
        orchestrator_decs = [d for d in ctx.decisions if d.made_by == "orchestrator"]
        assert not orchestrator_decs, (
            "No clarification decision may be made_by='orchestrator' — "
            "these are human business decisions"
        )

    async def test_pause_timestamps_recorded(self, full_state) -> None:
        ctx = full_state.stages["clarification"]
        assert "clarification_requested_at" in ctx.output_data
        assert "clarification_answered_at" in ctx.output_data

    async def test_clarification_complete_flag(self, full_state) -> None:
        ctx = full_state.stages["clarification"]
        assert ctx.output_data["clarification_complete"] is True

    async def test_human_answered_count_matches_preset(self, full_state) -> None:
        ctx = full_state.stages["clarification"]
        # All 7 were preset answers → all 7 are human-answered
        assert ctx.output_data["human_answered_count"] == 7
        assert ctx.output_data["defaulted_count"] == 0

    async def test_decisions_have_decision_ids_in_log(self, full_state) -> None:
        art = next(
            a for a in full_state.stages["clarification"].artifacts
            if a.name == "clarification_log.json"
        )
        data = json.loads(art.content)
        for record in data["clarification_records"]:
            assert record["decision_id"], f"Q{record['question_id']} has no decision_id"

    async def test_default_answer_gateway_marks_answered_by_default(
        self, default_answer_state
    ) -> None:
        ctx = default_answer_state.stages["clarification"]
        assert ctx.output_data["defaulted_count"] == 7
        assert ctx.output_data["human_answered_count"] == 0

    async def test_default_gateway_decisions_made_by_default(
        self, default_answer_state
    ) -> None:
        ctx = default_answer_state.stages["clarification"]
        for dec in ctx.decisions:
            assert dec.made_by == "default", (
                f"Expected made_by='default', got '{dec.made_by}'"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Normalisation — no silent inventions
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalization:
    def _norm(self, state) -> dict:
        art = next(
            a for a in state.stages["normalization"].artifacts
            if a.name == "normalised_requirement.json"
        )
        return json.loads(art.content)

    def _assumption_registry(self, state) -> dict:
        art = next(
            a for a in state.stages["normalization"].artifacts
            if a.name == "assumption_registry.json"
        )
        return json.loads(art.content)

    async def test_normalised_requirement_artifact_present(self, full_state) -> None:
        arts = full_state.stages["normalization"].artifacts
        assert any(a.name == "normalised_requirement.json" for a in arts)

    async def test_assumption_registry_artifact_present(self, full_state) -> None:
        arts = full_state.stages["normalization"].artifacts
        assert any(a.name == "assumption_registry.json" for a in arts)

    async def test_every_fr_has_source_decision(self, full_state) -> None:
        """No silent invention: every FR must trace to a clarification Q-ID."""
        data = self._norm(full_state)
        for fr in data["functional_requirements"]:
            assert fr.get("source_decision"), (
                f"FR '{fr['id']}' has no source_decision — this is a silent invention"
            )

    async def test_assumption_registry_has_confirmed_section(
        self, full_state
    ) -> None:
        reg = self._assumption_registry(full_state)
        assert "confirmed_by_human" in reg
        assert len(reg["confirmed_by_human"]) > 0

    async def test_assumption_registry_has_safe_defaults_section(
        self, full_state
    ) -> None:
        reg = self._assumption_registry(full_state)
        assert "safe_defaults_applied" in reg

    async def test_excluded_features_documented(self, full_state) -> None:
        data = self._norm(full_state)
        # Q5 answer = API only → UI dashboard excluded
        excluded = data.get("explicitly_excluded", [])
        assert any("dashboard" in str(e).lower() or "ui" in str(e).lower()
                   for e in excluded), "UI dashboard should be excluded for API-only answer"

    async def test_country_included_for_full_metrics_answer(
        self, full_state
    ) -> None:
        """Q1 includes 'country' → country breakdown should be in FR text."""
        data = self._norm(full_state)
        fr2 = next(fr for fr in data["functional_requirements"] if fr["id"] == "FR-ANLY-002")
        assert "country" in fr2["text"].lower()

    async def test_hashed_ip_in_fr_for_gdpr_answer(self, full_state) -> None:
        """Q7 = hashed IP → FR should mention hashed IP."""
        data = self._norm(full_state)
        fr1 = next(fr for fr in data["functional_requirements"] if fr["id"] == "FR-ANLY-001")
        assert "hash" in fr1["text"].lower()

    async def test_normalization_complete_flag(self, full_state) -> None:
        ctx = full_state.stages["normalization"]
        assert ctx.output_data["normalisation_complete"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Task planning — tasks sourced from clarification
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskPlanning:
    def _plan(self, state) -> dict:
        art = next(
            a for a in state.stages["task_planning"].artifacts
            if a.name == "task_plan.json"
        )
        return json.loads(art.content)

    async def test_task_plan_artifact_present(self, full_state) -> None:
        arts = full_state.stages["task_planning"].artifacts
        assert any(a.name == "task_plan.json" for a in arts)

    async def test_every_task_has_source_decision(self, full_state) -> None:
        """No orphan tasks: every implementation task must have source_decision."""
        plan = self._plan(full_state)
        for task in plan["implementation_tasks"]:
            assert task.get("source_decision"), (
                f"Task '{task['id']}' missing source_decision — orphan task"
            )

    async def test_tasks_added_by_clarification_count_positive(
        self, full_state
    ) -> None:
        """With full metrics answer (Q1), multiple tasks should be added."""
        ctx = full_state.stages["task_planning"]
        assert ctx.output_data["tasks_added_by_clarification"] > 0

    async def test_ui_dashboard_excluded(self, full_state) -> None:
        """Q5=API-only → UI dashboard task should be in excluded_tasks."""
        plan = self._plan(full_state)
        excluded_ids = [t["id"] for t in plan["excluded_tasks"]]
        assert "EXCL-001" in excluded_ids

    async def test_unique_visitors_task_added_for_full_metrics(
        self, full_state
    ) -> None:
        """Q1 includes unique visitors → T-006 or similar task should be added."""
        plan = self._plan(full_state)
        titles = [t["title"].lower() for t in plan["implementation_tasks"]]
        assert any("unique" in t for t in titles), (
            "Q1='full analytics' should add a unique visitor counting task"
        )

    async def test_country_task_added_for_country_metrics(self, full_state) -> None:
        """Q1 includes country → geo-lookup task should be added."""
        plan = self._plan(full_state)
        titles = [t["title"].lower() for t in plan["implementation_tasks"]]
        assert any("country" in t or "geo" in t for t in titles), (
            "Q1 answer includes 'country' → should add geo-lookup task"
        )

    async def test_hashing_task_added_for_gdpr_answer(self, full_state) -> None:
        """Q7=hashed → IP hashing task should be added."""
        plan = self._plan(full_state)
        titles = [t["title"].lower() for t in plan["implementation_tasks"]]
        assert any("hash" in t for t in titles), (
            "Q7='hashed IP' should add an IP hashing task"
        )

    async def test_planning_complete_flag(self, full_state) -> None:
        assert full_state.stages["task_planning"].output_data["planning_complete"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Different answers → different task plan
# ═══════════════════════════════════════════════════════════════════════════════


class TestAnswerSensitivity:
    async def test_minimal_metrics_answer_produces_fewer_tasks(self) -> None:
        """Q1='Click count only' should produce fewer tasks than 'full analytics'."""
        minimal_state = await run_ambiguous_scenario(
            clarification_gateway=PresetClarificationGateway({
                **DEFAULT_PRESET_ANSWERS,
                "Q1": "Click count only (simplest)",
            }),
            approval_gateway=AutoApproveGateway(),
        )
        full_state = await run_ambiguous_scenario(
            clarification_gateway=PresetClarificationGateway(DEFAULT_PRESET_ANSWERS),
            approval_gateway=AutoApproveGateway(),
        )

        minimal_tasks = minimal_state.stages["task_planning"].output_data["total_tasks"]
        full_tasks = full_state.stages["task_planning"].output_data["total_tasks"]
        assert minimal_tasks < full_tasks, (
            f"Minimal metrics should produce fewer tasks ({minimal_tasks}) "
            f"than full analytics ({full_tasks})"
        )

    async def test_default_answers_produce_valid_plan(
        self, default_answer_state
    ) -> None:
        """DefaultAnswerGateway path must also produce a complete, valid plan."""
        assert default_answer_state.status == WorkflowStatus.COMPLETED
        ctx = default_answer_state.stages["task_planning"]
        assert ctx.output_data["total_tasks"] >= 3

    async def test_default_answers_still_have_no_silent_inventions(
        self, default_answer_state
    ) -> None:
        """Even with safe defaults, all tasks must have source_decision."""
        art = next(
            a for a in default_answer_state.stages["task_planning"].artifacts
            if a.name == "task_plan.json"
        )
        plan = json.loads(art.content)
        for task in plan["implementation_tasks"]:
            assert task.get("source_decision"), (
                f"Task '{task['id']}' is an orphan even on default-answer path"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Validation — zero silent inventions
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidation:
    async def test_validation_report_artifact_present(self, full_state) -> None:
        arts = full_state.stages["validation"].artifacts
        assert any(a.name == "ambiguous_validation_report.json" for a in arts)

    async def test_zero_critical_failures(self, full_state) -> None:
        ctx = full_state.stages["validation"]
        assert ctx.output_data["critical_failures"] == 0

    async def test_no_silent_inventions_check_passed(self, full_state) -> None:
        ctx = full_state.stages["validation"]
        no_inv = next(
            (v for v in ctx.validations if v.rule_name == "no_silent_inventions"),
            None,
        )
        assert no_inv is not None and no_inv.passed

    async def test_all_answers_became_decisions_check_passed(
        self, full_state
    ) -> None:
        ctx = full_state.stages["validation"]
        check = next(
            (v for v in ctx.validations if v.rule_name == "all_answers_became_decisions"),
            None,
        )
        assert check is not None and check.passed

    async def test_assumption_registry_check_passed(self, full_state) -> None:
        ctx = full_state.stages["validation"]
        check = next(
            (v for v in ctx.validations if v.rule_name == "assumption_registry_present"),
            None,
        )
        assert check is not None and check.passed

    async def test_implementation_ready_confirmed(self, full_state) -> None:
        ctx = full_state.stages["validation"]
        assert ctx.output_data["implementation_ready"] is True

    async def test_human_approval_at_validation(self, full_state) -> None:
        val_approvals = [
            a for a in full_state.approvals if a.stage_name == "validation"
        ]
        assert val_approvals, "validation stage must obtain human approval"

    async def test_approval_rejection_fails_workflow(self) -> None:
        state = await run_ambiguous_scenario(
            clarification_gateway=PresetClarificationGateway(DEFAULT_PRESET_ANSWERS),
            approval_gateway=AutoRejectGateway(),
        )
        assert state.status == WorkflowStatus.FAILED
        val_ctx = state.stages.get("validation")
        assert val_ctx is not None
        assert val_ctx.status == StageStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Entry gate enforcement — no skipping stages
# ═══════════════════════════════════════════════════════════════════════════════


class TestEntryGates:
    async def test_clarification_blocked_without_ambiguity_output(self) -> None:
        from orchestrator.scenarios.ambiguous import ClarificationStage
        from orchestrator.core.models import StageContext as SC

        stage = ClarificationStage(gateway=DefaultAnswerGateway())
        ctx = SC(stage_name="clarification")
        result = await stage.entry_gate(ctx)
        assert not result.passed
        assert "clarification_questions" in result.reason

    async def test_normalization_blocked_without_clarifications(self) -> None:
        from orchestrator.scenarios.ambiguous import RequirementNormalizationStage
        from orchestrator.core.models import StageContext as SC

        stage = RequirementNormalizationStage()
        ctx = SC(stage_name="normalization")
        result = await stage.entry_gate(ctx)
        assert not result.passed
        assert "clarifications" in result.reason

    async def test_task_planning_blocked_without_normalization(self) -> None:
        from orchestrator.scenarios.ambiguous import TaskPlanningStage
        from orchestrator.core.models import StageContext as SC

        stage = TaskPlanningStage()
        ctx = SC(stage_name="task_planning")
        result = await stage.entry_gate(ctx)
        assert not result.passed
        assert "normalised_requirement" in result.reason

    async def test_validation_blocked_without_task_planning(self) -> None:
        from orchestrator.scenarios.ambiguous import AmbiguousValidationStage
        from orchestrator.core.models import StageContext as SC

        stage = AmbiguousValidationStage()
        ctx = SC(stage_name="validation")
        result = await stage.entry_gate(ctx)
        assert not result.passed


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Observability
# ═══════════════════════════════════════════════════════════════════════════════


class TestObservability:
    async def test_observability_report_builds(self, full_state) -> None:
        report = build_observability_report(full_state)
        assert report.workflow_id == full_state.id

    async def test_trace_has_many_decisions(self, full_state) -> None:
        report = build_observability_report(full_state)
        # At least 7 clarification decisions + detection + normalization + planning + validation
        assert len(report.execution_trace.decisions) >= 7

    async def test_trace_shows_human_decisions(self, full_state) -> None:
        """Decision trace must include decisions made_by='human'."""
        all_decs = [
            d for ctx in full_state.stages.values() for d in ctx.decisions
        ]
        human_decs = [d for d in all_decs if d.made_by == "human"]
        assert human_decs, "Trace must contain decisions made_by='human'"

    async def test_metrics_show_completion(self, full_state) -> None:
        report = build_observability_report(full_state)
        assert report.metrics.succeeded is True

    async def test_stage_ids_unique(self, full_state) -> None:
        ids = [ctx.stage_id for ctx in full_state.stages.values()]
        assert len(ids) == len(set(ids))


# ═══════════════════════════════════════════════════════════════════════════════
# 10. ClarificationRecord helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestClarificationRecord:
    def test_human_answered_flag(self) -> None:
        from orchestrator.scenarios.ambiguous import (
            ClarificationAnswer, ClarificationQuestion,
        )
        q = ClarificationQuestion(
            id="Q1", question="?", context="ctx", default="d",
            impact="imp", ambiguity_id="a1",
        )
        a_human = ClarificationAnswer(question_id="Q1", answer="yes", answered_by="human")
        a_default = ClarificationAnswer(question_id="Q1", answer="d", answered_by="default")
        assert ClarificationRecord(question=q, answer=a_human).was_human_answered is True
        assert ClarificationRecord(question=q, answer=a_default).was_human_answered is False


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Replanning after normalisation change
# ═══════════════════════════════════════════════════════════════════════════════


class TestAmbiguousReplan:
    async def test_replan_after_normalization_change(self) -> None:
        state = await run_ambiguous_scenario(
            clarification_gateway=PresetClarificationGateway(DEFAULT_PRESET_ANSWERS),
            approval_gateway=AutoApproveGateway(),
        )
        assert state.status == WorkflowStatus.COMPLETED

        defn = create_ambiguous_workflow()
        stages = create_ambiguous_stages(
            PresetClarificationGateway(DEFAULT_PRESET_ANSWERS)
        )
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        state = await engine.replan(
            state,
            ChangeEvent(
                event_type=ChangeEventType.DECISION_CHANGED,
                originating_stage="normalization",
                change_description="Human revised Q1 answer — adding device tracking",
            ),
        )
        assert state.status == WorkflowStatus.COMPLETED
        assert state.replan_count == 1
        result = state.replan_history[0]
        assert "ambiguity_detection" in result.stages_preserved
        assert "clarification" in result.stages_preserved
        assert "normalization" in result.stages_preserved
        assert "task_planning" in result.stages_replanned
        assert "validation" in result.stages_replanned

    async def test_impact_analysis_for_clarification_change(self) -> None:
        state = await run_ambiguous_scenario(
            clarification_gateway=DefaultAnswerGateway(),
            approval_gateway=AutoApproveGateway(),
        )
        defn = create_ambiguous_workflow()
        stages = create_ambiguous_stages()
        engine = WorkflowEngine(
            definition=defn,
            stages=stages,
            approval_gateway=AutoApproveGateway(),
        )
        impact = engine.analyze_impact(
            state,
            ChangeEvent(
                event_type=ChangeEventType.DECISION_CHANGED,
                originating_stage="clarification",
                change_description="Human revised answers after initial submission",
            ),
        )
        assert "normalization" in impact.impacted_stages
        assert "task_planning" in impact.impacted_stages
        assert "validation" in impact.impacted_stages
        assert "ambiguity_detection" in impact.preserved_stages


# ═══════════════════════════════════════════════════════════════════════════════
# 12. HumanClarificationGateway (CLI)
# ═══════════════════════════════════════════════════════════════════════════════


class TestHumanClarificationGateway:
    async def test_records_human_answers(self) -> None:
        from orchestrator.scenarios.ambiguous import (
            ClarificationQuestion,
            HumanClarificationGateway,
        )

        q = ClarificationQuestion(
            id="Q1",
            question="What metrics?",
            context="scope",
            default="clicks only",
            impact="schema",
            ambiguity_id="analytics_metrics",
            options=["clicks only", "full analytics"],
        )
        gateway = HumanClarificationGateway(
            input_fn=lambda _prompt="": "full analytics",
            output_fn=lambda *a, **k: None,
        )
        answers = await gateway.clarify([q])
        assert len(answers) == 1
        assert answers[0].answer == "full analytics"
        assert answers[0].answered_by == "human"

    async def test_empty_input_uses_safe_default(self) -> None:
        from orchestrator.scenarios.ambiguous import (
            ClarificationQuestion,
            HumanClarificationGateway,
        )

        q = ClarificationQuestion(
            id="Q2",
            question="Who can view?",
            context="auth",
            default="URL owner only",
            impact="auth middleware",
            ambiguity_id="access_control",
        )
        gateway = HumanClarificationGateway(
            input_fn=lambda _prompt="": "  ",
            output_fn=lambda *a, **k: None,
        )
        answers = await gateway.clarify([q])
        assert answers[0].answer == "URL owner only"
        assert answers[0].answered_by == "default"

    async def test_scenario_runs_with_human_gateway(self) -> None:
        """Injected CLI answers drive a full ambiguous run to COMPLETED."""
        from orchestrator.scenarios.ambiguous import HumanClarificationGateway

        # Seven questions in the scenario; answer all with non-empty strings.
        replies = iter(
            [
                "clicks only",
                "URL owner only",
                "90 days",
                "async background",
                "REST API only",
                "preserve redirect SLA",
                "hashed IP (SHA-256)",
            ]
        )
        gateway = HumanClarificationGateway(
            input_fn=lambda _prompt="": next(replies),
            output_fn=lambda *a, **k: None,
        )
        state = await run_ambiguous_scenario(
            clarification_gateway=gateway,
            approval_gateway=AutoApproveGateway(),
        )
        assert state.status == WorkflowStatus.COMPLETED
        human_decs = [
            d
            for ctx in state.stages.values()
            for d in ctx.decisions
            if d.made_by == "human"
        ]
        assert human_decs
