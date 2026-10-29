"""add environment transfers

Environment ownership transfers (Phase 7C-3, D035) and the rule that an environment's
owner is a current member of its organization.

Additive for existing rows. The membership foreign key is refused (with a count, no
identities) if an owned environment's owner already left its organization: an operator
must resolve those first, nothing is reassigned automatically.

Revision ID: 4b8937624c7f
Revises: 461f6726a70e
Create Date: 2026-10-27 22:28:32.822691

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b8937624c7f"
down_revision: str | Sequence[str] | None = "461f6726a70e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        DO $$
        DECLARE orphaned bigint;
        BEGIN
            SELECT count(*) INTO orphaned FROM environments e
            WHERE e.owner_user_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM organization_memberships m
                WHERE m.organization_id = e.organization_id AND m.user_id = e.owner_user_id
            );
            IF orphaned > 0 THEN
                RAISE EXCEPTION '% environments are owned by users who are no longer members '
                    'of their organization; resolve them before upgrading', orphaned;
            END IF;
        END $$;
        """
    )
    op.create_table(
        "environment_transfers",
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("from_user_id", sa.Uuid(), nullable=False),
        sa.Column("to_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "(status = 'pending') = (resolved_at IS NULL AND resolved_by_user_id IS NULL)",
            name=op.f("ck_environment_transfers_resolved_when_closed"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'declined', 'cancelled')",
            name=op.f("ck_environment_transfers_transferstatus"),
        ),
        sa.CheckConstraint(
            "from_user_id <> to_user_id", name=op.f("ck_environment_transfers_different_users")
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_environment_transfers_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_user_id"],
            ["users.id"],
            name=op.f("fk_environment_transfers_from_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"],
            ["users.id"],
            name=op.f("fk_environment_transfers_resolved_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_user_id"],
            ["users.id"],
            name=op.f("fk_environment_transfers_to_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environment_transfers")),
    )
    op.create_index(
        op.f("ix_environment_transfers_environment_id"),
        "environment_transfers",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_environment_transfers_to_user_id"),
        "environment_transfers",
        ["to_user_id"],
        unique=False,
    )
    op.create_index(
        "uq_environment_transfers_pending",
        "environment_transfers",
        ["environment_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_foreign_key(
        op.f("fk_environments_organization_id_organization_memberships"),
        "environments",
        "organization_memberships",
        ["organization_id", "owner_user_id"],
        ["organization_id", "user_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Downgrade schema. Refused once transfers exist: dropping them would discard the
    record of who handed an environment to whom (the audit events stay either way)."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM environment_transfers) THEN
                RAISE EXCEPTION 'environment transfers exist; refusing to drop them';
            END IF;
        END $$;
        """
    )
    op.drop_constraint(
        op.f("fk_environments_organization_id_organization_memberships"),
        "environments",
        type_="foreignkey",
    )
    op.drop_index(
        "uq_environment_transfers_pending",
        table_name="environment_transfers",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(op.f("ix_environment_transfers_to_user_id"), table_name="environment_transfers")
    op.drop_index(
        op.f("ix_environment_transfers_environment_id"), table_name="environment_transfers"
    )
    op.drop_table("environment_transfers")
