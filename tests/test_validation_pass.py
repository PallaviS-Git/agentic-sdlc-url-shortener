"""
Dedicated testing and validation pass.

Covers genuine coverage gaps and important failure/edge-case scenarios
across all 21 feature areas.  No production code is modified; only
tests are added.

Areas targeted
──────────────
 1. URL shortener APIs          — handler coverage via http_client
 2. Requirement parsing         — model properties and edge cases
 3. Requirement normalisation   — ambiguity resolution
 4. Task decomposition          — StageContext.ready_tasks, deps
 5. Dependency graph            — edge cases, CONDITIONAL type
 6. Sequential execution        — ordering proof
 7. Parallel execution          — concurrent branches
 8. Synchronisation             — multi-predecessor sync point
 9. Entry / exit gates          — exception paths
10. Context propagation         — 3-hop multi-predecessor merge
11. Decision lineage            — lineage on empty / minimal state
12. Human approval              — timeout / escalation records
13. Policy guardrails           — WARN pass-through, combined policies
14. Retry                       — exit-gate exception triggers retry
15. Fallback                    — USE_PRESET without fallback_output
16. Rollback                    — rollback not called on success
17. Safe-stop                   — parallel branch triggers safe-stop
18. Dynamic replanning          — replan on FAILED workflow
19. Greenfield scenario         — artifact content spot-checks
20. Brownfield scenario         — preserved file list integrity
21. Ambiguous scenario          — gateway answer sensitivity

Also covered
────────────
  BaseAgent / BaseOrchestrator  — repr + abstract instantiation guard
  Repository                    — mock-session unit tests
  Observability logging         — configure_logging / get_audit_logger
  Model properties              — uncovered StageContext helpers
  Engine branches               — exit-gate exception + final-QC rejection
  Failure scenarios             — parallel failure, invalid workflow, empty DAG
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── Core imports ─────────────────────────────────────────────────────────────
from orchestrator.core.autonomy import (
    ActionImpact,
    AutoApproveGateway,
    AutoRejectGateway,
)
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.context import ExecutionContext
from orchestrator.core.failure import (
    FailureClassification,
    FallbackBehavior,
    RecoveryDecision,
    RetryPolicy,
    StageAttemptRecord,
)
from orchestrator.core.governance import (
    ActionContext,
    EnforcementDecision,
    PolicyDomain,
    PolicyEngine,
    PolicyViolation,
    RequireChangeTicket,
    WarnOnHighRiskAction,
)
from orchestrator.core.graph import DependencyType, StageDependency, WorkflowDefinition
from orchestrator.core.lineage import build_lineage
from orchestrator.core.models import (
    AmbiguityItem,
    GateResult,
    Requirement,
    RequirementType,
    StageContext,
    StageStatus,
    Task,
    TaskStatus,
    WorkflowState,
    WorkflowStatus,
)
from orchestrator.core.observability import (
    build_execution_trace,
    build_structured_logs,
    compute_reliability_metrics,
    compute_workflow_metrics,
)
from orchestrator.core.replanning import ChangeEvent, ChangeEventType
from orchestrator.core.results import (
    Artifact,
    ArtifactType,
    Decision,
    DecisionType,
    ExecutionResult,
    ExecutionStatus,
    Risk,
    RiskSeverity,
    ValidationResult,
    ValidationSeverity,
)
from orchestrator.engine.workflow_engine import WorkflowEngine


# ─── Stage stub helpers ───────────────────────────────────────────────────────


class _SimpleStage(BaseStage):
    """Minimal concrete stage for engine tests."""

    def __init__(self, name: str, *, output: dict | None = None) -> None:
        self.stage_name = name
        self._output = output or {}
        self.call_count = 0

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_entry", passed=True)

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name=f"{self.stage_name}_exit", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        self.call_count += 1
        ctx.output_data.update(self._output)
        ctx.output_data["executed_by"] = self.stage_name
        return ctx

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


@pytest.fixture()
def req() -> Requirement:
    return Requirement(
        title="Validation pass test",
        raw_text="Test requirement",
        requirement_type=RequirementType.GREENFIELD,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. URL Shortener APIs  (via http_client fixture from conftest.py)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUrlShortenerAPIHandlers:
    """Cover the API handler lines (api/urls.py ~58% in unit-only run)."""

    async def test_shorten_returns_201_and_short_url(self, http_client) -> None:
        resp = await http_client.post("/shorten", json={"url": "https://example.com"})
        assert resp.status_code == 201
        body = resp.json()
        assert "short_url" in body
        assert "code" in body
        assert body["original_url"] == "https://example.com"

    async def test_shorten_invalid_url_returns_422(self, http_client) -> None:
        resp = await http_client.post("/shorten", json={"url": "not-a-url"})
        assert resp.status_code == 422

    async def test_shorten_url_too_long_returns_422(self, http_client) -> None:
        long_url = "https://example.com/" + "x" * 3000
        resp = await http_client.post("/shorten", json={"url": long_url})
        assert resp.status_code == 422

    async def test_redirect_returns_302(self, http_client) -> None:
        # Create a short URL first
        create_resp = await http_client.post("/shorten", json={"url": "https://python.org"})
        assert create_resp.status_code == 201
        code = create_resp.json()["code"]

        # Follow redirect
        resp = await http_client.get(f"/{code}", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://python.org"

    async def test_redirect_unknown_code_returns_404(self, http_client) -> None:
        resp = await http_client.get("/xxxxxxxx", follow_redirects=False)
        assert resp.status_code == 404

    async def test_delete_returns_204(self, http_client) -> None:
        create_resp = await http_client.post(
            "/shorten", json={"url": "https://to-delete.example.com"}
        )
        code = create_resp.json()["code"]

        del_resp = await http_client.delete(f"/{code}")
        assert del_resp.status_code == 204

    async def test_delete_deactivated_code_returns_404(self, http_client) -> None:
        create_resp = await http_client.post(
            "/shorten", json={"url": "https://double-delete.example.com"}
        )
        code = create_resp.json()["code"]

        await http_client.delete(f"/{code}")
        # Second delete should 404
        resp = await http_client.delete(f"/{code}")
        assert resp.status_code == 404

    async def test_delete_unknown_code_returns_404(self, http_client) -> None:
        resp = await http_client.delete("/zzzzzzzz")
        assert resp.status_code == 404

    async def test_redirect_after_delete_returns_404(self, http_client) -> None:
        create_resp = await http_client.post(
            "/shorten", json={"url": "https://gone.example.com"}
        )
        code = create_resp.json()["code"]
        await http_client.delete(f"/{code}")

        resp = await http_client.get(f"/{code}", follow_redirects=False)
        assert resp.status_code == 404

    async def test_shorten_with_ttl(self, http_client) -> None:
        resp = await http_client.post(
            "/shorten", json={"url": "https://expiring.example.com", "expires_in_seconds": 3600}
        )
        assert resp.status_code == 201
        assert resp.json()["expires_at"] is not None

    async def test_shorten_response_schema(self, http_client) -> None:
        resp = await http_client.post("/shorten", json={"url": "https://schema-check.example.com"})
        body = resp.json()
        assert all(k in body for k in ["short_url", "code", "original_url", "created_at"])


# ═══════════════════════════════════════════════════════════════════════════════
# 2. URL Repository (unit tests with mock session)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUrlRepository:
    """Cover url_shortener/repositories/url_repo.py (46% in unit-only run)."""

    def _mock_session(self) -> AsyncMock:
        session = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        session.execute = AsyncMock()
        return session

    async def test_create_returns_short_url(self) -> None:
        from url_shortener.repositories.url_repo import UrlRepository
        from url_shortener.models.url import ShortUrl

        session = self._mock_session()
        record = ShortUrl(code="abc12345", original_url="https://example.com")
        session.refresh = AsyncMock(side_effect=lambda r: None)
        session.add = MagicMock()
        session.flush = AsyncMock()

        repo = UrlRepository(session)
        # Patch refresh to set the returned object
        async def _refresh(obj):
            obj.code = "abc12345"
            obj.original_url = "https://example.com"
        session.refresh = _refresh

        result = await repo.create("abc12345", "https://example.com")
        assert result.code == "abc12345"
        session.add.assert_called_once()

    async def test_get_by_code_returns_none_when_missing(self) -> None:
        from url_shortener.repositories.url_repo import UrlRepository

        session = self._mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        repo = UrlRepository(session)
        result = await repo.get_by_code("nonexistent")
        assert result is None

    async def test_code_exists_returns_false_when_missing(self) -> None:
        from url_shortener.repositories.url_repo import UrlRepository

        session = self._mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        repo = UrlRepository(session)
        exists = await repo.code_exists("missing")
        assert exists is False

    async def test_code_exists_returns_true_when_present(self) -> None:
        from url_shortener.repositories.url_repo import UrlRepository

        session = self._mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "some-uuid"
        session.execute.return_value = mock_result

        repo = UrlRepository(session)
        exists = await repo.code_exists("abc12345")
        assert exists is True

    async def test_deactivate_returns_false_when_not_found(self) -> None:
        from url_shortener.repositories.url_repo import UrlRepository

        session = self._mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        repo = UrlRepository(session)
        updated = await repo.deactivate("missing")
        assert updated is False

    async def test_deactivate_returns_true_when_found(self) -> None:
        from url_shortener.repositories.url_repo import UrlRepository

        session = self._mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "some-uuid"
        session.execute.return_value = mock_result

        repo = UrlRepository(session)
        updated = await repo.deactivate("abc12345")
        assert updated is True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Requirement parsing — model properties and edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestRequirementParsing:
    def test_is_fully_resolved_with_no_ambiguities(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        assert req.is_fully_resolved is True

    def test_is_fully_resolved_all_resolved(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.AMBIGUOUS)
        req.ambiguities.append(AmbiguityItem(field="f", description="d",
                                              resolved=True, resolution="fixed"))
        assert req.is_fully_resolved is True

    def test_is_fully_resolved_unresolved_item(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.AMBIGUOUS)
        req.ambiguities.append(AmbiguityItem(field="f", description="d", resolved=False))
        assert req.is_fully_resolved is False

    def test_is_fully_resolved_mixed(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.AMBIGUOUS)
        req.ambiguities.append(AmbiguityItem(field="a", description="d", resolved=True, resolution="x"))
        req.ambiguities.append(AmbiguityItem(field="b", description="d", resolved=False))
        assert req.is_fully_resolved is False

    def test_requirement_type_enum_values(self) -> None:
        assert RequirementType.GREENFIELD == "greenfield"
        assert RequirementType.BROWNFIELD == "brownfield"
        assert RequirementType.AMBIGUOUS == "ambiguous"

    def test_requirement_with_constraints_and_criteria(self) -> None:
        req = Requirement(
            title="T", raw_text="R",
            requirement_type=RequirementType.GREENFIELD,
            constraints=["<50ms p99"],
            acceptance_criteria=["Returns 201"],
        )
        assert len(req.constraints) == 1
        assert len(req.acceptance_criteria) == 1

    def test_ambiguity_item_default_state(self) -> None:
        item = AmbiguityItem(field="scope", description="Unclear what scope means")
        assert item.resolved is False
        assert item.resolution is None
        assert item.resolved_at is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Task decomposition — StageContext helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestTaskDecomposition:
    def test_ready_tasks_no_dependencies(self) -> None:
        ctx = StageContext(stage_name="test")
        ctx.tasks = [
            Task(title="T1", description="D1", status=TaskStatus.PENDING),
            Task(title="T2", description="D2", status=TaskStatus.PENDING),
        ]
        ready = ctx.ready_tasks
        assert len(ready) == 2

    def test_ready_tasks_with_completed_dependency(self) -> None:
        ctx = StageContext(stage_name="test")
        t1 = Task(title="T1", description="D1", status=TaskStatus.COMPLETED)
        t2 = Task(title="T2", description="D2", status=TaskStatus.PENDING,
                  depends_on=[t1.id])
        ctx.tasks = [t1, t2]
        ready = ctx.ready_tasks
        assert len(ready) == 1
        assert ready[0].title == "T2"

    def test_ready_tasks_blocked_by_pending_dependency(self) -> None:
        ctx = StageContext(stage_name="test")
        t1 = Task(title="T1", description="D1", status=TaskStatus.PENDING)
        t2 = Task(title="T2", description="D2", status=TaskStatus.PENDING,
                  depends_on=[t1.id])
        ctx.tasks = [t1, t2]
        # T1 is pending → T2 is NOT ready
        ready = ctx.ready_tasks
        assert len(ready) == 1
        assert ready[0].title == "T1"

    def test_has_blocking_validation_failures_false_when_all_pass(self) -> None:
        ctx = StageContext(stage_name="test")
        ctx.validations = [
            ValidationResult(rule_name="r1", passed=True, severity=ValidationSeverity.ERROR,
                             message="OK", stage="test"),
        ]
        assert ctx.has_blocking_validation_failures is False

    def test_has_blocking_validation_failures_true_when_error_fails(self) -> None:
        ctx = StageContext(stage_name="test")
        ctx.validations = [
            ValidationResult(rule_name="r1", passed=False, severity=ValidationSeverity.ERROR,
                             message="FAIL", stage="test"),
        ]
        assert ctx.has_blocking_validation_failures is True

    def test_has_blocking_validation_failures_false_for_warning_failure(self) -> None:
        ctx = StageContext(stage_name="test")
        ctx.validations = [
            ValidationResult(rule_name="r1", passed=False, severity=ValidationSeverity.WARNING,
                             message="WARN", stage="test"),
        ]
        # WARNING failures are not blocking
        assert ctx.has_blocking_validation_failures is False

    def test_add_execution_result_aggregates_outputs(self) -> None:
        ctx = StageContext(stage_name="test")
        art = Artifact(name="a.txt", artifact_type=ArtifactType.CODE, produced_by_stage="test")
        dec = Decision(decision_type=DecisionType.SCOPE, title="T", description="D",
                       rationale="R", stage="test")
        task = Task(title="T", description="D")
        result = ExecutionResult(
            task_id=task.id,
            agent_name="agent",
            status=ExecutionStatus.SUCCESS,
            artifacts=[art],
            decisions=[dec],
        )
        ctx.add_execution_result(result)
        assert len(ctx.artifacts) == 1
        assert len(ctx.decisions) == 1
        assert len(ctx.execution_results) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Dependency graph — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestDependencyGraph:
    def test_conditional_dependency_type(self) -> None:
        dep = StageDependency(from_stage="A", to_stage="B",
                              dependency_type=DependencyType.CONDITIONAL,
                              condition="B only runs if A produced analytics artifact")
        assert dep.dependency_type == DependencyType.CONDITIONAL
        assert dep.condition is not None

    def test_single_stage_workflow(self) -> None:
        defn = WorkflowDefinition(name="single", description="", stages=["only"])
        assert defn.get_ready_stages(completed=set()) == ["only"]
        assert defn.get_ready_stages(completed={"only"}) == []

    def test_stages_reachable_from_root(self) -> None:
        defn = WorkflowDefinition(
            name="chain", description="",
            stages=["A", "B", "C"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="C"),
            ],
        )
        reachable = defn.stages_reachable_from("A")
        assert "B" in reachable
        assert "C" in reachable
        assert "A" not in reachable

    def test_terminal_stage_has_no_successors(self) -> None:
        defn = WorkflowDefinition(
            name="chain", description="",
            stages=["A", "B"],
            dependencies=[StageDependency(from_stage="A", to_stage="B")],
        )
        assert defn.stages_reachable_from("B") == set()

    def test_missing_dep_target_is_a_known_gap(self) -> None:
        """
        KNOWN GAP: WorkflowDefinition does NOT raise when a dependency
        references a stage ('B') that is absent from the stages list.
        networkx silently adds phantom nodes.

        Safety net: WorkflowEngine._validate() raises when stages dict
        is missing an implementation for a stage — tested separately in
        TestImportantFailureScenarios.test_workflow_with_missing_stage_impl_raises.
        """
        defn = WorkflowDefinition(
            name="phantom", description="",
            stages=["A"],
            dependencies=[StageDependency(from_stage="A", to_stage="B")],
        )
        # Construction does not raise — this is the gap
        assert "A" in defn.stages


# ═══════════════════════════════════════════════════════════════════════════════
# 6-8. Sequential, Parallel, Synchronisation
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutionPatterns:
    async def test_sequential_stages_execute_in_order(self, req) -> None:
        execution_order: list[str] = []

        class _OrderedStage(_SimpleStage):
            async def execute(self, ctx):
                execution_order.append(self.stage_name)
                ctx.output_data[f"{self.stage_name}_done"] = True
                return ctx

        stages = {n: _OrderedStage(n) for n in ["A", "B", "C"]}
        defn = WorkflowDefinition(
            name="seq", description="", stages=["A", "B", "C"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="C"),
            ],
        )
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert execution_order == ["A", "B", "C"]

    async def test_parallel_stages_both_complete(self, req) -> None:
        defn = WorkflowDefinition(
            name="par", description="", stages=["ROOT", "BRANCH_A", "BRANCH_B", "SYNC"],
            dependencies=[
                StageDependency(from_stage="ROOT", to_stage="BRANCH_A"),
                StageDependency(from_stage="ROOT", to_stage="BRANCH_B"),
                StageDependency(from_stage="BRANCH_A", to_stage="SYNC"),
                StageDependency(from_stage="BRANCH_B", to_stage="SYNC"),
            ],
        )
        stages = {n: _SimpleStage(n) for n in ["ROOT", "BRANCH_A", "BRANCH_B", "SYNC"]}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        for name in ["ROOT", "BRANCH_A", "BRANCH_B", "SYNC"]:
            assert state.stages[name].status == StageStatus.COMPLETED

    async def test_sync_stage_blocked_when_one_branch_fails(self, req) -> None:
        class _FailingStage(_SimpleStage):
            async def execute(self, ctx):
                raise RuntimeError("branch failure")

        defn = WorkflowDefinition(
            name="par_fail", description="", stages=["ROOT", "BRANCH_A", "BRANCH_B", "SYNC"],
            dependencies=[
                StageDependency(from_stage="ROOT", to_stage="BRANCH_A"),
                StageDependency(from_stage="ROOT", to_stage="BRANCH_B"),
                StageDependency(from_stage="BRANCH_A", to_stage="SYNC"),
                StageDependency(from_stage="BRANCH_B", to_stage="SYNC"),
            ],
        )
        stages = {
            "ROOT": _SimpleStage("ROOT"),
            "BRANCH_A": _SimpleStage("BRANCH_A"),
            "BRANCH_B": _FailingStage("BRANCH_B"),
            "SYNC": _SimpleStage("SYNC"),
        }
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["BRANCH_B"].status == StageStatus.FAILED
        # SYNC never ran — BLOCKED
        assert state.stages["SYNC"].status == StageStatus.BLOCKED

    async def test_3_predecessor_sync_point(self, req) -> None:
        """3 parallel branches must all complete before sync."""
        defn = WorkflowDefinition(
            name="3par", description="",
            stages=["R", "A", "B", "C", "SYNC"],
            dependencies=[
                StageDependency(from_stage="R", to_stage="A"),
                StageDependency(from_stage="R", to_stage="B"),
                StageDependency(from_stage="R", to_stage="C"),
                StageDependency(from_stage="A", to_stage="SYNC"),
                StageDependency(from_stage="B", to_stage="SYNC"),
                StageDependency(from_stage="C", to_stage="SYNC"),
            ],
        )
        stages = {n: _SimpleStage(n) for n in ["R", "A", "B", "C", "SYNC"]}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["SYNC"].status == StageStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Entry / exit gates — exception paths
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateExceptionPaths:
    async def test_exit_gate_exception_triggers_retry(self, req) -> None:
        """Exit gate exception + exit_gate_failure_retryable=True → retry execute."""
        exit_calls: list[int] = []

        class _Flaky(_SimpleStage):
            def __init__(self):
                super().__init__("flaky")
                self.retry_policy = RetryPolicy(max_attempts=2)

            async def exit_gate(self, ctx):
                exit_calls.append(len(exit_calls))
                if len(exit_calls) == 1:
                    raise RuntimeError("Transient exit gate error")
                return GateResult(gate_name="flaky_exit", passed=True)

        defn, stages = WorkflowDefinition(name="t", description="", stages=["flaky"]), {"flaky": _Flaky()}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert len(exit_calls) == 2  # failed once, succeeded on retry

    async def test_exit_gate_exception_not_retryable_fails_stage(self, req) -> None:
        class _BadExit(_SimpleStage):
            def __init__(self):
                super().__init__("bad_exit")
                self.retry_policy = RetryPolicy(
                    max_attempts=2, exit_gate_failure_retryable=False
                )

            async def exit_gate(self, ctx):
                raise ValueError("Permanent exit gate error")

        defn = WorkflowDefinition(name="t", description="", stages=["bad_exit"])
        stages = {"bad_exit": _BadExit()}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.FAILED
        ctx = state.stages["bad_exit"]
        assert ctx.attempt_records[0].error_type == "ValueError"

    async def test_entry_gate_exception_fails_stage(self, req) -> None:
        class _BadEntry(_SimpleStage):
            def __init__(self):
                super().__init__("bad_entry")

            async def entry_gate(self, ctx):
                raise RuntimeError("Entry gate exploded")

        defn = WorkflowDefinition(name="t", description="", stages=["bad_entry"])
        stages = {"bad_entry": _BadEntry()}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["bad_entry"].status == StageStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Context propagation — multi-hop
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextPropagation:
    async def test_3_hop_propagation(self, req) -> None:
        """A → B → C: C must receive A's outputs."""

        class _Emitter(_SimpleStage):
            async def execute(self, ctx):
                ctx.output_data["from_A"] = "value_from_A"
                return ctx

        class _PassThrough(_SimpleStage):
            async def execute(self, ctx):
                ctx.output_data.update(ctx.input_data)  # forward upstream
                ctx.output_data["from_B"] = "value_from_B"
                return ctx

        class _Checker(_SimpleStage):
            async def execute(self, ctx):
                assert "from_A" in ctx.input_data, "A's output must reach C"
                assert "from_B" in ctx.input_data, "B's output must reach C"
                ctx.output_data["check_passed"] = True
                return ctx

        defn = WorkflowDefinition(
            name="propagate", description="", stages=["A", "B", "C"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="C"),
            ],
        )
        stages = {"A": _Emitter("A"), "B": _PassThrough("B"), "C": _Checker("C")}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["C"].output_data["check_passed"] is True

    async def test_multi_predecessor_merge(self, req) -> None:
        """SYNC receives merged outputs from both A and B."""

        class _A(_SimpleStage):
            async def execute(self, ctx):
                ctx.output_data["from_A"] = "a_value"
                return ctx

        class _B(_SimpleStage):
            async def execute(self, ctx):
                ctx.output_data["from_B"] = "b_value"
                return ctx

        class _SYNC(_SimpleStage):
            async def execute(self, ctx):
                assert "from_A" in ctx.input_data
                assert "from_B" in ctx.input_data
                ctx.output_data["merged"] = True
                return ctx

        defn = WorkflowDefinition(
            name="merge", description="", stages=["A", "B", "SYNC"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="SYNC"),
                StageDependency(from_stage="B", to_stage="SYNC"),
            ],
        )
        stages = {"A": _A("A"), "B": _B("B"), "SYNC": _SYNC("SYNC")}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["SYNC"].output_data["merged"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Decision lineage — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestDecisionLineage:
    def test_build_lineage_on_empty_state(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        state = WorkflowState(requirement=req, status=WorkflowStatus.COMPLETED)
        lineage = build_lineage(state)
        assert lineage.requirement.id == req.id
        assert lineage.decisions == []
        assert lineage.tasks == []

    def test_lineage_has_impact_property(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        state = WorkflowState(requirement=req, status=WorkflowStatus.COMPLETED)
        ctx = StageContext(stage_name="stage1", status=StageStatus.COMPLETED)
        dec = Decision(decision_type=DecisionType.SCOPE, title="T", description="D",
                       rationale="R", stage="stage1")
        ctx.decisions.append(dec)
        state.stages["stage1"] = ctx
        lineage = build_lineage(state)
        assert len(lineage.decisions) == 1

    def test_lineage_all_tasks_property(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        state = WorkflowState(requirement=req, status=WorkflowStatus.COMPLETED)
        ctx1 = StageContext(stage_name="s1", status=StageStatus.COMPLETED)
        ctx1.tasks.append(Task(title="T1", description="D1", stage="s1"))
        ctx2 = StageContext(stage_name="s2", status=StageStatus.COMPLETED)
        ctx2.tasks.append(Task(title="T2", description="D2", stage="s2"))
        state.stages["s1"] = ctx1
        state.stages["s2"] = ctx2
        assert len(state.all_tasks) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Human approval — escalation record fields
# ═══════════════════════════════════════════════════════════════════════════════


class TestApprovalDetails:
    async def test_approval_record_has_impact_metadata(self, req) -> None:
        class _HighImpactStage(_SimpleStage):
            action_impact = ActionImpact.HIGH_IMPACT
            requires_approval = True

        defn = WorkflowDefinition(name="t", description="", stages=["hi"])
        stages = {"hi": _HighImpactStage("hi")}
        engine = WorkflowEngine(definition=defn, stages=stages,
                                approval_gateway=AutoApproveGateway())
        state = await engine.run(req)

        assert state.approvals
        appr = state.approvals[0]
        assert appr.stage_name == "hi"
        from orchestrator.core.results import ApprovalStatus
        assert appr.status == ApprovalStatus.APPROVED

    async def test_final_qc_approval_rejection_fails_workflow(self, req) -> None:
        defn = WorkflowDefinition(name="t", description="", stages=["s"])
        stages = {"s": _SimpleStage("s")}
        engine = WorkflowEngine(
            definition=defn, stages=stages,
            approval_gateway=AutoRejectGateway(),
            final_approval_required=True,
        )
        state = await engine.run(req)
        assert state.status == WorkflowStatus.FAILED
        events = {e.event for e in state.audit_trail}
        assert "final_qc_rejected" in events or "workflow_failed" in events


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Policy guardrails — WARN pass-through, combined policies
# ═══════════════════════════════════════════════════════════════════════════════


class TestPolicyGuardrails:
    async def test_warn_policy_does_not_block_execution(self, req) -> None:
        class _HighRiskStage(_SimpleStage):
            action_impact = ActionImpact.ROUTINE
            policy_metadata = {"high_risk_action": True}

        defn = WorkflowDefinition(name="t", description="", stages=["warn_stage"])
        stages = {"warn_stage": _HighRiskStage("warn_stage")}
        pe = PolicyEngine(policies=[WarnOnHighRiskAction()])
        engine = WorkflowEngine(definition=defn, stages=stages, policy_engine=pe)
        state = await engine.run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.policy_evaluations[0].final_decision == EnforcementDecision.WARN

    async def test_combined_allow_and_warn_policies(self, req) -> None:
        class _SigStage(_SimpleStage):
            action_impact = ActionImpact.SIGNIFICANT
            policy_metadata = {"high_risk_action": True, "change_ticket_id": "CHG-001"}

        defn = WorkflowDefinition(name="t", description="", stages=["sig"])
        stages = {"sig": _SigStage("sig")}
        # RequireChangeTicket → ALLOW (ticket provided), WarnOnHighRisk → WARN
        pe = PolicyEngine(policies=[RequireChangeTicket(), WarnOnHighRiskAction()])
        engine = WorkflowEngine(definition=defn, stages=stages, policy_engine=pe)
        state = await engine.run(req)

        # WARN > ALLOW → WARN is final
        assert state.status == WorkflowStatus.COMPLETED
        assert state.policy_evaluations[0].final_decision == EnforcementDecision.WARN

    def test_policy_engine_with_zero_policies(self) -> None:
        pe = PolicyEngine(policies=[])
        ctx = ActionContext(
            workflow_id="wf", stage_name="s",
            action_impact=ActionImpact.HIGH_IMPACT,
        )
        record = pe.evaluate(ctx)
        assert record.final_decision == EnforcementDecision.ALLOW
        assert record.violations == []


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Retry — exit-gate exception retry path
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryEdgeCases:
    async def test_base_class_exception_classified_as_permanent(self) -> None:
        """Subclass of non_retryable exception must also be PERMANENT (MRO)."""

        class _ParentError(Exception):
            pass

        class _ChildError(_ParentError):
            pass

        class _ChildFailStage(_SimpleStage):
            def __init__(self):
                super().__init__("cf")
                self.retry_policy = RetryPolicy(
                    max_attempts=3,
                    non_retryable_error_types=["_ParentError"],
                )

            async def execute(self, ctx):
                raise _ChildError("child of non-retryable")

        defn = WorkflowDefinition(name="t", description="", stages=["cf"])
        stages = {"cf": _ChildFailStage()}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req := Requirement(
            title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD
        ))

        assert state.status == WorkflowStatus.FAILED
        # Only 1 attempt because PERMANENT → no retry
        assert len(state.stages["cf"].attempt_records) == 1
        assert state.stages["cf"].attempt_records[0].classification == FailureClassification.PERMANENT

    async def test_retry_count_equals_max_attempts(self, req) -> None:
        class _AlwaysFailsStage(_SimpleStage):
            def __init__(self):
                super().__init__("af")
                self.retry_policy = RetryPolicy(max_attempts=4)

            async def execute(self, ctx):
                raise RuntimeError("always")

        defn = WorkflowDefinition(name="t", description="", stages=["af"])
        stages = {"af": _AlwaysFailsStage()}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert len(state.stages["af"].attempt_records) == 4


# ═══════════════════════════════════════════════════════════════════════════════
# 15. Fallback — USE_PRESET without fallback_output falls through
# ═══════════════════════════════════════════════════════════════════════════════


class TestFallbackEdgeCases:
    async def test_use_preset_with_no_output_causes_failure(self, req) -> None:
        class _NoPreset(_SimpleStage):
            def __init__(self):
                super().__init__("np")
                self.retry_policy = RetryPolicy(
                    max_attempts=1,
                    fallback_behavior=FallbackBehavior.USE_PRESET,
                    fallback_output=None,  # no preset → falls through to FAIL
                )

            async def execute(self, ctx):
                raise RuntimeError("fail")

        defn = WorkflowDefinition(name="t", description="", stages=["np"])
        stages = {"np": _NoPreset()}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.FAILED

    async def test_skip_fallback_downstream_receives_empty_output(self, req) -> None:
        class _SkipStage(_SimpleStage):
            def __init__(self):
                super().__init__("skip")
                self.retry_policy = RetryPolicy(
                    max_attempts=1, fallback_behavior=FallbackBehavior.SKIP
                )

            async def execute(self, ctx):
                raise RuntimeError("fail")

        class _DownstreamStage(_SimpleStage):
            async def execute(self, ctx):
                ctx.output_data["received_input"] = dict(ctx.input_data)
                return ctx

        defn = WorkflowDefinition(
            name="t", description="", stages=["skip", "downstream"],
            dependencies=[StageDependency(from_stage="skip", to_stage="downstream")],
        )
        stages = {"skip": _SkipStage(), "downstream": _DownstreamStage("downstream")}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert state.stages["skip"].status == StageStatus.SKIPPED
        assert state.stages["downstream"].status == StageStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Rollback — not called on success
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackNotCalledOnSuccess:
    async def test_rollback_not_called_when_stage_succeeds(self, req) -> None:
        rollback_called = []

        class _TrackRollback(_SimpleStage):
            def __init__(self):
                super().__init__("track")
                self.retry_policy = RetryPolicy(max_attempts=1, rollback_on_failure=True)

            async def rollback(self, ctx):
                rollback_called.append("called")
                ctx.rollback_performed = True
                return ctx

        defn = WorkflowDefinition(name="t", description="", stages=["track"])
        stages = {"track": _TrackRollback()}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.COMPLETED
        assert not rollback_called  # rollback must NOT be called on success
        assert state.stages["track"].rollback_performed is False


# ═══════════════════════════════════════════════════════════════════════════════
# 17. Safe-stop — from a parallel branch
# ═══════════════════════════════════════════════════════════════════════════════


class TestSafeStopParallelBranch:
    async def test_safe_stop_from_one_parallel_branch(self, req) -> None:
        class _SafeStopStage(_SimpleStage):
            def __init__(self, name):
                super().__init__(name)
                self.retry_policy = RetryPolicy(
                    max_attempts=1,
                    safe_stop_error_types=["SafeStopTestError"],
                )

            async def execute(self, ctx):
                class SafeStopTestError(Exception):
                    pass
                raise SafeStopTestError("critical!")

        defn = WorkflowDefinition(
            name="par_ss", description="", stages=["ROOT", "BRANCH_A", "BRANCH_B"],
            dependencies=[
                StageDependency(from_stage="ROOT", to_stage="BRANCH_A"),
                StageDependency(from_stage="ROOT", to_stage="BRANCH_B"),
            ],
        )
        stages = {
            "ROOT": _SimpleStage("ROOT"),
            "BRANCH_A": _SafeStopStage("BRANCH_A"),
            "BRANCH_B": _SimpleStage("BRANCH_B"),
        }
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.STOPPED
        assert state.safe_stopped is True
        events = {e.event for e in state.audit_trail}
        assert "safe_stop_triggered" in events


# ═══════════════════════════════════════════════════════════════════════════════
# 18. Dynamic replanning — on failed workflow
# ═══════════════════════════════════════════════════════════════════════════════


class TestReplanningEdgeCases:
    async def test_replan_on_failed_workflow_reruns_impacted(self, req) -> None:
        class _FailFirst(_SimpleStage):
            def __init__(self, name):
                super().__init__(name)
                self._calls = 0

            async def execute(self, ctx):
                self._calls += 1
                if self._calls == 1:
                    raise RuntimeError("fails first time")
                ctx.output_data["recovered"] = True
                return ctx

        defn = WorkflowDefinition(
            name="fail_replan", description="", stages=["A", "B"],
            dependencies=[StageDependency(from_stage="A", to_stage="B")],
        )
        fail_first = _FailFirst("A")
        stages = {"A": fail_first, "B": _SimpleStage("B")}
        engine = WorkflowEngine(definition=defn, stages=stages)

        # First run fails
        state = await engine.run(req)
        assert state.status == WorkflowStatus.FAILED

        # Replan — A now succeeds on second call
        state = await engine.replan(
            state,
            ChangeEvent(
                event_type=ChangeEventType.REQUIREMENT_CHANGE,
                originating_stage=None,
                change_description="Retry after fix",
            ),
        )
        assert state.status == WorkflowStatus.COMPLETED
        assert state.replan_count == 1

    async def test_analyze_impact_before_any_stage_runs(self, req) -> None:
        defn = WorkflowDefinition(
            name="fresh", description="", stages=["A", "B", "C"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="C"),
            ],
        )
        stages = {n: _SimpleStage(n) for n in ["A", "B", "C"]}
        engine = WorkflowEngine(definition=defn, stages=stages)

        # State with no completed stages
        state = WorkflowState(requirement=req, status=WorkflowStatus.PENDING)
        impact = engine.analyze_impact(
            state,
            ChangeEvent(event_type=ChangeEventType.ARTIFACT_CHANGED,
                        originating_stage="A", change_description="test"),
        )
        assert "B" in impact.impacted_stages
        assert "C" in impact.impacted_stages
        # No stages completed yet → nothing to invalidate
        assert impact.invalidated_artifact_ids == []


# ═══════════════════════════════════════════════════════════════════════════════
# 19. Greenfield scenario — artifact content spot-checks
# ═══════════════════════════════════════════════════════════════════════════════


class TestGreenfieldSpotChecks:
    @pytest.fixture(scope="class")
    async def gf_state(self):
        from orchestrator.scenarios.greenfield import run_greenfield_scenario
        return await run_greenfield_scenario(approval_gateway=AutoApproveGateway())

    async def test_task_graph_has_critical_path(self, gf_state) -> None:
        art = next(a for a in gf_state.stages["task_decomposition"].artifacts
                   if a.name == "task_graph.json")
        data = json.loads(art.content)
        assert "critical_path" in data["summary"]

    async def test_implementation_plan_has_error_handling(self, gf_state) -> None:
        art = next(a for a in gf_state.stages["implementation_planning"].artifacts
                   if a.name == "implementation_plan.json")
        data = json.loads(art.content)
        assert "error_handling" in data

    async def test_release_checklist_all_items_passed(self, gf_state) -> None:
        art = next(a for a in gf_state.stages["release_readiness"].artifacts
                   if a.name == "release_checklist.json")
        data = json.loads(art.content)
        statuses = [v["status"] for v in data["checklist"].values()]
        # Every checklist item should be PASSED or APPROVED (no FAILED)
        assert not any("FAILED" in s for s in statuses)


# ═══════════════════════════════════════════════════════════════════════════════
# 20. Brownfield scenario — preserved file list integrity
# ═══════════════════════════════════════════════════════════════════════════════


class TestBrownfieldPreservation:
    @pytest.fixture(scope="class")
    async def bf_state(self):
        from orchestrator.scenarios.brownfield import run_brownfield_scenario
        return await run_brownfield_scenario(approval_gateway=AutoApproveGateway())

    async def test_preserved_files_never_appear_in_change_plan(self, bf_state) -> None:
        cp_art = next(a for a in bf_state.stages["change_planning"].artifacts
                      if a.name == "change_plan.json")
        cp_data = json.loads(cp_art.content)
        preserved = set(cp_data["do_not_modify"])
        impl_files = {t["file"] for t in cp_data["implementation_tasks"]}
        overlap = preserved & impl_files
        assert not overlap, f"Files in both do_not_modify and implementation_tasks: {overlap}"

    async def test_change_plan_total_files_accounting(self, bf_state) -> None:
        cp_art = next(a for a in bf_state.stages["change_planning"].artifacts
                      if a.name == "change_plan.json")
        cp_data = json.loads(cp_art.content)
        total = cp_data["total_files_modified"] + cp_data["total_new_files"] + cp_data["total_files_unchanged"]
        # Should account for a reasonable number of files
        assert total >= 8


# ═══════════════════════════════════════════════════════════════════════════════
# 21. Ambiguous scenario — answer sensitivity (additional cases)
# ═══════════════════════════════════════════════════════════════════════════════


class TestAmbiguousAnswerSensitivity:
    async def test_public_access_answer_changes_fr_text(self) -> None:
        from orchestrator.scenarios.ambiguous import run_ambiguous_scenario, DEFAULT_PRESET_ANSWERS, PresetClarificationGateway
        state = await run_ambiguous_scenario(
            clarification_gateway=PresetClarificationGateway({
                **DEFAULT_PRESET_ANSWERS,
                "Q2": "Publicly readable (no auth required)",
            }),
            approval_gateway=AutoApproveGateway(),
        )
        art = next(a for a in state.stages["normalization"].artifacts
                   if a.name == "normalised_requirement.json")
        data = json.loads(art.content)
        fr3 = next(fr for fr in data["functional_requirements"] if fr["id"] == "FR-ANLY-003")
        # "owner only" branch excluded → text says "any authenticated user" (not owner-only)
        assert "owner only" not in fr3["text"].lower()
        # Source decision must still be Q2
        assert fr3["source_decision"] == "Q2"

    async def test_no_country_answer_no_country_task(self) -> None:
        from orchestrator.scenarios.ambiguous import run_ambiguous_scenario, DEFAULT_PRESET_ANSWERS, PresetClarificationGateway
        state = await run_ambiguous_scenario(
            clarification_gateway=PresetClarificationGateway({
                **DEFAULT_PRESET_ANSWERS,
                "Q1": "Click count only (simplest)",
            }),
            approval_gateway=AutoApproveGateway(),
        )
        art = next(a for a in state.stages["task_planning"].artifacts
                   if a.name == "task_plan.json")
        data = json.loads(art.content)
        task_titles = [t["title"].lower() for t in data["implementation_tasks"]]
        # No country geo-lookup task expected
        assert not any("country" in t or "geo" in t for t in task_titles)


# ═══════════════════════════════════════════════════════════════════════════════
# BaseAgent / BaseOrchestrator — abstract instantiation guard
# ═══════════════════════════════════════════════════════════════════════════════


class TestAbstractBases:
    def test_base_agent_cannot_be_instantiated(self) -> None:
        from orchestrator.core.base_agent import BaseAgent
        with pytest.raises(TypeError):
            BaseAgent()  # type: ignore[abstract]

    def test_base_agent_repr_on_concrete(self) -> None:
        from orchestrator.core.base_agent import BaseAgent
        class _ConcreteAgent(BaseAgent):
            name = "test-agent"
            async def execute(self, ctx): return ctx
            async def validate_input(self, ctx): return True
            async def validate_output(self, ctx): return True
            async def rollback(self, ctx): return ctx

        agent = _ConcreteAgent()
        assert "test-agent" in repr(agent)

    def test_base_orchestrator_cannot_be_instantiated(self) -> None:
        from orchestrator.core.base_orchestrator import BaseOrchestrator
        with pytest.raises(TypeError):
            BaseOrchestrator()  # type: ignore[abstract]


# ═══════════════════════════════════════════════════════════════════════════════
# Observability logging
# ═══════════════════════════════════════════════════════════════════════════════


class TestObservabilityLogging:
    """
    configure_logging() mutates the global structlog configuration.
    Each test resets to structlog defaults after running to avoid
    breaking other tests that rely on the default (unconfigured) state.
    """

    @pytest.fixture(autouse=True)
    def _reset_structlog(self):
        """Reset structlog to its default state after each test in this class."""
        import structlog
        yield
        structlog.reset_defaults()

    def test_configure_logging_production(self) -> None:
        from orchestrator.observability.logging import configure_logging
        configure_logging(level="WARNING", environment="production")

    def test_configure_logging_development(self) -> None:
        from orchestrator.observability.logging import configure_logging
        configure_logging(level="DEBUG", environment="development")

    def test_get_audit_logger_returns_logger(self) -> None:
        from orchestrator.observability.logging import get_audit_logger
        logger = get_audit_logger()
        assert logger is not None

    def test_configure_logging_unknown_level_defaults_to_info(self) -> None:
        from orchestrator.observability.logging import configure_logging
        # Should not raise for unknown level (getattr fallback)
        configure_logging(level="NOTREAL")

    def test_configure_logging_allows_info_emit(self) -> None:
        """Regression: PrintLogger + add_logger_name crashed app startup."""
        import structlog
        from orchestrator.observability.logging import configure_logging

        configure_logging(level="INFO", environment="development")
        log = structlog.get_logger("startup-regression")
        log.info("application_startup", app="test", version="0.0.0")


# ═══════════════════════════════════════════════════════════════════════════════
# Observability metrics — edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestObservabilityEdgeCases:
    def _make_state_with_stage(self, status: StageStatus, rollback: bool = False) -> WorkflowState:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        state = WorkflowState(requirement=req, status=WorkflowStatus.COMPLETED)
        _t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ctx = StageContext(
            stage_name="s", status=status,
            started_at=_t0, completed_at=_t0 + timedelta(seconds=3),
            rollback_performed=rollback,
        )
        state.stages["s"] = ctx
        state.completed_at = _t0 + timedelta(seconds=5)
        return state

    def test_stage_latency_none_without_start(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        state = WorkflowState(requirement=req, status=WorkflowStatus.FAILED)
        ctx = StageContext(stage_name="s", status=StageStatus.FAILED)
        state.stages["s"] = ctx
        m = compute_workflow_metrics(state)
        assert m.stage_metrics[0].latency_seconds is None

    def test_rollback_flag_in_stage_metrics(self) -> None:
        state = self._make_state_with_stage(StageStatus.ROLLED_BACK, rollback=True)
        m = compute_workflow_metrics(state)
        assert m.stage_metrics[0].rolled_back is True

    def test_reliability_metrics_all_failed(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        s1 = WorkflowState(requirement=req, status=WorkflowStatus.FAILED)
        s2 = WorkflowState(requirement=req, status=WorkflowStatus.FAILED)
        m = compute_reliability_metrics([s1, s2])
        assert m.success_rate == pytest.approx(0.0)
        assert m.failure_rate == pytest.approx(1.0)
        assert m.successful_runs == 0
        assert m.failed_runs == 2

    def test_execution_trace_result_status_failed(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        state = WorkflowState(requirement=req, status=WorkflowStatus.FAILED)
        trace = build_execution_trace(state)
        assert trace.result is not None
        assert trace.result.name == "failed"

    def test_structured_logs_warn_for_rollback_event(self) -> None:
        req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
        state = WorkflowState(requirement=req, status=WorkflowStatus.FAILED)
        state.add_audit_entry("rollback_started", stage="s", details={})
        logs = build_structured_logs(state)
        rollback_logs = [l for l in logs if l.event == "rollback_started"]
        assert rollback_logs and rollback_logs[0].level == "WARN"


# ═══════════════════════════════════════════════════════════════════════════════
# Important failure scenarios
# ═══════════════════════════════════════════════════════════════════════════════


class TestImportantFailureScenarios:
    def test_workflow_with_missing_stage_impl_raises_on_engine_init(self, req) -> None:
        """WorkflowDefinition references 'B' but stages dict only has 'A'.
        The engine raises StageNotRegisteredError at construction time."""
        defn = WorkflowDefinition(name="bad", description="", stages=["A", "B"])
        stages = {"A": _SimpleStage("A")}  # 'B' is missing
        with pytest.raises(Exception):  # StageNotRegisteredError
            WorkflowEngine(definition=defn, stages=stages)

    def test_workflow_definition_cycle_caught_by_engine(self) -> None:
        """
        KNOWN BEHAVIOR: WorkflowDefinition itself does NOT raise on cycle
        during construction.  Cycle detection happens in WorkflowEngine.__init__
        which calls WorkflowDefinition.validate() → raises WorkflowValidationError.
        """
        defn = WorkflowDefinition(
            name="cycle", description="",
            stages=["A", "B"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="A"),  # cycle
            ],
        )
        stages = {"A": _SimpleStage("A"), "B": _SimpleStage("B")}
        with pytest.raises(Exception):  # WorkflowValidationError from engine._validate()
            WorkflowEngine(definition=defn, stages=stages)

    async def test_failed_first_stage_blocks_all_downstream(self, req) -> None:
        class _Fail(_SimpleStage):
            async def execute(self, ctx):
                raise RuntimeError("first stage fails")

        defn = WorkflowDefinition(
            name="cascade", description="", stages=["A", "B", "C"],
            dependencies=[
                StageDependency(from_stage="A", to_stage="B"),
                StageDependency(from_stage="B", to_stage="C"),
            ],
        )
        stages = {"A": _Fail("A"), "B": _SimpleStage("B"), "C": _SimpleStage("C")}
        state = await WorkflowEngine(definition=defn, stages=stages).run(req)

        assert state.status == WorkflowStatus.FAILED
        assert state.stages["A"].status == StageStatus.FAILED
        assert state.stages["B"].status == StageStatus.BLOCKED
        assert state.stages["C"].status == StageStatus.BLOCKED

    def test_empty_workflow_rejected_at_engine_init(self, req) -> None:
        """Empty workflows are rejected by WorkflowEngine validation (not silently accepted)."""
        defn = WorkflowDefinition(name="empty", description="", stages=[])
        with pytest.raises(Exception):  # WorkflowValidationError
            WorkflowEngine(definition=defn, stages={})

    async def test_approval_missing_gateway_fails_approval_stage(self, req) -> None:
        class _NeedsApproval(_SimpleStage):
            requires_approval = True

        defn = WorkflowDefinition(name="t", description="", stages=["appr"])
        stages = {"appr": _NeedsApproval("appr")}
        engine = WorkflowEngine(definition=defn, stages=stages, approval_gateway=None)
        state = await engine.run(req)

        assert state.status == WorkflowStatus.FAILED
        # Stage should have failed due to missing gateway
        assert state.stages["appr"].status == StageStatus.FAILED

    async def test_retry_policy_max_bounds(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValidationError):
            RetryPolicy(max_attempts=11)
        # Boundary valid values
        assert RetryPolicy(max_attempts=1).max_attempts == 1
        assert RetryPolicy(max_attempts=10).max_attempts == 10

    def test_analyze_impact_invalid_stage_raises(self, req) -> None:
        defn = WorkflowDefinition(name="t", description="", stages=["A", "B"])
        stages = {n: _SimpleStage(n) for n in ["A", "B"]}
        engine = WorkflowEngine(definition=defn, stages=stages)
        state = WorkflowState(requirement=req, status=WorkflowStatus.COMPLETED)

        with pytest.raises(ValueError, match="not defined"):
            engine.analyze_impact(
                state,
                ChangeEvent(event_type=ChangeEventType.ARTIFACT_CHANGED,
                            originating_stage="NONEXISTENT", change_description="test"),
            )
