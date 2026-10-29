"""add dedicated connection verification

Operator verifications of dedicated SigNoz connections (Phase 7D-2B closeout, D038).
Nothing existing is verified: outside local development, dedicated connections stay
refused until an operator verifies them (scripts.bind_signoz_tenant review / verify).
The downgrade is refused while verifications exist, so they are not lost silently.

Revision ID: da5ac09860ea
Revises: 8b3d105d8150
Create Date: 2026-10-28 18:36:15.096887

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "da5ac09860ea"
down_revision: str | Sequence[str] | None = "8b3d105d8150"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "signoz_dedicated_connections",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("connection_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("verified_by", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_signoz_dedicated_connections_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signoz_dedicated_connections")),
        sa.UniqueConstraint(
            "connection_fingerprint",
            name=op.f("uq_signoz_dedicated_connections_connection_fingerprint"),
        ),
    )
    op.create_index(
        op.f("ix_signoz_dedicated_connections_organization_id"),
        "signoz_dedicated_connections",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().execute(sa.text("SELECT 1 FROM signoz_dedicated_connections LIMIT 1")).first():
        raise RuntimeError(
            "refusing to downgrade: dedicated connection verifications exist "
            "(remove them with scripts.bind_signoz_tenant unverify first)"
        )
    op.drop_index(
        op.f("ix_signoz_dedicated_connections_organization_id"),
        table_name="signoz_dedicated_connections",
    )
    op.drop_table("signoz_dedicated_connections")
