# Agentic SDLC — URL Shortener

A production-grade **Agentic Software Engineering System** that demonstrates automated SDLC orchestration. The system uses a DAG-based orchestration engine with entry/exit gates, parallel execution, human approval checkpoints, retries, rollback, and audit-grade traceability. The **URL shortener service** is the engineering artifact the orchestrator produces and reasons about.

Scenario stages **simulate** agent work (structured artifacts) on the real engine; see [docs/engineering-summary.md](docs/engineering-summary.md) for scope, limitations, and judgment.

---

## Quick Start

```bash
git clone <repo>
cd agentic-sdlc-url-shortener
cp .env.example .env
docker-compose up --build
```

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

Full setup (venv, tests, scenarios): [docs/setup.md](docs/setup.md)

---

## Development Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env

# Start backing services only (URL shortener against Postgres)
docker-compose up postgres redis -d

# Run the service
uvicorn url_shortener.main:app --reload

# Unit + orchestrator + scenario tests (no Docker)
python -m pytest tests/ --ignore=tests/integration

# Integration tests (ASGI + SQLite fixtures)
python -m pytest tests/integration/
```

---

## Directory Guide

| Path | Contents |
|---|---|
| `orchestrator/core/` | Domain models, gates context, governance, autonomy, failure, lineage, observability, replanning, ABCs |
| `orchestrator/engine/` | `WorkflowEngine`, task scheduler |
| `orchestrator/scenarios/` | Greenfield, brownfield, ambiguous stage implementations + runners |
| `orchestrator/policies/` | Re-exports of governance policies (SEC / COMP / CHANGE_CONTROL) |
| `orchestrator/stages/` | Package reserved for shared stages; concrete stages live under `scenarios/` |
| `orchestrator/observability/` | Structured logging bootstrap used by the URL shortener |
| `url_shortener/` | FastAPI service: config, API, models, services, repositories, schemas |
| `tests/unit/` | Pure unit tests |
| `tests/integration/` | ASGI integration tests |
| `tests/orchestrator/` | Engine and orchestration unit tests |
| `tests/scenarios/` | End-to-end scenario tests |
| `docs/` | Architecture, setup, engineering summary |

---

## Documentation

| Doc | Purpose |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, orchestration, control flow |
| [docs/setup.md](docs/setup.md) | Install, run, test, scenarios, troubleshooting |
| [docs/engineering-summary.md](docs/engineering-summary.md) | Testing, validation, scope, risks, trade-offs, limitations, assumptions, judgment |
