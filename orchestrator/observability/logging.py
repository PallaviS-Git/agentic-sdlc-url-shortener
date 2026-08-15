"""
Structured logging bootstrap for the Agentic SDLC system.

Call configure_logging() once at application startup (in FastAPI lifespan
or orchestrator __main__). All subsequent structlog calls will use the
configured processors without re-initialisation.

Outputs:
  - production  → JSON to stdout (machine-parseable, suitable for log aggregators)
  - development → coloured human-readable console output
"""
from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(
    level: str = "INFO",
    environment: str = "development",
) -> None:
    """
    Configure structlog with shared processors.

    Args:
        level:       Python logging level name (DEBUG, INFO, WARNING, ERROR).
        environment: Controls renderer: 'production' → JSON, else console.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.stdlib.add_logger_name,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    if environment == "production":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Also configure the stdlib root logger so third-party libraries that
    # use logging.getLogger() emit at the same level.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_audit_logger() -> structlog.BoundLogger:
    """
    Return a logger pre-bound with the 'audit' context key.

    Use this logger exclusively for AuditEntry emissions so that audit
    events can be filtered/routed separately from operational logs.
    """
    return structlog.get_logger("audit").bind(log_type="audit")
