# Agentic SDLC — URL Shortener

A production-grade **Agentic Software Engineering System** that demonstrates automated SDLC orchestration. The system uses a DAG-based orchestration engine with entry/exit gates, parallel execution, human approval checkpoints, retries, rollback, and audit-grade traceability. The **URL shortener service** is the engineering artifact the orchestrator produces.

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

---

## Development Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -e ".[dev]"
cp .env.example .env

# Start backing services only
docker-compose up postgres redis -d

# Run the service
uvicorn url_shortener.main:app --reload

# Run tests (unit — no infra required)
pytest -m unit

# Run all tests (requires postgres + redis)
pytest
```

---

## Directory Guide

| Path | Contents |
|---|---|
| `orchestrator/core/` | Domain models, abstract base classes for agents/stages/orchestrator |
| `orchestrator/stages/` | Concrete SDLC stage implementations (added in Step 5) |
| `orchestrator/policies/` | Security, compliance, change-control guardrails (Step 6) |
| `orchestrator/observability/` | Structured logging bootstrap, audit logger, metrics |
| `orchestrator/scenarios/` | Greenfield, brownfield, and ambiguous scenario drivers (Steps 7–8) |
| `url_shortener/` | FastAPI service: config, API routers, models, services, repositories, analytics |
| `tests/unit/` | Pure unit tests — no I/O required |
| `tests/integration/` | Tests requiring live postgres + redis |
| `tests/orchestrator/` | Orchestration engine and stage tests |
| `alembic/` | Database migration scripts |
| `docs/` | Architecture documentation |

See [docs/architecture.md](docs/architecture.md) for the full system design.
