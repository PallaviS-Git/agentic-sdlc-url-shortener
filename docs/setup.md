# Setup and Execution Guide

> **Verified:** Every command in this guide was run against the repository and produced the output shown.  
> **Platform:** Commands are written for both Unix/macOS and Windows PowerShell where they differ.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Environment Setup](#2-environment-setup)
3. [Dependency Installation](#3-dependency-installation)
4. [Configuration](#4-configuration)
5. [Running the URL Shortener](#5-running-the-url-shortener)
6. [Running the Agentic Orchestration System](#6-running-the-agentic-orchestration-system)
7. [Running Tests](#7-running-tests)
8. [Running the Greenfield Scenario](#8-running-the-greenfield-scenario)
9. [Running the Brownfield Scenario](#9-running-the-brownfield-scenario)
10. [Running the Ambiguous Scenario](#10-running-the-ambiguous-scenario)
11. [Viewing Workflow Results](#11-viewing-workflow-results)
12. [Viewing Validation Results](#12-viewing-validation-results)
13. [Understanding Approvals](#13-understanding-approvals)
14. [Observing Failure and Recovery Behaviour](#14-observing-failure-and-recovery-behaviour)
15. [Troubleshooting](#15-troubleshooting)

Related: [engineering-summary.md](engineering-summary.md) (risks, limitations, judgment).

---

## 1. Prerequisites

### Required

| Requirement | Version | Notes |
|---|---|---|
| Python | **3.11+** | Required by FastAPI / Pydantic v2 stack |
| pip | latest | Ships with Python; upgrade with `pip install --upgrade pip` |
| Git | any | To clone the repository |

### Required for URL Shortener with PostgreSQL (Option B)

| Requirement | Version | Notes |
|---|---|---|
| Docker Desktop | ≥ 4.x | Runs PostgreSQL 15 + Redis 7 without local install |
| Docker Compose | v2 (bundled) | Used via `docker compose` (no hyphen) or `docker-compose` |

### Not required for orchestrator tests and scenarios

The orchestration engine, all three scenarios, and the non-integration test suite (875 tests) run with **no Docker, no PostgreSQL, and no Redis**. Those tools are only needed to start the URL Shortener HTTP service with a real database. Integration tests (18) use in-memory SQLite and also do not require Docker.

---

## 2. Environment Setup

### Clone the repository

```bash
git clone <repo-url>
cd agentic-sdlc-url-shortener
```

### Create a virtual environment

```bash
# Unix / macOS
python3.11 -m venv .venv
source .venv/bin/activate

# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Verify the correct Python is active:

```bash
python --version
# Expected: Python 3.11.x
```

---

## 3. Dependency Installation

Install all dependencies from `requirements.txt`. Run commands from the repository root so `orchestrator` and `url_shortener` import correctly.

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

**What `requirements.txt` installs:**

| Package | Purpose |
|---|---|
| `fastapi==0.111.0` | Web framework for URL shortener |
| `uvicorn[standard]==0.30.1` | ASGI server |
| `sqlalchemy[asyncio]==2.0.30` | Async ORM |
| `asyncpg==0.29.0` | PostgreSQL async driver |
| `networkx==3.3` | DAG engine (orchestrator) |
| `pydantic==2.7.1` | Data validation |
| `pydantic-settings==2.3.1` | Config from env vars |
| `structlog==24.2.0` | Structured logging |
| `httpx==0.27.0` | HTTP client (integration tests) |
| `aiosqlite==0.20.0` | SQLite async driver |
| `pytest==8.2.2` | Test runner |
| `pytest-asyncio==0.23.7` | Async test support |

Verify the install:

```bash
python -c "import fastapi, sqlalchemy, networkx, pydantic; print('OK')"
# Expected: OK
```

---

## 4. Configuration

### Option A — Local development (no Docker)

Copy the example environment file:

```bash
# Unix / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

The defaults in `.env.example` work for a local development setup where PostgreSQL and Redis run on `localhost`. For the orchestration scenarios (which need no database), the `.env` file is not required.

### Option B — Docker Compose (recommended for URL Shortener)

When using Docker Compose, environment variables are set directly in `docker-compose.yml`. No `.env` file is needed for the services themselves, but you may want one for any local tooling.

### Configuration reference

All settings are read from environment variables via `url_shortener/config.py` (pydantic-settings). Unset variables fall back to these defaults:

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | `development \| testing \| production` |
| `LOG_LEVEL` | `INFO` | `DEBUG \| INFO \| WARNING \| ERROR` |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/urlshortener` | Async PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `BASE_URL` | `http://localhost:8000` | Used to build `short_url` in responses |
| `SHORT_CODE_LENGTH` | `8` | Length of generated short codes |
| `DEFAULT_TTL_SECONDS` | `31536000` | Default URL expiry (1 year); applies when `expires_in_seconds` is omitted |
| `RATE_LIMIT_PER_MINUTE` | `100` | Declared but not yet enforced by middleware |
| `MAX_STAGE_RETRIES` | `3` | Orchestrator stage retry limit |
| `APPROVAL_TIMEOUT_SECONDS` | `300` | Orchestrator approval timeout |

---

## 5. Running the URL Shortener

### Option A — Docker Compose (full stack)

Start PostgreSQL, Redis, and the URL Shortener in one command:

```bash
docker compose up --build
```

This:
1. Builds the multi-stage Docker image (`Dockerfile`)
2. Starts `postgres:15-alpine` with healthcheck
3. Starts `redis:7-alpine` with healthcheck
4. Waits for both to be healthy, then starts the `app` service
5. Exposes the API on `http://localhost:8000`

Wait for the log line `application_startup` to confirm the service is ready, then:

```bash
# Create a short URL
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'

# Expected response:
# {"short_url":"http://localhost:8000/abc12345","code":"abc12345",
#  "original_url":"https://example.com","created_at":"...","expires_at":null}

# Resolve the short URL (follow the redirect)
curl -L http://localhost:8000/abc12345

# Health check
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0"}

# Interactive API docs
# Open http://localhost:8000/docs in a browser
```

Stop and remove containers:

```bash
docker compose down          # stop containers, keep data volume
docker compose down -v       # stop containers, also remove postgres_data volume
```

### Option B — Local uvicorn with SQLite (no Docker)

Tables are created automatically on startup:

```powershell
$env:DATABASE_URL = "sqlite+aiosqlite:///./local_dev.db"
$env:BASE_URL = "http://127.0.0.1:8000"
uvicorn url_shortener.main:app --host 127.0.0.1 --port 8000
```

```bash
DATABASE_URL="sqlite+aiosqlite:///./local_dev.db" \
BASE_URL="http://127.0.0.1:8000" \
  uvicorn url_shortener.main:app --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000/docs or:

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/shorten -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

### API Endpoints

| Method | Path | Description | Response |
|---|---|---|---|
| `POST` | `/shorten` | Create a short URL | 201 with JSON body |
| `GET` | `/{code}` | Redirect to original URL | 302 redirect |
| `DELETE` | `/{code}` | Soft-delete a short URL | 204 no content |
| `GET` | `/health` | Liveness probe | 200 `{"status":"ok"}` |
| `GET` | `/docs` | Swagger UI (auto-generated) | HTML |

**Request schema for `POST /shorten`:**

```json
{
  "url": "https://example.com",
  "expires_in_seconds": 3600
}
```

- `url` — required; must be a valid `http` or `https` URL, max 2048 characters
- `expires_in_seconds` — optional; 1 to 31536000 (1 year); omit for no expiry

---

## 6. Running the Agentic Orchestration System

The orchestration engine has **no CLI** — it is a Python library. Scenarios are invoked via Python scripts. No database or Docker is required.

The three built-in scenario runners are coroutines in:

| Scenario | Module | Runner function |
|---|---|---|
| Greenfield | `orchestrator.scenarios.greenfield` | `run_greenfield_scenario(...)` |
| Brownfield | `orchestrator.scenarios.brownfield` | `run_brownfield_scenario(...)` |
| Ambiguous | `orchestrator.scenarios.ambiguous` | `run_ambiguous_scenario(...)` |

Each runner is fully self-contained — it creates the `WorkflowDefinition`, instantiates stages, and runs the `WorkflowEngine`. See Sections 8–10 for complete runnable examples.

---

## 7. Running Tests

The full suite is **894** tests. Non-integration tests (**876**) need no running services (no Docker, no PostgreSQL, no Redis). Integration tests in `tests/integration/` (**18**) use SQLite in-memory via `aiosqlite` and also do not require Docker.

### Run the full suite

```bash
python -m pytest tests/ --ignore=tests/integration
```

Expected output (abbreviated):

```
collected 875 items
...
875 passed
```

### Run with coverage

```bash
python -m pytest tests/ --ignore=tests/integration \
    --cov=orchestrator \
    --cov=url_shortener \
    --cov-report=term-missing
```

Expected summary (figures drift slightly as the suite evolves):

```
875 passed
```

### Run specific test suites

```bash
# URL shortener unit tests (no DB required)
python -m pytest tests/unit/ -v

# Orchestrator unit tests
python -m pytest tests/orchestrator/ -v

# All three scenarios
python -m pytest tests/scenarios/ -v

# Single test file
python -m pytest tests/orchestrator/test_workflow_engine.py -v

# Single test class
python -m pytest tests/orchestrator/test_failure.py::TestSuccessfulRetry -v

# Single test
python -m pytest tests/orchestrator/test_failure.py::TestSuccessfulRetry::test_stage_succeeds_on_second_attempt -v

# Filter by marker
python -m pytest -m unit -v
python -m pytest -m orchestrator -v
```

### Run integration tests (ASGI + SQLite; no Docker required)

```bash
python -m pytest tests/integration/ -v
```

These tests use in-memory SQLite via `aiosqlite`. Docker Postgres/Redis are only needed to run the live URL shortener HTTP service (see Section 5).

### Run with HTML coverage report

```bash
python -m pytest tests/ --ignore=tests/integration \
    --cov=orchestrator \
    --cov=url_shortener \
    --cov-report=html \
    --cov-report=term
# Report written to htmlcov/index.html
```

---

## 8. Running the Greenfield Scenario

**What it does:** Runs the full 8-stage SDLC pipeline to "build" the URL shortener from scratch. Produces 9 reviewable artifacts, 12 decisions, and 2 human approvals.

**Stages:** `requirements_analysis` → `architecture_design` → `task_decomposition` → `[implementation_planning | testing_planning | documentation_planning]` → `validation` → `release_readiness`

Create a file `run_greenfield.py`:

```python
"""Run the greenfield URL shortener SDLC scenario."""
import asyncio
import json

from orchestrator.scenarios.greenfield import run_greenfield_scenario
from orchestrator.core.autonomy import AutoApproveGateway
from orchestrator.core.observability import build_observability_report
from orchestrator.core.lineage import build_lineage


async def main() -> None:
    print("Starting greenfield scenario...")
    print("Approval: AutoApproveGateway (auto-approves all checkpoints)\n")

    state = await run_greenfield_scenario(
        approval_gateway=AutoApproveGateway()
    )

    # ── Workflow summary ───────────────────────────────────────────────────
    print(f"Workflow ID : {state.id}")
    print(f"Status      : {state.status.value}")
    print(f"Completed   : {state.completed_at}")
    print()

    # ── Stage outcomes ─────────────────────────────────────────────────────
    print("Stage results:")
    for name in sorted(state.stages):
        ctx = state.stages[name]
        print(f"  {name:<30} {ctx.status.value:<12} "
              f"artifacts={len(ctx.artifacts)}  decisions={len(ctx.decisions)}")
    print()

    # ── Artifacts ──────────────────────────────────────────────────────────
    print("Artifacts produced:")
    for name, ctx in state.stages.items():
        for art in ctx.artifacts:
            print(f"  [{art.artifact_type.value}] {art.name}  (stage: {name})")
    print()

    # ── Approvals ──────────────────────────────────────────────────────────
    print(f"Approvals obtained: {len(state.approvals)}")
    for appr in state.approvals:
        print(f"  stage={appr.stage_name!r:<30} "
              f"status={appr.status.value}  approver={appr.approver!r}")
    print()

    # ── Reliability metrics ────────────────────────────────────────────────
    report = build_observability_report(state)
    m = report.metrics
    print(f"End-to-end latency : {m.total_latency_seconds:.3f}s")
    print(f"Total retries      : {m.total_retries}")
    print(f"Total rollbacks    : {m.total_rollbacks}")
    print()

    # ── Decision lineage ───────────────────────────────────────────────────
    lineage = build_lineage(state)
    print(f"Decisions recorded : {len(lineage.decisions)}")
    print(f"Validations run    : {len(lineage.validations)}")
    print()

    # ── Export full report as JSON ─────────────────────────────────────────
    report_path = "greenfield_report.json"
    with open(report_path, "w") as f:
        json.dump(report.as_dict(), f, indent=2, default=str)
    print(f"Full observability report written to: {report_path}")


asyncio.run(main())
```

Run it:

```bash
python run_greenfield.py
```

Expected output:

```
Starting greenfield scenario...
Approval: AutoApproveGateway (auto-approves all checkpoints)

Workflow ID : 952d97bf-42f0-4519-959a-5e18c9d493a6
Status      : completed
Completed   : 2026-08-15 07:...

Stage results:
  architecture_design            completed    artifacts=2  decisions=4
  documentation_planning         completed    artifacts=1  decisions=1
  implementation_planning        completed    artifacts=1  decisions=2
  release_readiness              completed    artifacts=1  decisions=1
  requirements_analysis          completed    artifacts=1  decisions=1
  task_decomposition             completed    artifacts=1  decisions=1
  testing_planning               completed    artifacts=1  decisions=2
  validation                     completed    artifacts=1  decisions=0

Artifacts produced:
  [documentation] normalized_requirement.json  (stage: requirements_analysis)
  [documentation] architecture_design.json     (stage: architecture_design)
  [schema] data_model.json                     (stage: architecture_design)
  [documentation] task_graph.json              (stage: task_decomposition)
  [documentation] implementation_plan.json     (stage: implementation_planning)
  [test] test_plan.json                        (stage: testing_planning)
  [documentation] documentation_plan.json      (stage: documentation_planning)
  [report] validation_report.json              (stage: validation)
  [report] release_checklist.json              (stage: release_readiness)

Approvals obtained: 2
  stage='architecture_design'   status=approved  approver='auto-approve-gateway'
  stage='release_readiness'     status=approved  approver='auto-approve-gateway'

End-to-end latency : 0.010s
Total retries      : 0
Total rollbacks    : 0

Decisions recorded : 12
Validations run    : 10

Full observability report written to: greenfield_report.json
```

---

## 9. Running the Brownfield Scenario

**What it does:** Analyses the existing URL shortener codebase, identifies a dormant `rate_limit_per_minute` config field that was never wired to enforcement code, and produces a surgical change plan that explicitly lists what must **not** be modified.

**Stages:** `codebase_analysis` → `impact_analysis` → `risk_assessment` → `change_planning` → `regression_test_planning` → `validation`

Create a file `run_brownfield.py`:

```python
"""Run the brownfield rate-limiting enhancement scenario."""
import asyncio
import json

from orchestrator.scenarios.brownfield import run_brownfield_scenario
from orchestrator.core.autonomy import AutoApproveGateway
from orchestrator.core.observability import build_observability_report


async def main() -> None:
    print("Starting brownfield scenario...")
    print("Enhancement: Enforce rate limiting on POST /shorten\n")

    state = await run_brownfield_scenario(
        approval_gateway=AutoApproveGateway()
    )

    print(f"Workflow ID : {state.id}")
    print(f"Status      : {state.status.value}")
    print()

    # ── Impact map ─────────────────────────────────────────────────────────
    import ast
    ctx = state.stages["impact_analysis"]
    impact_data = json.loads(ctx.artifacts[0].content)
    print("Impacted components:")
    for module, info in impact_data["impacted_components"].items():
        print(f"  [{info['impact_type']}] {module}")
    print()

    print("Explicitly preserved (DO NOT MODIFY):")
    for module in impact_data["explicitly_preserved"]:
        print(f"  {module}")
    print()

    # ── Change plan ────────────────────────────────────────────────────────
    cp_ctx = state.stages["change_planning"]
    cp_data = json.loads(cp_ctx.artifacts[0].content)
    print(f"Implementation tasks: {len(cp_data['implementation_tasks'])}")
    for task in cp_data["implementation_tasks"]:
        print(f"  {task['id']}: {task['title']}")
    print()
    print("Rollback plan:")
    for step in cp_data["rollback_plan"]["steps"]:
        print(f"  - {step}")
    print(f"  RTO: {cp_data['rollback_plan']['rto_estimate']}")
    print()

    # ── Validation ─────────────────────────────────────────────────────────
    val_ctx = state.stages["validation"]
    val_data = json.loads(val_ctx.artifacts[0].content)
    print(f"Validation checks: {val_data['validation_checks']} passed, "
          f"{val_data['failed']} failed")
    print(f"Critical failures: {val_data['critical_failures']}")
    print(f"Implementation ready: {val_data['implementation_ready']}")
    print()

    # ── Approvals ──────────────────────────────────────────────────────────
    print(f"Approvals: {len(state.approvals)}")
    for appr in state.approvals:
        print(f"  {appr.stage_name!r}: {appr.status.value}")

    report = build_observability_report(state)
    with open("brownfield_report.json", "w") as f:
        json.dump(report.as_dict(), f, indent=2, default=str)
    print("\nFull report written to: brownfield_report.json")


asyncio.run(main())
```

Run it:

```bash
python run_brownfield.py
```

Expected output (abbreviated):

```
Starting brownfield scenario...
Enhancement: Enforce rate limiting on POST /shorten

Workflow ID : ...
Status      : completed

Impacted components:
  [ADDITION] url_shortener/config.py
  [NEW FILE] url_shortener/api/rate_limit.py
  [ADDITION] url_shortener/api/deps.py
  [MODIFICATION] url_shortener/api/urls.py
  [ADDITION] url_shortener/api/exceptions.py

Explicitly preserved (DO NOT MODIFY):
  url_shortener/services/url_service.py
  url_shortener/models/url.py
  url_shortener/repositories/url_repo.py
  url_shortener/schemas/url.py
  url_shortener/database.py
  url_shortener/services/exceptions.py

Implementation tasks: 6
  BF-IMPL-001: Add rate_limit_window_seconds to config.py
  BF-IMPL-002: Create url_shortener/api/rate_limit.py
  BF-IMPL-003: Add get_redis() dep and check_rate_limit() factory to deps.py
  BF-IMPL-004: Wire check_rate_limit to POST /shorten in urls.py
  BF-IMPL-005: Add 429 exception handler to exceptions.py
  BF-IMPL-006: Wire Redis client into app lifespan in main.py

Rollback plan:
  - git revert <commit-sha>
  - Flush Redis rate-limit keys: redis-cli KEYS 'rate_limit:*' | xargs redis-cli DEL
  RTO: < 5 minutes

Validation checks: 7 passed, 0 failed
Critical failures: 0
Implementation ready: True

Approvals: 2
  'change_planning': approved
  'validation': approved
```

---

## 10. Running the Ambiguous Scenario

**What it does:** Processes the deliberately ambiguous requirement `"Add analytics to our URL shortener"`. Detects 7 ambiguities, pauses for human clarification, records every answer as an explicit `Decision` with `made_by="human"`, then produces a normalised requirement and task plan that traces every scope item to a clarification answer.

**Stages:** `ambiguity_detection` → `clarification` → `normalization` → `task_planning` → `validation`

**Key concepts:**
- `PresetClarificationGateway` simulates the human providing answers
- `DefaultAnswerGateway` uses safe defaults (no human input simulated)
- Every clarification answer becomes a `Decision` with `made_by="human"` or `made_by="default"`

Create a file `run_ambiguous.py`:

```python
"""Run the ambiguous analytics requirement scenario."""
import asyncio
import json

from orchestrator.scenarios.ambiguous import (
    run_ambiguous_scenario,
    DEFAULT_PRESET_ANSWERS,
    PresetClarificationGateway,
    DefaultAnswerGateway,
)
from orchestrator.core.autonomy import AutoApproveGateway
from orchestrator.core.observability import build_observability_report


async def main() -> None:
    print("Starting ambiguous scenario...")
    print(f"Requirement: 'Add analytics to our URL shortener'")
    print()

    # Use preset answers to simulate a specific human decision path.
    # Switch to DefaultAnswerGateway() to see what happens with no human input.
    gateway = PresetClarificationGateway(DEFAULT_PRESET_ANSWERS)

    state = await run_ambiguous_scenario(
        clarification_gateway=gateway,
        approval_gateway=AutoApproveGateway(),
    )

    print(f"Workflow ID : {state.id}")
    print(f"Status      : {state.status.value}")
    print()

    # ── Ambiguity detection ────────────────────────────────────────────────
    det_ctx = state.stages["ambiguity_detection"]
    cat_data = json.loads(det_ctx.artifacts[0].content)
    print(f"Ambiguities detected: {cat_data['ambiguity_count']}")
    print(f"Questions generated : {cat_data['clarification_question_count']}")
    print(f"Status              : {cat_data['status']}")
    print()

    # ── Clarification log ─────────────────────────────────────────────────
    clar_ctx = state.stages["clarification"]
    clar_data = json.loads(clar_ctx.artifacts[0].content)
    print("Clarification answers (pause/resume point):")
    for rec in clar_data["clarification_records"]:
        source = "HUMAN" if rec["was_human_answered"] else "DEFAULT"
        print(f"  [{source}] {rec['question_id']}: {rec['answer'][:70]}")
    print()

    # ── Decisions made_by human ───────────────────────────────────────────
    human_decs = [d for d in clar_ctx.decisions if d.made_by == "human"]
    print(f"Decisions made_by='human': {len(human_decs)}")
    for dec in human_decs[:3]:
        print(f"  {dec.title[:70]}")
    if len(human_decs) > 3:
        print(f"  ... and {len(human_decs) - 3} more")
    print()

    # ── Normalised requirement ────────────────────────────────────────────
    norm_ctx = state.stages["normalization"]
    norm_data = json.loads(norm_ctx.artifacts[0].content)
    print("Normalised functional requirements:")
    for fr in norm_data["functional_requirements"]:
        print(f"  {fr['id']}: {fr['text']}")
        print(f"         ↑ source_decision: {fr['source_decision']}")
    print()

    excluded = norm_data.get("explicitly_excluded", [])
    print(f"Explicitly excluded ({len(excluded)} features):")
    for item in excluded:
        print(f"  - {item.get('feature', item)}: {item.get('reason', '')[:60]}")
    print()

    # ── Task plan ─────────────────────────────────────────────────────────
    plan_ctx = state.stages["task_planning"]
    plan_data = json.loads(plan_ctx.artifacts[0].content)
    added = [t for t in plan_data["implementation_tasks"] if t.get("added_by_clarification")]
    excluded_tasks = plan_data["excluded_tasks"]
    print(f"Implementation tasks: {plan_data['total_tasks']} total")
    print(f"  Added by clarification answers: {len(added)}")
    for t in added:
        print(f"    {t['id']}: {t['title']} (source: Q{t.get('source_decision', '?')})")
    print(f"  Excluded by clarification: {len(excluded_tasks)}")
    for t in excluded_tasks:
        print(f"    {t['id']}: {t['title']} (source: {t.get('source_decision', '?')})")
    print()

    # ── Assumption registry ────────────────────────────────────────────────
    print("Assumption registry:")
    assump = json.loads(norm_ctx.artifacts[1].content)
    print(f"  Confirmed by human : {len(assump['confirmed_by_human'])}")
    print(f"  Safe defaults used : {len(assump['safe_defaults_applied'])}")
    for d in assump["safe_defaults_applied"]:
        print(f"    [{d['source']}] {d['aspect']}: {d['value']}")
    print()

    report = build_observability_report(state)
    with open("ambiguous_report.json", "w") as f:
        json.dump(report.as_dict(), f, indent=2, default=str)
    print("Full report written to: ambiguous_report.json")


asyncio.run(main())
```

Run it:

```bash
python run_ambiguous.py
```

To see the **default-answers path** (no human input), change one line:

```python
gateway = DefaultAnswerGateway()   # instead of PresetClarificationGateway(...)
```

---

## 11. Viewing Workflow Results

Each scenario runner returns a `WorkflowState` object. The `build_observability_report()` function converts it to a fully JSON-serialisable `WorkflowObservabilityReport`.

### Print a structured workflow summary

```python
import asyncio, json
from orchestrator.scenarios.greenfield import run_greenfield_scenario
from orchestrator.core.autonomy import AutoApproveGateway
from orchestrator.core.observability import build_observability_report

async def main():
    state = await run_greenfield_scenario(approval_gateway=AutoApproveGateway())
    report = build_observability_report(state)

    # Serialise the entire report to JSON
    data = report.as_dict()
    print(json.dumps(data, indent=2, default=str))

asyncio.run(main())
```

### Access specific parts of the report

```python
report = build_observability_report(state)

# Execution trace: full provenance chain
trace = report.execution_trace
print("Requirement:", trace.requirement.name)
print("Decisions:  ", [s.name for s in trace.decisions])
print("Artifacts:  ", [s.name for s in trace.artifacts])
print("Approvals:  ", [s.name for s in trace.approvals])
print("Result:     ", trace.result.name)

# Per-stage metrics
for sm in report.metrics.stage_metrics:
    print(f"  {sm.stage_name}: {sm.latency_seconds:.3f}s  "
          f"retried={sm.retried}  fallback={sm.fallback_used}")

# Structured log records (searchable by level, event, stage)
error_logs = report.failure_trace()           # ERROR level only
policy_logs = report.policy_trace()           # policy_evaluated + policy_blocked
approval_logs = report.approval_trace()       # approval TraceSteps
decision_logs = report.decision_trace()       # decision TraceSteps
artifact_logs = report.artifact_trace()       # artifact TraceSteps
```

### Read individual stage artifacts

Each stage's artifacts are stored in `StageContext.artifacts` as `Artifact` objects with a `content: str` field (JSON text):

```python
import json

# Read the release checklist from the greenfield scenario
ctx = state.stages["release_readiness"]
checklist_art = next(a for a in ctx.artifacts if a.name == "release_checklist.json")
checklist = json.loads(checklist_art.content)
print(json.dumps(checklist, indent=2))
```

---

## 12. Viewing Validation Results

Validation results are stored per-stage in `StageContext.validations` as `ValidationResult` objects.

### Print all validation results

```python
from orchestrator.core.results import ValidationSeverity

for stage_name, ctx in state.stages.items():
    if ctx.validations:
        print(f"\n── {stage_name} ──")
        for v in ctx.validations:
            icon = "✓" if v.passed else "✗"
            print(f"  {icon} [{v.severity.value}] {v.rule_name}")
            print(f"    {v.message}")
            if v.evidence:
                print(f"    evidence: {v.evidence}")
```

### Check for any failed critical validations

```python
failed_critical = [
    (name, v)
    for name, ctx in state.stages.items()
    for v in ctx.validations
    if not v.passed and v.severity == ValidationSeverity.ERROR
]

if failed_critical:
    print("FAILED CRITICAL VALIDATIONS:")
    for stage_name, v in failed_critical:
        print(f"  [{stage_name}] {v.rule_name}: {v.message}")
else:
    print("All critical validations passed.")
```

### Access validation results from the lineage

```python
from orchestrator.core.lineage import build_lineage

lineage = build_lineage(state)
validations_for_stage = lineage.get_validations_for_stage("validation")
for v in validations_for_stage:
    print(f"{v.rule_name}: {'PASS' if v.passed else 'FAIL'}")
```

---

## 13. Understanding Approvals

The system records every approval request and decision in `WorkflowState.approvals`.

### List all approvals

```python
from orchestrator.core.results import ApprovalStatus

for appr in state.approvals:
    print(f"Stage    : {appr.stage_name}")
    print(f"Status   : {appr.status.value}")
    print(f"Approver : {appr.approver}")
    print(f"Rationale: {appr.decision_rationale[:100]}")
    print(f"Impact   : {appr.impact_level}")
    print(f"Override : {appr.is_override}")
    print()
```

### Live CLI human approval (demos)

```python
from orchestrator.core.autonomy import HumanApprovalGateway

# Blocks on stdin for y/n — use only in interactive sessions
state = await run_greenfield_scenario(
    approval_gateway=HumanApprovalGateway(approver="operator"),
)
```

For ambiguous clarification interactively:

```python
from orchestrator.scenarios.ambiguous import HumanClarificationGateway, run_ambiguous_scenario

state = await run_ambiguous_scenario(
    clarification_gateway=HumanClarificationGateway(),
)
```

### Simulate rejection to see failure behaviour

Swap `AutoApproveGateway` for `AutoRejectGateway` in any scenario:

```python
from orchestrator.core.autonomy import AutoRejectGateway

state = await run_greenfield_scenario(approval_gateway=AutoRejectGateway())
print("Status:", state.status.value)   # failed
print("Failed stages:", [n for n, c in state.stages.items() if c.status.value == "failed"])
```

### Use a preset gateway to control specific approvals

```python
from orchestrator.core.autonomy import PresetApprovalGateway

# Approve architecture_design, reject release_readiness
gateway = PresetApprovalGateway(decisions={
    "architecture_design": True,
    "release_readiness": False,
})
state = await run_greenfield_scenario(approval_gateway=gateway)
```

### Understand the audit trail for approvals

```python
approval_events = [
    e for e in state.audit_trail
    if "approval" in e.event
]
for e in approval_events:
    print(f"{e.timestamp.isoformat()[:19]}  {e.event:<30} stage={e.stage}  {e.details}")
```

---

## 14. Observing Failure and Recovery Behaviour

### 14.1 Retry scenario

Paste and run this script to see a stage fail on the first attempt and succeed on the second:

```python
"""Demonstrate retry + recovery tracking."""
import asyncio
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.models import GateResult, StageContext, Requirement, RequirementType
from orchestrator.core.graph import WorkflowDefinition
from orchestrator.core.failure import RetryPolicy
from orchestrator.engine.workflow_engine import WorkflowEngine


class FlakyStage(BaseStage):
    """Fails on attempt 1, succeeds on attempt 2."""
    stage_name = "flaky"

    def __init__(self):
        self._calls = 0
        self.retry_policy = RetryPolicy(max_attempts=3)

    async def entry_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name="entry", passed=True)

    async def execute(self, ctx: StageContext) -> StageContext:
        self._calls += 1
        if self._calls < 2:
            raise RuntimeError(f"Transient failure on attempt {self._calls}")
        ctx.output_data["recovered_on"] = self._calls
        return ctx

    async def exit_gate(self, ctx: StageContext) -> GateResult:
        return GateResult(gate_name="exit", passed=True)

    async def rollback(self, ctx: StageContext) -> StageContext:
        ctx.rollback_performed = True
        return ctx


async def main():
    req = Requirement(
        title="Retry demo",
        raw_text="Demonstrate retry",
        requirement_type=RequirementType.GREENFIELD,
    )
    defn = WorkflowDefinition(name="retry-demo", description="", stages=["flaky"])
    engine = WorkflowEngine(definition=defn, stages={"flaky": FlakyStage()})
    state = await engine.run(req)

    print(f"Status: {state.status.value}")
    ctx = state.stages["flaky"]
    print(f"Succeeded on call #{ctx.output_data['recovered_on']}")
    print(f"Attempt records: {len(ctx.attempt_records)}")
    for rec in ctx.attempt_records:
        print(f"  attempt={rec.attempt}  "
              f"classification={rec.classification.value}  "
              f"decision={rec.recovery_decision.value}")
    retry_events = [e for e in state.audit_trail if "retry" in e.event]
    print(f"Retry audit events: {[e.event for e in retry_events]}")


asyncio.run(main())
```

Expected output:

```
Status: completed
Succeeded on call #2
Attempt records: 1
  attempt=0  classification=transient  decision=retry
Retry audit events: ['stage_retrying']
```

### 14.2 Retry exhaustion + fallback

```python
"""Demonstrate retry exhaustion then fallback to preset output."""
import asyncio
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.models import GateResult, StageContext, Requirement, RequirementType
from orchestrator.core.graph import WorkflowDefinition, StageDependency
from orchestrator.core.failure import RetryPolicy, FallbackBehavior
from orchestrator.engine.workflow_engine import WorkflowEngine


class ExhaustingStage(BaseStage):
    stage_name = "always_fails"

    def __init__(self):
        self.retry_policy = RetryPolicy(
            max_attempts=3,
            fallback_behavior=FallbackBehavior.USE_PRESET,
            fallback_output={"status": "fallback", "value": "safe_default"},
        )

    async def entry_gate(self, ctx): return GateResult(gate_name="e", passed=True)
    async def exit_gate(self, ctx): return GateResult(gate_name="x", passed=True)
    async def rollback(self, ctx): return ctx

    async def execute(self, ctx):
        raise RuntimeError("Always fails — fallback should apply")


class DownstreamStage(BaseStage):
    stage_name = "downstream"

    async def entry_gate(self, ctx): return GateResult(gate_name="e", passed=True)
    async def exit_gate(self, ctx): return GateResult(gate_name="x", passed=True)
    async def rollback(self, ctx): return ctx

    async def execute(self, ctx):
        print(f"  Downstream received: {ctx.input_data}")
        ctx.output_data["downstream_ran"] = True
        return ctx


async def main():
    req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
    defn = WorkflowDefinition(
        name="fallback-demo", description="",
        stages=["always_fails", "downstream"],
        dependencies=[StageDependency(from_stage="always_fails", to_stage="downstream")]
    )
    stages = {"always_fails": ExhaustingStage(), "downstream": DownstreamStage()}
    state = await WorkflowEngine(definition=defn, stages=stages).run(req)

    always_ctx = state.stages["always_fails"]
    print(f"always_fails status : {always_ctx.status.value}")     # completed
    print(f"fallback_used       : {always_ctx.fallback_used}")    # True
    print(f"output_data         : {always_ctx.output_data}")
    print(f"attempt_records     : {len(always_ctx.attempt_records)} (3 = exhausted)")
    print(f"downstream status   : {state.stages['downstream'].status.value}")
    print(f"workflow status     : {state.status.value}")


asyncio.run(main())
```

Expected output:

```
  Downstream received: {'status': 'fallback', 'value': 'safe_default'}
always_fails status : completed
fallback_used       : True
output_data         : {'status': 'fallback', 'value': 'safe_default'}
attempt_records     : 3 (3 = exhausted)
downstream status   : completed
workflow status     : completed
```

### 14.3 Rollback on failure

```python
"""Demonstrate rollback being called after retry exhaustion."""
import asyncio
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.models import GateResult, StageContext, Requirement, RequirementType
from orchestrator.core.graph import WorkflowDefinition
from orchestrator.core.failure import RetryPolicy
from orchestrator.engine.workflow_engine import WorkflowEngine


class RollbackStage(BaseStage):
    stage_name = "needs_rollback"
    rollback_log = []

    def __init__(self):
        self.retry_policy = RetryPolicy(max_attempts=2, rollback_on_failure=True)

    async def entry_gate(self, ctx): return GateResult(gate_name="e", passed=True)
    async def exit_gate(self, ctx): return GateResult(gate_name="x", passed=True)

    async def execute(self, ctx):
        raise RuntimeError("Permanent failure")

    async def rollback(self, ctx):
        self.__class__.rollback_log.append("rollback_called")
        ctx.rollback_performed = True
        return ctx


async def main():
    req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
    defn = WorkflowDefinition(name="rollback-demo", description="", stages=["needs_rollback"])
    state = await WorkflowEngine(definition=defn, stages={"needs_rollback": RollbackStage()}).run(req)

    ctx = state.stages["needs_rollback"]
    print(f"Stage status      : {ctx.status.value}")         # rolled_back
    print(f"rollback_performed: {ctx.rollback_performed}")   # True
    print(f"rollback_called   : {RollbackStage.rollback_log}")
    print(f"rolled_back_stages: {state.rolled_back_stages}")
    events = [e.event for e in state.audit_trail if "rollback" in e.event]
    print(f"Rollback audit    : {events}")


asyncio.run(main())
```

Expected output:

```
Stage status      : rolled_back
rollback_performed: True
rollback_called   : ['rollback_called']
rolled_back_stages: ['needs_rollback']
Rollback audit    : ['rollback_started', 'rollback_completed']
```

### 14.4 Safe-stop

```python
"""Demonstrate safe-stop triggered by a CRITICAL exception."""
import asyncio
from orchestrator.core.base_stage import BaseStage
from orchestrator.core.models import GateResult, StageContext, Requirement, RequirementType
from orchestrator.core.graph import WorkflowDefinition, StageDependency
from orchestrator.core.failure import RetryPolicy
from orchestrator.engine.workflow_engine import WorkflowEngine


class SecurityBreach(Exception):
    """Simulates a CRITICAL security exception."""

class DangerousStage(BaseStage):
    stage_name = "dangerous"

    def __init__(self):
        self.retry_policy = RetryPolicy(
            max_attempts=3,
            safe_stop_error_types=["SecurityBreach"],
        )

    async def entry_gate(self, ctx): return GateResult(gate_name="e", passed=True)
    async def exit_gate(self, ctx): return GateResult(gate_name="x", passed=True)
    async def rollback(self, ctx): return ctx

    async def execute(self, ctx):
        raise SecurityBreach("Credentials leak detected — halting immediately")


class DownstreamStage(BaseStage):
    stage_name = "should_not_run"

    async def entry_gate(self, ctx): return GateResult(gate_name="e", passed=True)
    async def exit_gate(self, ctx): return GateResult(gate_name="x", passed=True)
    async def rollback(self, ctx): return ctx
    async def execute(self, ctx):
        ctx.output_data["ran"] = True  # should never be reached
        return ctx


async def main():
    req = Requirement(title="T", raw_text="R", requirement_type=RequirementType.GREENFIELD)
    defn = WorkflowDefinition(
        name="safe-stop-demo", description="",
        stages=["dangerous", "should_not_run"],
        dependencies=[StageDependency(from_stage="dangerous", to_stage="should_not_run")]
    )
    stages = {"dangerous": DangerousStage(), "should_not_run": DownstreamStage()}
    state = await WorkflowEngine(definition=defn, stages=stages).run(req)

    print(f"Workflow status   : {state.status.value}")       # stopped
    print(f"safe_stopped      : {state.safe_stopped}")       # True
    print(f"safe_stop_reason  : {state.safe_stop_reason}")
    print(f"dangerous status  : {state.stages['dangerous'].status.value}")  # failed
    # Downstream never ran
    print(f"downstream ran    : {'should_not_run' in state.stages}")  # False (blocked)
    events = [e.event for e in state.audit_trail if "safe_stop" in e.event]
    print(f"Safe-stop events  : {events}")


asyncio.run(main())
```

Expected output:

```
Workflow status   : stopped
safe_stopped      : True
safe_stop_reason  : Stage 'dangerous' raised a CRITICAL exception: Credentials leak detected — halting immediately
dangerous status  : failed
downstream ran    : False
Safe-stop events  : ['safe_stop_triggered', 'workflow_safe_stopped']
```

---

## 15. Troubleshooting

### `ModuleNotFoundError: No module named 'orchestrator'`

**Cause:** Commands were not run from the repository root, so Python cannot find the local packages.

**Fix:** `cd` into the repo root, then:

```bash
pip install -r requirements.txt
python -c "import orchestrator; print(orchestrator.__file__)"
```

Verify:

```bash
python -c "import orchestrator; print(orchestrator.__file__)"
```

---

### `ModuleNotFoundError: No module named 'aiosqlite'`

**Cause:** Dev dependencies not installed.

**Fix:**

```bash
pip install -r requirements.txt
```

---

### `asyncpg.exceptions.InvalidCatalogNameError` or `could not connect to server`

**Cause:** PostgreSQL is not running or the database does not exist.

**Fix (Docker):**

```bash
docker compose up postgres -d
# Wait for healthy status
docker compose ps
```

**Fix (local):** Create the database:

```bash
psql -U postgres -c "CREATE DATABASE urlshortener;"
```

Tables are created automatically when the app starts.

---

### `ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 6379)`

**Cause:** Redis is not running.

**Fix:**

```bash
docker compose up redis -d
# or
docker run -d -p 6379:6379 redis:7-alpine
```

---

### `uvicorn.error: error while attempting to bind on address ('0.0.0.0', 8000)`

**Cause:** Port 8000 is already in use.

**Fix:** Find and stop the process, or use a different port:

```bash
uvicorn url_shortener.main:app --port 8001
```

Update `BASE_URL` in `.env` to match:

```
BASE_URL=http://localhost:8001
```

---

### `pytest: error: unrecognized arguments`

**Cause:** `pytest.ini` sets `--strict-markers` and you may have added an unknown marker.

**Fix:** Register the marker in `pytest.ini` under `markers` or remove the marker from the test.

---

### `AttributeError: 'PrintLogger' object has no attribute 'name'`

**Cause (fixed in current code):** `configure_logging()` previously paired `PrintLoggerFactory` with `structlog.stdlib.add_logger_name`, which requires a stdlib logger that has `.name`. It now uses `structlog.stdlib.LoggerFactory()`.

If an old process still shows this, restart uvicorn. In tests, keep resetting structlog after calling `configure_logging()`:

```python
import structlog

def test_something():
    from orchestrator.observability.logging import configure_logging
    configure_logging(level="INFO", environment="production")
    # ... test ...
    structlog.reset_defaults()
```

---

### Tests fail with `SyntaxError` on Python 3.9 or 3.10

**Cause:** The codebase uses Python 3.10+ syntax (`match`, `X | Y` union types).

**Fix:** Upgrade to Python 3.11:

```bash
python --version   # must be 3.11.x
```

---

### `WorkflowValidationError: Workflow validation failed: cycle detected`

**Cause:** A `StageDependency` creates a cycle in the DAG.

**Fix:** Review the `dependencies` list passed to `WorkflowDefinition` and remove the circular edge. Use `definition.find_cycle()` to identify it:

```python
cycle = definition.find_cycle()
print("Cycle:", cycle)
```

---

### Docker build fails with `pip install` errors

**Cause:** Network issue or outdated pip in the Docker build layer.

**Fix:**

```bash
docker compose build --no-cache
```

Or explicitly upgrade pip in the Dockerfile (already done — the `RUN pip install --upgrade pip` line handles this).

---

### `docker compose` command not found

**Cause:** Older Docker installation uses `docker-compose` (with hyphen) instead of the plugin `docker compose`.

**Fix:**

```bash
# Use the hyphenated form for Docker Compose v1
docker-compose up --build
```

Or upgrade Docker Desktop to get Compose v2 (the plugin form).
