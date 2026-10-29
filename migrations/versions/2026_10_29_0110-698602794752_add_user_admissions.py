"""add user admissions

Sign-up admission control (Phase 7D-2E-1, D042; audit H3). Additive: nothing existing
changes and no user is admitted by the upgrade; under INQUEST_AUTH_ADMISSION=approval
existing users need an explicit operator decision (scripts.admit_user). The downgrade
refuses while admission decisions exist: dropping them would lose operator approvals and
withdrawals.

Revision ID: 698602794752
Revises: 8a49eb4cbecd
Create Date: 2026-10-29 01:10:49.907135

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "698602794752"
down_revision: str | Sequence[str] | None = "8a49eb4cbecd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_admissions",
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "source IN ('operator', 'invitation')", name=op.f("ck_user_admissions_source_known")
        ),
        sa.CheckConstraint(
            "email IS NOT NULL OR user_id IS NOT NULL", name=op.f("ck_user_admissions_has_target")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_admissions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_admissions")),
        sa.UniqueConstraint("user_id", name=op.f("uq_user_admissions_user_id")),
    )
    op.create_index(
        "uq_user_admissions_one_pending_email",
        "user_admissions",
        ["email"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL AND revoked_at IS NULL"),
    )
    op.add_column(
        "auth_login_states", sa.Column("invitation_hash", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM user_admissions")):
        raise RuntimeError(
            "user_admissions holds admission decisions; downgrading would lose them "
            "(export them first, then delete them deliberately)"
        )
    op.drop_column("auth_login_states", "invitation_hash")
    op.drop_index(
        "uq_user_admissions_one_pending_email",
        table_name="user_admissions",
        postgresql_where=sa.text("user_id IS NULL AND revoked_at IS NULL"),
    )
    op.drop_table("user_admissions")
