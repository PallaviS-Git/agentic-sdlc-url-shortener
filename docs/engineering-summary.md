# Final Engineering Summary

Assignment deliverable covering testing approach, limitations, risks/trade-offs, and engineering judgment. Complements [architecture.md](architecture.md) and [setup.md](setup.md).

---

## 1. What was built

| Layer | Role |
|---|---|
| **Orchestrator** | DAG-based SDLC engine: entry/exit gates, parallel waves, sync, approvals, retries, fallback, rollback, safe-stop, governance, lineage, replanning, observability |
| **URL shortener** | Production-shaped FastAPI service: `POST /shorten`, `GET /{code}`, `DELETE /{code}`, health — the artifact scenarios plan and reason about |
| **Scenarios** | Greenfield, brownfield, and ambiguous pipelines run on the real `WorkflowEngine` |

---

## 2. Agentic model (requirement 7)

**Intentional design:** scenario stages **simulate** agent work by producing structured artifacts, decisions, and validations inside `BaseStage.execute()`. They do not call external LLMs.

**Extension point:** `orchestrator.core.base_agent.BaseAgent` defines `execute` / `validate_input` / `validate_output` / `rollback`. Production agents would be invoked from stage `execute()` without changing the engine.

**Human control:** `ApprovalGateway` and `ClarificationGateway` pause high-impact or ambiguous work. CLI implementations (`HumanApprovalGateway`, `HumanClarificationGateway`) support live demos; presets/`AutoApprove*` support tests.

This keeps the prototype focused on **orchestration, governance, and traceability** — the assignment differentiator — rather than LLM integration.

---

## 3. Product scope: analytics & rate limiting (requirements 28, 32–34)

| Feature | Status in `url_shortener/` | How the assignment still demonstrates it |
|---|---|---|
| **Core APIs** | Implemented | Create / resolve / deactivate |
| **Analytics** | **Not shipped in the service** | Greenfield + ambiguous scenarios plan analytics (schemas, tasks, `source_decision` provenance). Ambiguous flow clarifies "add analytics" before planning. |
| **Rate limiting** | Config field `rate_limit_per_minute` exists; **not enforced** | Brownfield scenario reasons over the dormant field and produces a change plan with `do_not_modify` |

**Rationale:** Shipping full analytics/rate-limit middleware would expand the product beyond what the orchestration demos need. Scenarios prove requirement → plan → validation; the service remains a stable brownfield target.

---

## 4. Testing approach (requirements 29–30)

| Suite | Location | Count | Infra |
|---|---|---|---|
| Unit | `tests/unit/` | 67 | None |
| Orchestrator | `tests/orchestrator/` | 531 | None |
| Scenarios | `tests/scenarios/` | 190 | None |
| Validation pass | `tests/test_validation_pass.py` | 87 | None |
| Integration | `tests/integration/` | 18 | ASGI + SQLite (no Docker) |
| **Total** | | **893** | |

```bash
# Default CI-style run (no database, no network)
python -m pytest tests/ --ignore=tests/integration

# Full suite including integration
python -m pytest tests/

# Integration only
python -m pytest tests/integration/
```

Coverage target: ≥80% (`pyproject.toml`); recent runs ~95% on orchestrator + url_shortener.

**Validation approach:** each stage emits `ValidationResult` records (pass/fail + severity + evidence). Scenario exit gates fail the stage if critical validations fail. Humans approve high-impact stages via `ApprovalGateway`. Full inspection recipes: [setup.md §12](setup.md#12-viewing-validation-results).

---

## 5. Risks and trade-offs (requirement 36)

| Risk | Mitigation / accepted trade-off |
|---|---|
| Simulated agents understate “multi-agent” systems | Real engine + `BaseAgent` contract; documented above |
| In-memory workflow state lost on process exit | Full `WorkflowState` + audit in-process; durable store deferred |
| Fail-safe (no approval gateway → fail) breaks unattended demos | Always inject `AutoApproveGateway` or `HumanApprovalGateway` |
| Default `RetryPolicy.max_attempts = 1` | Fail-fast; stages opt in to retries |
| Policy exception → BLOCK | Prefer false deny over false allow |
| Parallel stages via `asyncio.gather` | Sync is DAG-predecessor based; stages write isolated `StageContext` keys |
| Analytics / rate-limit not in service | Explicit non-scope (section 3); planned in scenarios |

Further design trade-offs: [architecture.md Appendix B](architecture.md#appendix-b--key-design-decisions).

---

## 6. Limitations (requirement 37)

1. No live LLM or codegen agents — stages emit predetermined structured outputs.
2. No durable workflow persistence / operator pause-resume API (`BaseOrchestrator` describes a fuller lifecycle than `WorkflowEngine` currently exposes).
3. Safe-stop is CRITICAL-failure driven, not a general operator stop API.
4. Observability is in-process (structured audit + metrics models); no Prometheus exporter wired.
5. URL shortener: no analytics endpoints; rate limit config unused; Redis present for future use.
6. `HumanApprovalGateway` / `HumanClarificationGateway` are CLI-blocking; not a web UI.
7. Phantom DAG dependency names are caught at engine init, not at `WorkflowDefinition` construction.

---

## 7. Engineering judgment (requirement 38)

| Decision | Why |
|---|---|
| Orchestration-first, service-second | Assignment scores agentic SDLC more than another URL shortener |
| networkx DAG | Enables parallel fan-out, sync join, and reachability for replanning |
| Fail-safe approvals & policy BLOCK on error | Controlled autonomy over silent progress |
| Brownfield: enforce dormant config via plan, preserve service layer | Realistic change control vs rewrite |
| Ambiguous: pause + `source_decision` on every FR/task | Prevents silent invention of requirements |
| Conservative defaults in clarification | Prefer incomplete-but-honest plans over fabricated scope |
| High unit/scenario coverage without LLM flakiness | Deterministic reviewable outcomes |

---

## 8. Assumptions

| Assumption | Implication |
|---|---|
| Interview evaluators prioritize orchestration quality over shipping every planned product feature | Analytics and Redis rate-limit enforcement remain scenario-planned, not implemented in `url_shortener/` |
| Simulated stage agents are acceptable when the engine, gates, governance, and lineage are real | No external LLM credentials or network calls required to run demos/tests |
| Local/dev defaults (`postgres:postgres`, `DEBUG=false`) are for compose/dev only | Production must override `DATABASE_URL` / secrets via environment; never commit `.env` |
| `STATE_FILE_PATH` / `state_file_path` is reserved configuration | Durable workflow snapshots are not yet wired into `WorkflowEngine` |
| Approval and clarification gateways are injected by the caller | Unattended runs must pass `AutoApproveGateway` / preset clarification gateways |
| Integration tests validate HTTP contracts via SQLite | They do not prove PostgreSQL-specific behavior or live Redis behavior |
| Docker Compose credentials in-repo are local lab defaults | Same values appear in `.env.example` as placeholders, not production secrets |

---

## 9. Remaining known gaps (non-blocking)

- `BaseOrchestrator.pause` / `resume` / persisted `get_state` not implemented on `WorkflowEngine`
- Package docs justify `orchestrator/stages` (scenarios own concrete stages) and `orchestrator/policies` (re-exports from governance)
- Optional future: wire `BaseAgent` stubs; export `WorkflowState` to JSON; ship minimal analytics or Redis rate limit

See also architecture §22.4 for coverage-level gaps.
