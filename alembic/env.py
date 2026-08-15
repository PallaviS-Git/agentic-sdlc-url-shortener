"""
Alembic migration environment.

DATABASE_URL is read from the environment (never hardcoded).
The async driver prefix (+asyncpg) is stripped for the synchronous
SQLAlchemy engine used by Alembic's migration runner.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# ── Alembic config object ──────────────────────────────────────────────────
config = context.config

# Set the DB URL from the environment, stripping the async driver prefix
# so Alembic's synchronous runner can connect.
_database_url = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/urlshortener",
).replace("+asyncpg", "")

config.set_main_option("sqlalchemy.url", _database_url)

# Wire stdlib logging to the alembic.ini [loggers] config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Base.metadata is populated for --autogenerate
from url_shortener.database import Base  # noqa: E402
import url_shortener.models.url  # noqa: E402, F401 — registers ShortUrl with Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (generates SQL script)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
