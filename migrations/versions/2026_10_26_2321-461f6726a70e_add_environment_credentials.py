"""add environment credentials

Per-environment provider credentials, encrypted by the backend (Phase 7C-1, D034), and
the signed-in actor of audit events.

Additive: no existing row changes. Credentials reference (environment, owner) so an
environment's owner cannot change while credentials set by the previous owner remain.

Revision ID: 461f6726a70e
Revises: f2b7c1d9a4e3
Create Date: 2026-10-26 23:21:31.418682

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "461f6726a70e"
down_revision: str | Sequence[str] | None = "f2b7c1d9a4e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint(op.f("uq_environments_id"), "environments", ["id", "owner_user_id"])
    op.create_table(
        "environment_credentials",
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "public_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("connection_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('nebius', 'signoz')",
            name=op.f("ck_environment_credentials_credentialkind"),
        ),
        sa.CheckConstraint(
            "(kind = 'signoz') = (connection_fingerprint IS NOT NULL)",
            name=op.f("ck_environment_credentials_fingerprint_for_signoz"),
        ),
        sa.CheckConstraint(
            "octet_length(nonce) = 12", name=op.f("ck_environment_credentials_nonce_length")
        ),
        sa.ForeignKeyConstraint(
            ["environment_id", "owner_user_id"],
            ["environments.id", "environments.owner_user_id"],
            name=op.f("fk_environment_credentials_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=op.f("fk_environment_credentials_updated_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("environment_id", "kind", name=op.f("pk_environment_credentials")),
    )
    op.create_index(
        op.f("ix_environment_credentials_connection_fingerprint"),
        "environment_credentials",
        ["connection_fingerprint"],
        unique=False,
    )
    op.add_column("audit_events", sa.Column("actor_user_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_audit_events_actor_user_id"), "audit_events", ["actor_user_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_audit_events_actor_user_id_users"),
        "audit_events",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Downgrade schema. Refused once credentials or user-attributed audit events exist:
    dropping them would silently discard owners' configuration and audit history."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM environment_credentials)
               OR EXISTS (SELECT 1 FROM audit_events WHERE actor_user_id IS NOT NULL) THEN
                RAISE EXCEPTION 'environment credentials or user audit events exist; '
                    'refusing to drop them';
            END IF;
        END $$;
        """
    )
    op.drop_constraint(
        op.f("fk_audit_events_actor_user_id_users"), "audit_events", type_="foreignkey"
    )
    op.drop_index(op.f("ix_audit_events_actor_user_id"), table_name="audit_events")
    op.drop_column("audit_events", "actor_user_id")
    op.drop_index(
        op.f("ix_environment_credentials_connection_fingerprint"),
        table_name="environment_credentials",
    )
    op.drop_table("environment_credentials")
    op.drop_constraint(op.f("uq_environments_id"), "environments", type_="unique")
