# Architecture: Agentic SDLC URL Shortener

> **Classification:** Schwab Internal — Interview Assignment  
> **Version:** 1.0.0 | **Status:** Complete prototype  
> **Tests:** 893 passing | **Coverage:** ≥80% required (~95% recent)  
> **Engineering summary:** [engineering-summary.md](engineering-summary.md) — scope, risks, limitations, assumptions, judgment

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Component Architecture](#2-component-architecture)
3. [Agent Architecture](#3-agent-architecture)
4. [Orchestration Model](#4-orchestration-model)
5. [Dependency Graph](#5-dependency-graph)
6. [Workflow Lifecycle](#6-workflow-lifecycle)
7. [Control Flow](#7-control-flow)
8. [Sequential Execution](#8-sequential-execution)
9. [Parallel Execution](#9-parallel-execution)
10. [Synchronization](#10-synchronization)
11. [Entry / Exit Gates](#11-entry--exit-gates)
12. [Context Propagation](#12-context-propagation)
13. [Decision Lineage](#13-decision-lineage)
14. [Human Approval Checkpoints](#14-human-approval-checkpoints)
15. [Governance & Policy Guardrails](#15-governance--policy-guardrails)
16. [Retry / Fallback / Rollback](#16-retry--fallback--rollback)
17. [Safe-Stop](#17-safe-stop)
18. [Dynamic Replanning](#18-dynamic-replanning)
19. [Observability](#19-observability)
20. [Reliability Metrics](#20-reliability-metrics)
21. [Security Considerations](#21-security-considerations)
22. [Testing Strategy](#22-testing-strategy)

---

## 1. System Overview

The system is a **two-layer architecture**: a general-purpose **Agentic SDLC Orchestration Engine** layered above a production-quality **URL Shortener Service**. The URL shortener is the *engineering artifact* that the orchestrator produces and manages across its lifecycle — it is not a wrapper around it.

```
┌──────────────────────────────────────────────────────────────┐
│               Agentic SDLC Orchestration Layer               │
│                                                              │
│  WorkflowEngine ──► DAG Executor ──► Stage implementations  │
│       │                                                      │
│  PolicyEngine · ApprovalGateway · ClarificationGateway      │
│  WorkflowLineage · ObservabilityReport · ReplanningEngine   │
└──────────────────────────┬───────────────────────────────────┘
                           │  produces / manages
┌──────────────────────────▼───────────────────────────────────┐
│                URL Shortener Service                         │
│                                                              │
│  FastAPI (ASGI) · SQLAlchemy 2 async · PostgreSQL 15        │
│  POST /shorten · GET /{code} · DELETE /{code} · GET /health │
└──────────────────────────────────────────────────────────────┘
```

**Design principle:** the orchestrator is stage-agnostic. Swap the URL shortener stages for any other engineering domain and the engine behaves identically.

---

## 2. Component Architecture

### 2.1 Orchestrator module map

```
orchestrator/
├── core/
│   ├── autonomy.py        AutonomyLevel, ActionImpact, ApprovalGateway, ApprovalPolicy
│   ├── base_agent.py      BaseAgent ABC (execute / validate_input / validate_output / rollback)
│   ├── base_orchestrator.py  BaseOrchestrator ABC (run / pause / resume / stop / get_state)
│   ├── base_stage.py      BaseStage ABC (entry_gate / execute / exit_gate / rollback)
│   ├── context.py         ExecutionContext — cross-stage output accumulation
│   ├── failure.py         RetryPolicy, FailureClassification, FallbackBehavior, StageAttemptRecord
│   ├── governance.py      PolicyEngine, Policy ABC, 7 built-in policies
│   ├── graph.py           WorkflowDefinition, StageDependency — networkx DAG
│   ├── lineage.py         WorkflowLineage, build_lineage() — provenance query interface
│   ├── models.py          Requirement, Task, StageContext, WorkflowState, AuditEntry
│   ├── observability.py   StructuredLogRecord, ExecutionTrace, WorkflowMetrics, ReliabilityMetrics
│   ├── replanning.py      ChangeEvent, ImpactAnalysis, ReplanResult
│   └── results.py         Artifact, Decision, Approval, ValidationResult, Risk
├── engine/
│   ├── task_scheduler.py  TaskScheduler — intra-stage task dependency ordering
│   └── workflow_engine.py WorkflowEngine — the concrete DAG executor
├── observability/
│   └── logging.py         configure_logging(), get_audit_logger() — structlog bootstrap
└── scenarios/
    ├── greenfield.py      8-stage SDLC pipeline (URL shortener from scratch)
    ├── brownfield.py      6-stage enhancement pipeline (rate limiting)
    └── ambiguous.py       5-stage clarification pipeline + ClarificationGateway
```

### 2.2 URL shortener module map

```
url_shortener/
├── main.py              FastAPI app factory; lifespan (DB pool); CORS; exception handlers
├── config.py            pydantic-settings Settings; all config from env vars
├── database.py          SQLAlchemy async engine + session factory; Base metadata
├── api/
│   ├── deps.py          get_db() async generator; get_url_service() factory
│   ├── exceptions.py    Exception → HTTP response handlers (404, 503)
│   └── urls.py          POST /shorten, GET /{code}, DELETE /{code} routers
├── models/
│   └── url.py           ShortUrl ORM (id, code, original_url, is_active, created_at, expires_at)
├── repositories/
│   └── url_repo.py      UrlRepository: create, get_by_code, code_exists, deactivate
├── schemas/
│   └── url.py           ShortenRequest (url, expires_in_seconds), ShortenResponse
└── services/
    ├── exceptions.py    ShortCodeNotFoundError, CodeGenerationError
    └── url_service.py   UrlService: shorten(), resolve(), deactivate()
```

### 2.3 Technology stack

| Concern | Technology | Version |
|---|---|---|
| Web framework | FastAPI + Uvicorn | ≥ 0.111 |
| ORM | SQLAlchemy async | 2.x |
| Database (production) | PostgreSQL | 15 |
| Database (tests) | SQLite in-memory (aiosqlite) | — |
| Data validation | Pydantic v2 | ≥ 2.7 |
| Settings | pydantic-settings | ≥ 2.0 |
| Structured logging | structlog | — |
| DAG execution | networkx | ≥ 3.0 |
| Async concurrency | asyncio (stdlib) | — |
| Migrations | Alembic | — |
| Test runner | pytest + pytest-asyncio | — |
| HTTP test client | httpx AsyncClient | — |

---

## 3. Agent Architecture

### 3.1 BaseAgent contract

Every SDLC agent must implement `BaseAgent`:

```
BaseAgent (ABC)
├── name: str                    — unique identifier (class attribute)
├── execute(ctx) → StageContext  — primary work; writes to ctx.output_data
├── validate_input(ctx) → bool   — called by entry gate; side-effect-free
├── validate_output(ctx) → bool  — called by exit gate; side-effect-free
└── rollback(ctx) → StageContext — undo side effects; called on failure
```

Agents are stateless workers. All execution state lives in `StageContext`.

### 3.2 BaseStage contract

Stages coordinate one or more agents through a gate-execute-gate lifecycle:

```
BaseStage (ABC)
├── stage_name: str                        — matches DAG node label
├── requires_approval: bool = False        — triggers approval checkpoint
├── action_impact: ActionImpact = ROUTINE  — drives autonomy policy
├── high_impact_action_type: ... = None    — approver context
├── retry_policy: RetryPolicy             — overrides engine default
├── policy_metadata: dict[str, Any]       — inputs for PolicyEngine
│
├── entry_gate(ctx) → GateResult          — precondition check
├── execute(ctx) → StageContext           — primary work
├── exit_gate(ctx) → GateResult           — quality check
└── rollback(ctx) → StageContext          — cleanup on failure
```

### 3.3 Stage → agent relationship

A stage owns the gate-execute-gate lifecycle. Within `execute()`, a stage may invoke zero, one, or many agents. In the current implementation, the three scenario modules (`greenfield.py`, `brownfield.py`, `ambiguous.py`) implement stages that simulate agent behavior directly — they produce structured artifacts without calling external services. In a production deployment, `execute()` would dispatch work to real AI agents (Copilot, Claude, etc.) and aggregate their outputs into the `StageContext`.

---

## 4. Orchestration Model

The orchestration model has five layers that compose to produce a governed, auditable, reversible SDLC execution:

```
┌─────────────────────────────────────────────────────┐
│  5. Observability  (structured logs + metrics)      │
├─────────────────────────────────────────────────────┤
│  4. Governance     (policy engine + approvals)      │
├─────────────────────────────────────────────────────┤
│  3. Recovery       (retry / fallback / rollback)    │
├─────────────────────────────────────────────────────┤
│  2. Execution      (DAG traversal + gates)          │
├─────────────────────────────────────────────────────┤
│  1. State          (WorkflowState + ExecutionContext)│
└─────────────────────────────────────────────────────┘
```

Each layer is implemented as a distinct module with no upward dependencies.

### 4.1 Key domain objects

| Object | Module | Purpose |
|---|---|---|
| `Requirement` | `models.py` | Root input: raw text, type (GREENFIELD / BROWNFIELD / AMBIGUOUS), ambiguities, acceptance criteria |
| `WorkflowDefinition` | `graph.py` | Immutable DAG template: stage names + dependency edges |
| `WorkflowState` | `models.py` | Mutable runtime state: all stage contexts, audit trail, approvals, lineage |
| `StageContext` | `models.py` | Per-stage state: input/output data, tasks, artifacts, decisions, validations, retry records |
| `ExecutionContext` | `context.py` | Cross-stage output accumulation: merged output_data, artifacts, decisions, risks |
| `WorkflowEngine` | `workflow_engine.py` | Concrete DAG executor: runs stages, manages recovery, governance, replanning |

---

## 5. Dependency Graph

### 5.1 Structure

`WorkflowDefinition` uses a **networkx `DiGraph`** as its backing store. An edge from node A to node B means: *A must complete before B may start.*

```python
WorkflowDefinition(
    stages=["A", "B", "C"],
    dependencies=[
        StageDependency(from_stage="A", to_stage="B"),
        StageDependency(from_stage="B", to_stage="C"),
    ],
)
```

### 5.2 Available topology queries

| Method | Returns |
|---|---|
| `get_ready_stages(completed)` | Stages with all predecessors in `completed`, not yet in progress |
| `get_predecessors(stage)` | Direct upstream stages |
| `get_all_predecessors(stage)` | Transitive ancestors (`nx.ancestors`) |
| `stages_reachable_from(stage)` | Transitive descendants (`nx.descendants`) — used for replanning |
| `topological_sort()` | Deterministic linear ordering |

### 5.3 Validation

`WorkflowDefinition` validates at construction time:
- At least one stage must exist
- No cycles (`nx.is_directed_acyclic_graph`)
- Each stage listed in `stages` must have an implementation in the `stages` dict passed to `WorkflowEngine`

Cycle detection is enforced by `WorkflowEngine.__init__` via `definition.validate()`. A `WorkflowValidationError` is raised — construction fails rather than producing a silently broken workflow.

### 5.4 Greenfield scenario DAG

```
requirements_analysis
        │
architecture_design ── [SIGNIFICANT · approval required]
        │
task_decomposition
   ┌────┴─────────────────┐
   │                      │
implementation_planning  testing_planning  documentation_planning
   │                      │                   │
   └──────────────────────┴───────────────────┘
                          │  (synchronisation)
                     validation
                          │
               release_readiness ── [HIGH_IMPACT · PRODUCTION_RELEASE · approval required]
```

### 5.5 Brownfield scenario DAG (linear)

```
codebase_analysis → impact_analysis → risk_assessment
    → change_planning (approval) → regression_test_planning → validation (approval)
```

### 5.6 Ambiguous scenario DAG (linear)

```
ambiguity_detection → clarification (pause) → normalization
    → task_planning → validation (approval)
```

---

## 6. Workflow Lifecycle

### 6.1 Status state machine

```
PENDING ──► RUNNING
                │
                ├──► COMPLETED           (all stages passed)
                ├──► FAILED              (any stage failed after exhausting recovery)
                ├──► STOPPED             (safe-stop triggered by CRITICAL exception)
                ├──► PAUSED              (operator-initiated; not yet implemented in engine)
                ├──► AWAITING_APPROVAL   (engine paused at approval checkpoint)
                └──► ROLLED_BACK         (workflow-level rollback recorded)
```

### 6.2 Stage status state machine

```
PENDING ──► AWAITING_GATE ──► AWAITING_APPROVAL ──► IN_PROGRESS
                │                                        │
                │ (entry gate fails)                      │
                ▼                                         │
              FAILED                              ┌───────┴────────┐
                                                  │                │
                                            COMPLETED         FAILED
                                                                │
                                                          ROLLED_BACK
                                                          SKIPPED (fallback)
                                                          BLOCKED (downstream)
```

### 6.3 WorkflowState fields

| Field | Type | Purpose |
|---|---|---|
| `id` | `str` (UUID) | Workflow execution ID — correlates all logs, metrics, and approvals |
| `requirement` | `Requirement` | Root input that initiated the workflow |
| `status` | `WorkflowStatus` | Current terminal or intermediate state |
| `stages` | `dict[str, StageContext]` | Per-stage execution records |
| `audit_trail` | `list[AuditEntry]` | Append-only event log |
| `approvals` | `list[Approval]` | All approval request/decision records |
| `stage_transitions` | `list[StageTransition]` | What enabled each stage to start |
| `policy_evaluations` | `list[PolicyEvaluationRecord]` | Governance gate results |
| `replan_history` | `list[ReplanResult]` | Every replan cycle's audit record |
| `rolled_back_stages` | `list[str]` | Stage names whose rollback() succeeded |
| `safe_stopped` | `bool` | True if workflow halted due to CRITICAL exception |
| `safe_stop_reason` | `str` | Human-readable safe-stop cause |
| `created_at` / `completed_at` | `datetime` | End-to-end latency anchors |

---

## 7. Control Flow

### 7.1 `WorkflowEngine.run()` — main execution loop

```
run(requirement):
    state = WorkflowState(requirement, status=RUNNING)
    exec_ctx = ExecutionContext(workflow_id=state.id)
    all_stages = set(definition.stages)
    completed = {}; failed = {}

    while True:
        ready = definition.get_ready_stages(completed)
        if not ready: break

        # Execute all ready stages concurrently
        results = asyncio.gather(*[_execute_stage(s, state, exec_ctx) for s in ready])

        for stage, result in zip(ready, results):
            (completed if result else failed).add(stage)

        if failed or state.safe_stopped: break

    # Optional final QC checkpoint (if final_approval_required=True)
    if not failed and not safe_stopped and final_approval_required:
        if not _final_qc_checkpoint(state, exec_ctx):
            failed.add("__final_qc__")

    _finalise(state, all_stages, completed, failed)
    state.completed_at = now()
    return state
```

### 7.2 `_execute_stage()` — per-stage control flow

```
_execute_stage(stage_name, state, exec_ctx):
    policy = stage_impl.retry_policy or default_retry_policy
    stage_ctx = StageContext(stage_name, status=AWAITING_GATE)

    ① Entry gate evaluation
       → FAIL: _fail_stage(); return False

    ② Stage transition record (lineage)

    ③ CRITICAL action block
       → if autonomy == HUMAN_ONLY: _fail_stage(); return False

    ④ Governance gate (if policy_engine configured)
       → BLOCK: _fail_stage("policy_blocked"); return False
       → REQUIRE_APPROVAL: policy_requires_approval = True
       → WARN / ALLOW: continue

    ⑤ Approval checkpoint (if requires_approval or policy or action_impact threshold)
       → rejected / no gateway: _fail_stage(); return False

    ⑥ Retry loop (1 .. policy.max_attempts):
       a. execute() — catch exception → classify TRANSIENT/PERMANENT/CRITICAL
          CRITICAL → _trigger_safe_stop(); return False
          PERMANENT or exhausted → break
          TRANSIENT → continue (retry)
       b. exit_gate() — failure → classify and retry or break

    ⑦ Post-retry: success path
       → update ExecutionContext; return True

    ⑧ Post-retry: failure path
       → try fallback (SKIP or USE_PRESET)
       → if rollback_on_failure: _do_rollback()
       → _fail_stage(); return False
```

---

## 8. Sequential Execution

Sequential execution arises naturally from the DAG. Stage B cannot become "ready" until A is in `completed`. The engine calls `get_ready_stages(completed)` at every iteration — B simply never appears in the ready list until A is added to `completed`.

```
Iteration 1: ready = [A]        → execute A → completed = {A}
Iteration 2: ready = [B]        → execute B → completed = {A, B}
Iteration 3: ready = [C]        → execute C → completed = {A, B, C}
Iteration 4: ready = []         → break
```

No explicit sequencing mechanism exists — sequential order is a property of the dependency graph topology.

---

## 9. Parallel Execution

Parallel execution arises when two or more stages share no dependency edge and their predecessors are all complete. The engine uses `asyncio.gather` to launch them concurrently in the same event loop iteration.

```
Iteration 1: ready = [ROOT]              → execute ROOT → completed = {ROOT}
Iteration 2: ready = [BRANCH_A, BRANCH_B] → asyncio.gather(A, B)
                                          → completed = {ROOT, BRANCH_A, BRANCH_B}
Iteration 3: ready = [SYNC]              → execute SYNC
```

Because Python's asyncio is **single-threaded and cooperative**, concurrent stages yield at `await` points. There are no data races on `state` or `exec_ctx` — mutations between `await` points are atomic.

---

## 10. Synchronization

A **synchronization point** (barrier) is any stage that has two or more predecessors. It cannot become ready until **all** its predecessors are in `completed`. This is implemented implicitly by `get_ready_stages`:

```python
def get_ready_stages(self, completed: set[str]) -> list[str]:
    return [
        node for node in self._graph
        if node not in completed
        and all(pred in completed for pred in self._graph.predecessors(node))
    ]
```

If any predecessor fails (added to `failed`, not `completed`), the downstream stage never becomes ready and is eventually marked `BLOCKED` by `_finalise`.

---

## 11. Entry / Exit Gates

### 11.1 Entry gate

Evaluated **once** per stage, before the retry loop. A failing entry gate immediately fails the stage — there is no retry of the entry gate itself.

```python
@abstractmethod
async def entry_gate(self, ctx: StageContext) -> GateResult:
    ...
```

If `entry_gate()` raises an exception, the stage is failed with event `"entry_gate_exception"`. If it returns `GateResult(passed=False, reason="...")`, the stage is failed with event `"entry_gate_failed"`.

### 11.2 Exit gate

Evaluated **inside the retry loop** after every successful `execute()` call. A failing exit gate can trigger a retry if `RetryPolicy.exit_gate_failure_retryable=True`.

```python
@abstractmethod
async def exit_gate(self, ctx: StageContext) -> GateResult:
    ...
```

Exit gate failure modes:
- **Returns** `GateResult(passed=False)` → record `StageAttemptRecord`, retry or fail
- **Raises exception** → treated as TRANSIENT failure, same retry logic applies

### 11.3 GateResult model

```python
class GateResult(BaseModel):
    gate_name: str          # e.g. "architecture_exit"
    passed: bool
    reason: str | None      # populated on failure
```

All gate results are stored in `StageContext.entry_gate_results` and `StageContext.exit_gate_results` for audit and replay.

---

## 12. Context Propagation

### 12.1 ExecutionContext

`ExecutionContext` accumulates the merged outputs of all completed stages. It grows monotonically — outputs are never removed (except during replanning, when a fresh context is built from preserved stages).

```python
class ExecutionContext(BaseModel):
    workflow_id: str
    artifacts: list[Artifact]           # all produced artifacts, in order
    decisions: list[Decision]           # all decisions, chronological
    risks: list[Risk]                   # all identified risks
    stage_outputs: dict[str, dict]      # stage_name → output_data snapshot
```

### 12.2 Snapshot isolation

Each stage receives only its **direct predecessors'** outputs via `snapshot_for_stage(predecessors)`:

```python
def snapshot_for_stage(self, predecessors: list[str]) -> dict[str, Any]:
    merged = {}
    for pred in predecessors:
        merged.update(self.stage_outputs.get(pred, {}))
    return merged
```

This prevents stages from accidentally consuming unrelated upstream data. For a synchronization point with N predecessors, the outputs are merged in topological order — later predecessors overwrite earlier ones on key conflicts.

### 12.3 Multi-hop propagation

In a linear pipeline A → B → C, stage C receives only B's `output_data`. If C needs A's output, B must explicitly forward it:

```python
# In B.execute():
ctx.output_data.update(ctx.input_data)      # forward A's outputs
ctx.output_data["from_B"] = "my_value"      # add B's own outputs
```

This is an explicit design choice — stages declare their output contract rather than implicitly inheriting all upstream data.

---

## 13. Decision Lineage

### 13.1 WorkflowLineage

`WorkflowLineage` is a **read-only query interface** built from `WorkflowState`. It answers nine provenance questions without any additional storage:

| Question | Method |
|---|---|
| What requirement initiated this? | `initiating_requirement()` |
| Which tasks were created? | `get_all_tasks()` |
| Why was a task created? | `get_task_rationale(task_id)` |
| Which artifact produced it? | `get_artifact_for_task(task_id)` |
| Which decision led to the next stage? | `get_decision_for_transition(stage_name)` |
| Which agent executed it? | `get_agent_for_task(task_id)` |
| What validation was performed? | `get_validations_for_stage(stage_name)` |
| What changed? | `get_changes_for_stage(stage_name)` |
| What was approved? | `get_approvals()` |

### 13.2 Provenance fields

Every object that participates in the decision chain carries provenance fields:

| Model | Provenance fields |
|---|---|
| `Task` | `stage`, `rationale`, `created_by_decision_id`, `created_by_artifact_id`, `agent_execution_id` |
| `Decision` | `decision_type`, `rationale`, `made_at`, `made_by`, `parent_decision_id`, `downstream_impacts` |
| `ValidationResult` | `stage`, `rule_name`, `evidence` |
| `Artifact` | `produced_by_stage`, `produced_by_agent` |
| `StageTransition` | `predecessor_stages`, `driving_decision_id`, `transition_reason` |

### 13.3 Decision chain example

```
Requirement (id=REQ-001)
    └── Decision (id=DEC-001, type=SCOPE, parent=None)
            "URL Shortener v1.0 scope confirmed"
        └── Decision (id=DEC-002, type=ARCHITECTURAL, parent=DEC-001)
                "Use FastAPI + PostgreSQL"
            └── Decision (id=DEC-003, type=IMPLEMENTATION, parent=DEC-002)
                    "Layered architecture: router → service → repository"
                └── Task (created_by_decision_id=DEC-003)
                        "Design package structure"
```

`build_lineage(state)` normalises the full chain by annotating any tasks or validations that don't yet have a `stage` field with the stage name derived from the `StageContext` they were found in.

---

## 14. Human Approval Checkpoints

### 14.1 Autonomy levels

```python
class AutonomyLevel(str, Enum):
    FULL_AUTO         # Agent executes without any checkpoint
    SUPERVISED        # Agent executes; decision logged for human review
    APPROVAL_REQUIRED # Execution paused until human approves
    HUMAN_ONLY        # Agent may only recommend; human must execute
```

### 14.2 When approval is triggered

An approval checkpoint fires if **any** of these conditions is true:
1. `stage_impl.requires_approval = True` (explicit opt-in)
2. `approval_policy.requires_human_approval(stage_impl.action_impact)` is True — default threshold is `HIGH_IMPACT`
3. `policy_engine` returns `EnforcementDecision.REQUIRE_APPROVAL` for this stage

### 14.3 Approval flow

```
stage_ctx.status = AWAITING_APPROVAL

ApprovalRequest created:
  - workflow_id, stage_name, requesting_agent
  - stage_summary, risk_context, upstream_artifact_ids

gateway.request_approval(request) → ApprovalDecision
  ├── APPROVED → stage proceeds to execute()
  └── REJECTED / TIMED_OUT → stage fails with "approval_rejected"

Approval record written to WorkflowState.approvals:
  - approver identity (made_by field)
  - rationale
  - impact_level (ActionImpact value)
  - action_type (HighImpactActionType)
  - is_override flag
  - escalation_level
```

### 14.4 ApprovalGateway implementations

| Implementation | Use case |
|---|---|
| `AutoApproveGateway` | CI / testing — approves everything immediately |
| `AutoRejectGateway` | Testing rejection paths |
| `PresetApprovalGateway(decisions)` | Integration tests with specific decision outcomes |
| `EscalatingApprovalGateway` | First tries base gateway; on rejection, escalates and tries again |

### 14.5 CRITICAL action block

Stages with `action_impact = ActionImpact.CRITICAL` (mapped to `AutonomyLevel.HUMAN_ONLY`) are **blocked immediately** before any approval gateway is called. The stage fails with event `"critical_action_blocked"`. No agent may execute a CRITICAL action — it can only recommend.

### 14.6 Final QC checkpoint

`WorkflowEngine` accepts `final_approval_required=True`. When set, after all stages complete, the engine requests one final quality-control approval before marking the workflow COMPLETED. Rejection at this point marks the workflow FAILED.

---

## 15. Governance & Policy Guardrails

### 15.1 Architecture

```
PolicyEngine
    ├── evaluate(ActionContext) → PolicyEvaluationRecord
    │       runs all registered policies in order
    │       catches exceptions → fail-safe BLOCK
    │       aggregates: worst decision wins (BLOCK > REQUIRE_APPROVAL > WARN > ALLOW)
    └── policies: list[Policy]

Policy (ABC)
    ├── policy_id: str      (e.g. "SEC-001")
    ├── domain: PolicyDomain (SECURITY | COMPLIANCE | CHANGE_CONTROL)
    └── evaluate(ActionContext) → PolicyViolation | None
```

### 15.2 Built-in policies

| ID | Domain | Rule | Enforcement |
|---|---|---|---|
| SEC-001 | SECURITY | Production releases require `security_scan_passed=True` | BLOCK |
| SEC-002 | SECURITY | PII data access requires `pii_approved=True` | BLOCK |
| SEC-003 | SECURITY | Actions flagged `high_risk_action=True` emit a warning | WARN |
| COMP-001 | COMPLIANCE | SIGNIFICANT+ actions require `change_ticket_id` | BLOCK |
| COMP-002 | COMPLIANCE | Data deletion requires `retention_policy_checked=True` | BLOCK |
| CC-001 | CHANGE_CONTROL | Production releases require explicit human approval | REQUIRE_APPROVAL |
| CC-002 | CHANGE_CONTROL | HIGH_IMPACT+ actions require `rollback_plan_documented=True` | BLOCK |
| CC-003 | CHANGE_CONTROL | Changes during configured freeze windows are blocked | BLOCK |

### 15.3 Integration point in the execution flow

Governance is evaluated **after** the CRITICAL block and **before** the approval checkpoint, so a policy violation never reaches the approval step:

```
Entry gate → CRITICAL block → [Governance gate] → Approval checkpoint → Execute
```

### 15.4 Fail-safe behaviour

If a `Policy.evaluate()` raises an exception, the exception is caught and treated as `BLOCK`. A broken guardrail never permits execution — it always prevents it.

### 15.5 Audit trail

Every governance evaluation emits one `"policy_evaluated"` audit entry. BLOCK decisions additionally emit `"policy_blocked"`. All `PolicyEvaluationRecord` objects are stored in `WorkflowState.policy_evaluations`.

### 15.6 ActionContext — stage metadata interface

Stages declare their policy metadata via the `policy_metadata` class attribute:

```python
class ReleaseReadinessStage(BaseStage):
    policy_metadata = {
        "security_scan_passed": True,
        "change_ticket_id": "CHG-2026-GF-001",
        "rollback_plan_documented": True,
    }
```

The engine passes this dict to `PolicyEngine.evaluate(ActionContext(..., metadata=policy_metadata))`.

---

## 16. Retry / Fallback / Rollback

### 16.1 RetryPolicy

Every stage has a `retry_policy: RetryPolicy` class attribute that overrides the engine-level default. The default is **single attempt, no retry** (`max_attempts=1`).

```python
class RetryPolicy(BaseModel):
    max_attempts: int                    # 1-10; infinite never allowed
    non_retryable_error_types: list[str] # exception class names → PERMANENT
    safe_stop_error_types: list[str]     # exception class names → CRITICAL
    exit_gate_failure_retryable: bool    # True = exit gate failure triggers retry
    fallback_behavior: FallbackBehavior  # FAIL | SKIP | USE_PRESET
    fallback_output: dict | None         # used when behavior=USE_PRESET
    rollback_on_failure: bool            # True = call rollback() on exhaustion
```

### 16.2 Failure classification

```python
class FailureClassification(str, Enum):
    TRANSIENT  # retryable (network timeout, lock contention)
    PERMANENT  # not retryable (invalid input, schema violation)
    CRITICAL   # triggers safe-stop (security breach, invariant violation)
```

Classification uses exception MRO: if `ChildError` is a subclass of `ParentError` and `ParentError` is in `non_retryable_error_types`, `ChildError` is also classified PERMANENT.

### 16.3 Retry loop decision tree

```
attempt N:
    execute() raises exception
        → classify(exc, policy)
            CRITICAL  → _trigger_safe_stop(); return False immediately
            PERMANENT → record FAIL_IMMEDIATE; break loop
            TRANSIENT → if N < max_attempts: record RETRY; continue
                        else: record FAIL_IMMEDIATE; break

    execute() succeeds; exit_gate() fails or raises
        → if exit_gate_failure_retryable and N < max_attempts: RETRY
        → else: FAIL_IMMEDIATE

after loop:
    if succeeded: propagate to ExecutionContext; return True

    fallback_behavior = SKIP     → stage marked SKIPPED; return True (downstream proceeds)
    fallback_behavior = USE_PRESET + fallback_output set
                                 → stage marked COMPLETED with fallback output; return True
    rollback_on_failure = True   → _do_rollback(); stage marked ROLLED_BACK; return False
    else                         → _fail_stage(); return False
```

### 16.4 Attempt records

Every failed attempt appends a `StageAttemptRecord` to `StageContext.attempt_records`:

```python
class StageAttemptRecord(BaseModel):
    attempt: int                    # 0-indexed
    error: str                      # str(exception)
    error_type: str                 # exception class name
    classification: FailureClassification
    recovery_decision: RecoveryDecision  # RETRY | FALLBACK | ROLLBACK | SAFE_STOP | FAIL_IMMEDIATE
    timestamp: datetime
```

### 16.5 Rollback

`_do_rollback()` calls `stage_impl.rollback(ctx)` and records the outcome:
- On success: `stage_ctx.rollback_performed = True`, `status = ROLLED_BACK`, stage name added to `WorkflowState.rolled_back_stages`
- On rollback failure: `"rollback_failed"` audit event; not re-raised (does not block forward failure handling)

---

## 17. Safe-Stop

### 17.1 Trigger

Safe-stop is triggered when a stage raises an exception whose class name matches `RetryPolicy.safe_stop_error_types` for that stage. The failure is classified `CRITICAL`.

### 17.2 Behaviour

```
_trigger_safe_stop(stage_name, stage_ctx, state, error):
    stage_ctx.status = FAILED
    stage_ctx.error  = error
    state.safe_stopped = True
    state.safe_stop_reason = f"Stage '{stage_name}' raised CRITICAL exception: {error}"
    state.status = WorkflowStatus.STOPPED
    audit: "safe_stop_triggered"
```

Key properties:
- **No rollback** — the workflow is frozen for operator investigation
- **No retry** — CRITICAL exceptions bypass the retry loop entirely
- **No approval gateway call** — happens before the approval checkpoint returns
- **Future batches blocked** — the main loop checks `state.safe_stopped` and breaks
- **Concurrent stages** may still complete their current operation in the same `asyncio.gather` batch (cooperative multitasking)

### 17.3 Finalisation after safe-stop

`_finalise()` handles the STOPPED branch separately: it marks stages that never reached `entry_gate_evaluating` as `BLOCKED`, then returns. Workflow status remains `STOPPED`.

---

## 18. Dynamic Replanning

### 18.1 Motivation

When an upstream output changes — an artifact is revised, a requirement is clarified, an architecture decision is reversed — downstream stages that consumed the old output produce stale results. The replanning mechanism re-executes only the impacted stages, preserving all unaffected work.

### 18.2 ChangeEvent

```python
class ChangeEvent(BaseModel):
    event_type: ChangeEventType   # REQUIREMENT_CHANGE | ARTIFACT_CHANGED |
                                  # DECISION_CHANGED | VALIDATION_FAILED |
                                  # EXTERNAL_DEPENDENCY_CHANGED
    originating_stage: str | None # None = requirement-level (all stages impacted)
    change_description: str
    changed_artifact_id: str | None
    changed_decision_id: str | None
    rationale: str
```

### 18.3 Impact analysis

`WorkflowEngine.analyze_impact(state, change_event) → ImpactAnalysis`:

```
if originating_stage is None:
    impacted = all stages
else:
    impacted = stages_reachable_from(originating_stage)  # nx.descendants

preserved = all_stages - impacted
```

`ImpactAnalysis` also collects `invalidated_artifact_ids` and `invalidated_decision_ids` from impacted stage contexts.

### 18.4 Replan execution

`WorkflowEngine.replan(state, change_event) → WorkflowState`:

```
1. analyze_impact()  →  impacted, preserved
   if no impact: emit "replan_skipped"; increment replan_count; return

2. Drop stale stage contexts from state.stages

3. Rebuild ExecutionContext from preserved-completed stages only
   (fresh context — no stale outputs from invalidated stages)

4. Execution loop (same as run() but filtered to impacted_set):
   ready = get_ready_stages(completed) ∩ impacted_set
   asyncio.gather(*[_execute_stage(s, ...) for s in ready])
   → governance gate re-evaluates for each replanned stage
   → approval checkpoint re-runs for each replanned stage

5. Update workflow status

6. Append ReplanResult to state.replan_history
   state.replan_count += 1
```

### 18.5 ReplanResult (audit record)

```python
class ReplanResult(BaseModel):
    change_event: ChangeEvent
    impact_analysis: ImpactAnalysis
    replan_cycle: int                    # 1-indexed; increments on every replan()
    stages_replanned: list[str]          # successfully re-executed
    stages_preserved: list[str]          # untouched; outputs reused
    governance_reevaluations: list[str]  # stages re-evaluated by PolicyEngine
    approvals_rerequested: list[str]     # stages that requested fresh approval
    final_status: str
```

---

## 19. Observability

### 19.1 Structured log records

Every `AuditEntry` in `WorkflowState.audit_trail` is convertible to a `StructuredLogRecord`:

```python
class StructuredLogRecord(BaseModel):
    timestamp: datetime
    level: str          # INFO | WARN | ERROR (mapped from event name)
    event: str          # machine-readable event (e.g. "stage_started")
    workflow_id: str    # correlation ID — links all records for one run
    stage_name: str | None
    stage_id: str | None  # unique per stage execution (≠ stage name)
    actor: str
    details: dict[str, Any]
```

Log level mapping:
- `ERROR`: `stage_execution_failed`, `exit_gate_failed`, `policy_blocked`, `safe_stop_triggered`, `approval_rejected`, `workflow_failed`, and others
- `WARN`: `stage_retrying`, `exit_gate_retrying`, `stage_skipped_fallback`, `rollback_started`
- `INFO`: all other events

### 19.2 Execution trace

`build_execution_trace(state) → ExecutionTrace` reconstructs the full provenance chain:

```
TraceStepKind: REQUIREMENT → DECISION → TASK → AGENT → ARTIFACT → VALIDATION → APPROVAL → RESULT
```

Each `TraceStep` carries:
- Unique `id` (the actual model ID — requirement.id, decision.id, artifact.id, etc.)
- `links: list[str]` — IDs of predecessor steps (navigable chain)
- `timestamp` and `details` dict

### 19.3 Unique execution IDs

| ID | Where | Purpose |
|---|---|---|
| `WorkflowState.id` | UUID assigned at `run()` | Correlates all logs, metrics, approvals for one workflow run |
| `StageContext.stage_id` | UUID assigned at stage start | Distinguishes two executions of the same stage in different runs |
| `Task.agent_execution_id` | Optional; set by stage implementations | Correlates tasks to specific agent invocations |
| `Decision.id`, `Artifact.id`, etc. | UUID on every model | Enables full provenance chain reconstruction |

### 19.4 Audit events reference

| Event | Level | When emitted |
|---|---|---|
| `workflow_started` | INFO | `run()` begins |
| `stages_scheduled` | INFO | First ready batch determined |
| `entry_gate_evaluating` | INFO | Entry gate about to be called |
| `entry_gate_passed` | INFO | Entry gate returned `passed=True` |
| `entry_gate_failed` | ERROR | Entry gate returned `passed=False` |
| `policy_evaluated` | INFO | PolicyEngine evaluated the stage |
| `policy_blocked` | ERROR | PolicyEngine returned BLOCK |
| `approval_requested` | INFO | ApprovalGateway called |
| `approval_resolved` | INFO | ApprovalGateway returned a decision |
| `approval_rejected` | ERROR | Human rejected or gateway timed out |
| `stage_started` | INFO | execute() called (first attempt) |
| `stage_retrying` | WARN | execute() failed, retrying |
| `exit_gate_evaluating` | INFO | exit_gate() about to be called |
| `exit_gate_passed` | INFO | exit_gate() returned `passed=True` |
| `exit_gate_failed` | ERROR | exit_gate() returned `passed=False` |
| `exit_gate_retrying` | WARN | exit_gate() failed, retrying |
| `stage_completed` | INFO | Stage succeeded |
| `stage_failed_all_attempts` | ERROR | All retry attempts exhausted |
| `stage_skipped_fallback` | WARN | Fallback SKIP applied |
| `stage_fallback_applied` | WARN | Fallback USE_PRESET applied |
| `rollback_started` | WARN | rollback() about to be called |
| `rollback_completed` | WARN | rollback() succeeded |
| `rollback_failed` | ERROR | rollback() raised an exception |
| `safe_stop_triggered` | ERROR | CRITICAL exception halted workflow |
| `replan_initiated` | INFO | replan() started |
| `replan_completed` | INFO | replan() finished |
| `replan_skipped` | INFO | No impacted stages; replan was a no-op |
| `workflow_completed` | INFO | All stages completed |
| `workflow_failed` | ERROR | One or more stages failed |
| `workflow_safe_stopped` | ERROR | Safe-stop finalisation |

---

## 20. Reliability Metrics

### 20.1 Single-workflow metrics (`WorkflowMetrics`)

Computed by `compute_workflow_metrics(state: WorkflowState)`:

| Metric | Computation |
|---|---|
| End-to-end latency | `state.completed_at − state.created_at` (seconds) |
| Stage latency | `stage_ctx.completed_at − stage_ctx.started_at` per stage |
| Total retries | `sum(len(ctx.attempt_records) for ctx in state.stages.values())` |
| Total rollbacks | `len(state.rolled_back_stages)` |
| MTTR | Mean of `(stage_ctx.completed_at − attempt_records[0].timestamp)` for stages that retried and succeeded |
| Succeeded | `state.status == COMPLETED` |

### 20.2 Cross-run reliability metrics (`ReliabilityMetrics`)

Computed by `compute_reliability_metrics(states: list[WorkflowState])`:

| Metric | Computation |
|---|---|
| Success rate | `successful_runs / total_runs` |
| Failure rate | `failed_runs / total_runs` |
| Retry frequency | `total_retries / total_runs` |
| Rollback frequency | `total_rollbacks / total_runs` |
| Mean e2e latency | `mean(latency for run in runs if latency is not None)` |
| Mean stage latency | `mean(stage_latency for all stages in all runs)` |
| MTTR (cross-run) | `mean(recovery_time for all retried+recovered stages across all runs)` |

### 20.3 Observability report

`build_observability_report(state) → WorkflowObservabilityReport` combines all three into one JSON-serializable artifact:

```python
class WorkflowObservabilityReport:
    workflow_id: str
    execution_trace: ExecutionTrace
    metrics: WorkflowMetrics
    structured_logs: list[StructuredLogRecord]

    def failure_trace() → list[StructuredLogRecord]    # ERROR-level only
    def decision_trace() → list[TraceStep]             # decisions
    def approval_trace() → list[TraceStep]             # approvals
    def policy_trace() → list[StructuredLogRecord]     # policy events
    def artifact_trace() → list[TraceStep]             # artifacts
    def as_dict() → dict[str, Any]                     # JSON-serializable
```

---

## 21. Security Considerations

| Concern | Implementation |
|---|---|
| **URL input validation** | `ShortenRequest.url` validated by Pydantic: must be a valid http/https URL; `max_length=2048`; scheme whitelist enforced by the `url` type validator |
| **Short-code collision** | PostgreSQL `UNIQUE` constraint on `short_urls.code`; service retries up to `_MAX_GENERATION_ATTEMPTS=10` times on `IntegrityError` |
| **Short-code generation** | `secrets.choice()` (CSPRNG) over 36-character base-36 alphabet; 36^8 ≈ 2.8 trillion combinations |
| **Soft-delete for deactivated codes** | Deactivated codes set `is_active=False`; code cannot be re-issued after deactivation (audit trail preserved) |
| **No PII in logs** | URL content, user-agent, and IP are not logged at the service layer; only short-code and event type |
| **Config secrets** | All secrets (database URL, etc.) read from environment variables via pydantic-settings; no secrets hardcoded; `.env` in `.gitignore` |
| **CORS** | Configured in `main.py`; current prototype allows `allow_origins=["*"]` (development mode) |
| **Approval fail-safe** | Missing `ApprovalGateway` when `requires_approval=True` causes stage to fail with `"approval_timed_out"` — never silently bypassed |
| **Policy fail-safe** | A `Policy.evaluate()` exception is caught and treated as `BLOCK` — a broken guardrail never permits execution |
| **CRITICAL action block** | `AutonomyLevel.HUMAN_ONLY` stages fail immediately before any gateway call — agents cannot execute them under any circumstances |
| **Audit trail integrity** | `WorkflowState.audit_trail` is append-only; no delete API; every gate result, approval decision, and policy evaluation is recorded with timestamp and actor |
| **IP hashing (scenarios)** | Analytics planning stages document SHA-256 hashing of IPs — no raw IP storage advocated |

**Not yet implemented:** JWT authentication, API key validation, Redis-backed rate limiting enforcement (config field `rate_limit_per_minute` exists but enforcement code is not wired up).

---

## 22. Testing Strategy

### 22.1 Test inventory

Verified with `pytest --collect-only` (893 total):

| Suite | Location | Tests | Focus |
|---|---|---|---|
| Unit (URL shortener) | `tests/unit/` | 67 | Models, schemas, `UrlService` |
| Orchestrator | `tests/orchestrator/` | 531 | Engine, gates, failure, governance, lineage, replan, observability, approvals |
| Scenarios | `tests/scenarios/` | 190 | Greenfield, brownfield, ambiguous E2E on real `WorkflowEngine` |
| Validation pass | `tests/test_validation_pass.py` | 87 | Gap coverage across API/repo/edge paths |
| Integration | `tests/integration/` | 18 | ASGI stack (SQLite in-memory fixtures) |
| **Total** | | **893** | |

### 22.2 Coverage

| Module | Coverage |
|---|---|
| `orchestrator/core/autonomy.py` | 100% |
| `orchestrator/core/base_agent.py` | 100% |
| `orchestrator/core/base_orchestrator.py` | 100% |
| `orchestrator/core/context.py` | 100% |
| `orchestrator/core/failure.py` | 100% |
| `orchestrator/core/observability.py` | 100% |
| `orchestrator/core/replanning.py` | 100% |
| `orchestrator/core/results.py` | 100% |
| `orchestrator/engine/task_scheduler.py` | 100% |
| `orchestrator/observability/logging.py` | 100% |
| `url_shortener/repositories/url_repo.py` | 100% |
| `url_shortener/services/url_service.py` | 100% |
| `orchestrator/core/governance.py` | 99% |
| `orchestrator/core/lineage.py` | 99% |
| `orchestrator/core/models.py` | 99% |
| `orchestrator/engine/workflow_engine.py` | 98% |
| `orchestrator/core/graph.py` | 95% |
| `orchestrator/scenarios/ambiguous.py` | 93% |
| `orchestrator/scenarios/brownfield.py` | 89% |
| `orchestrator/scenarios/greenfield.py` | 89% |
| `url_shortener/api/urls.py` | 85% |
| **Overall** | **~95%** (fail_under=80 in `pyproject.toml`) |

### 22.3 Test types

**Unit tests** — no I/O, no network, no database. All external dependencies mocked.
- Repository layer: `AsyncSession` mocked with `unittest.mock.AsyncMock`
- Stage execution: custom `BaseStage` stubs with call counters
- Policy evaluation: `ActionContext` constructed directly

**Integration tests** — full ASGI stack via `httpx.AsyncClient`, SQLite in-memory.
- `conftest.py` provides `http_client`, `db_session`, `test_engine` fixtures
- Each test runs in a savepoint (nested transaction) rolled back on completion
- Tests in `tests/integration/` require the full fixture stack

**Scenario tests** — end-to-end via `WorkflowEngine.run()`, no mocking.
- `AutoApproveGateway` satisfies approval checkpoints
- `PresetClarificationGateway` (ambiguous scenario) satisfies clarification
- Real `WorkflowDefinition` DAGs, real stage implementations

### 22.4 Known gaps

| Gap | Risk | Path to close |
|---|---|---|
| `url_shortener/main.py` lifespan (67%) | Low — integration test infra skips lifespan | Add ASGI lifespan integration test |
| `url_shortener/api/deps.py` `get_db` (56%) | Low — covered indirectly by integration tests | Covered by `tests/integration/` which are excluded from unit-only runs |
| Phantom node in `WorkflowDefinition` | Low — `WorkflowEngine.__init__` catches it | No code change needed; gap is documented |
| `url_shortener/database.py` engine creation (78%) | Low — requires real connection string | Add env-gated integration test |
| Scenario branch paths at 89-93% | Low | Scenarios with forced entry/exit gate failures |

### 22.5 Running the test suite

```bash
# All unit + scenario tests (no database, no network)
python -m pytest tests/ --ignore=tests/integration

# With coverage
python -m pytest tests/ --ignore=tests/integration \
    --cov=orchestrator --cov=url_shortener \
    --cov-report=term-missing

# Integration tests (ASGI + SQLite in-memory; no Docker required)
python -m pytest tests/integration/

# Single scenario
python -m pytest tests/scenarios/test_greenfield.py -v

# Specific area
python -m pytest tests/orchestrator/test_replanning.py -v
```

---

## Appendix A — Three Scenarios Side by Side

| Dimension | Greenfield | Brownfield | Ambiguous |
|---|---|---|---|
| **Requirement type** | `GREENFIELD` | `BROWNFIELD` | `AMBIGUOUS` |
| **Stages** | 8 | 6 | 5 |
| **Topology** | Partially parallel (fan-out + sync) | Linear | Linear |
| **Approval stages** | 2 (architecture + release) | 2 (change_planning + validation) | 1 (validation) |
| **Special mechanism** | — | Codebase analysis; explicit `do_not_modify` list | `ClarificationGateway`; pause point |
| **Key output** | `release_checklist.json` | `change_plan.json` | `assumption_registry.json` |
| **Invariant enforced** | All 9 ADRs traced to decisions | Preserved files ∩ implementation_tasks = ∅ | Every FR has `source_decision` field |
| **Governance** | SEC-001 + CC-001 + CC-002 at release | COMP-001 + CC-002 at change_planning | CC-001 + CC-002 at validation |

## Appendix B — Key Design Decisions

| Decision | Rationale | Trade-off accepted |
|---|---|---|
| **networkx DAG over linear chain** | Enables parallel execution and `stages_reachable_from()` for replanning | More complex than a list; requires validation |
| **`DEFAULT_RETRY_POLICY.max_attempts = 1`** | Fail-fast by default; stages opt-in to retries | Existing stages must explicitly declare retry policy |
| **`asyncio.gather` for parallel stages** | Single-threaded; no races without locks | Concurrent stages in same batch may see each other's partial state if they share mutable state (they don't — each writes to its own `StageContext` key) |
| **`ExecutionContext` snapshot isolation** | Each stage sees only its direct predecessors' outputs | Long pipeline stages must explicitly forward upstream data they want downstream to access |
| **Fail-safe approval: no gateway = fail** | Unattended system never proceeds past a governance checkpoint | Breaks workflows if gateway is accidentally omitted |
| **Policy exception = BLOCK** | Broken guardrail never permits execution | May produce confusing errors if policies have bugs |
| **`completed_at` set after `_finalise`** | Includes all finalisation work in latency measurement | Latency includes BLOCKED stage creation time |
| **Replan rebuilds ExecutionContext from scratch** | Clean slate; no stale outputs; simple to reason about | Slightly more work per replan than selective invalidation |
| **`ClarificationGateway` constructor-injected** | Stages are testable without the engine knowing about clarification | Breaks the pattern of stages being pure configuration objects |
