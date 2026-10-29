"""add rate limit counters

Shared request-rate counters (Phase 7D-2C-1, D039; audit H3). Additive: nothing existing
changes. Rows live only for their window; the downgrade drops in-flight counts, which
only resets current rate windows.

Revision ID: 0b9ac8fb6d48
Revises: da5ac09860ea
Create Date: 2026-10-28 20:19:29.764935

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0b9ac8fb6d48"
down_revision: str | Sequence[str] | None = "da5ac09860ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rate_limit_counters",
        sa.Column("bucket", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("bucket", "window_start", name=op.f("pk_rate_limit_counters")),
    )
    op.create_index(
        op.f("ix_rate_limit_counters_expires_at"),
        "rate_limit_counters",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_rate_limit_counters_expires_at"), table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")
