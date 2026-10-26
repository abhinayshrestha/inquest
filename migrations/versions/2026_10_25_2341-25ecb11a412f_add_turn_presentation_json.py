"""add turn presentation_json

Revision ID: 25ecb11a412f
Revises: 0970ca43d320
Create Date: 2026-10-25 23:41:19.159930

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "25ecb11a412f"
down_revision: str | Sequence[str] | None = "0970ca43d320"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrors MAX_PRESENTATION_JSON_BYTES in src/models/turn.py at the time of this revision.
MAX_PRESENTATION_JSON_BYTES = 131_072


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "investigation_turns",
        sa.Column("presentation_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_investigation_turns_presentation_when_done"),
        "investigation_turns",
        "presentation_json IS NULL OR status = 'completed'",
    )
    op.create_check_constraint(
        op.f("ck_investigation_turns_presentation_json_size"),
        "investigation_turns",
        f"octet_length(presentation_json::text) <= {MAX_PRESENTATION_JSON_BYTES}",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        op.f("ck_investigation_turns_presentation_json_size"), "investigation_turns", type_="check"
    )
    op.drop_constraint(
        op.f("ck_investigation_turns_presentation_when_done"), "investigation_turns", type_="check"
    )
    op.drop_column("investigation_turns", "presentation_json")
