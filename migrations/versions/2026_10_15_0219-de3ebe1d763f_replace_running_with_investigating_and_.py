"""replace running with investigating and correlating

Revision ID: de3ebe1d763f
Revises: fa4cef7d3fdb
Create Date: 2026-10-15 02:19:50.454593

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "de3ebe1d763f"
down_revision: str | Sequence[str] | None = "fa4cef7d3fdb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Autogenerate does not detect CHECK changes, so this migration is hand-written.
CONSTRAINT = "ck_investigations_investigationstatus"
OLD_VALUES = "('pending', 'running', 'completed', 'failed', 'cancelled')"
NEW_VALUES = "('pending', 'investigating', 'correlating', 'completed', 'failed', 'cancelled')"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(op.f(CONSTRAINT), "investigations", type_="check")
    op.execute("UPDATE investigations SET status = 'investigating' WHERE status = 'running'")
    op.create_check_constraint(op.f(CONSTRAINT), "investigations", f"status IN {NEW_VALUES}")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(op.f(CONSTRAINT), "investigations", type_="check")
    op.execute(
        "UPDATE investigations SET status = 'running' "
        "WHERE status IN ('investigating', 'correlating')"
    )
    op.create_check_constraint(op.f(CONSTRAINT), "investigations", f"status IN {OLD_VALUES}")
