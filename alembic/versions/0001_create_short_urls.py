"""create short_urls table

Revision ID: 0001
Revises:
Create Date: 2026-08-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "short_urls",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_short_urls")),
        sa.UniqueConstraint("code", name=op.f("uq_short_urls_code")),
    )
    op.create_index(
        op.f("ix_short_urls_code"),
        "short_urls",
        ["code"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_short_urls_code"), table_name="short_urls")
    op.drop_table("short_urls")
