"""add telemetry isolation

SigNoz connection isolation modes and operator-bound ingestion keys (Phase 7D-2B, D037).

Existing SigNoz credentials become `dedicated`, the mode their behaviour matches (the
environment owner's own connection); nothing else changes for them. The downgrade is
refused while provider-scoped connections or ingestion key bindings exist: without this
schema their queries would lose the mandatory tenant filter.

Revision ID: 8b3d105d8150
Revises: 6b04637acc00
Create Date: 2026-10-28 15:45:06.452881

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b3d105d8150"
down_revision: str | Sequence[str] | None = "6b04637acc00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "signoz_tenant_keys",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("connection_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "key_id ~ '^[A-Za-z0-9_-]{1,64}$'", name=op.f("ck_signoz_tenant_keys_key_id_format")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_signoz_tenant_keys_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signoz_tenant_keys")),
        sa.UniqueConstraint(
            "connection_fingerprint",
            "key_id",
            name=op.f("uq_signoz_tenant_keys_connection_fingerprint"),
        ),
    )
    op.create_index(
        op.f("ix_signoz_tenant_keys_organization_id"),
        "signoz_tenant_keys",
        ["organization_id"],
        unique=False,
    )
    op.add_column(
        "environment_credentials", sa.Column("isolation", sa.String(length=32), nullable=True)
    )
    op.execute("UPDATE environment_credentials SET isolation = 'dedicated' WHERE kind = 'signoz'")
    op.create_check_constraint(
        op.f("ck_environment_credentials_telemetryisolation"),
        "environment_credentials",
        "isolation IN ('dedicated', 'provider_scoped')",
    )
    op.create_check_constraint(
        op.f("ck_environment_credentials_isolation_for_signoz"),
        "environment_credentials",
        "(kind = 'signoz') = (isolation IS NOT NULL)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM environment_credentials WHERE isolation = 'provider_scoped')
               OR EXISTS (SELECT 1 FROM signoz_tenant_keys) THEN
                RAISE EXCEPTION 'provider-scoped SigNoz connections or ingestion key bindings '
                    'exist; remove them before downgrading';
            END IF;
        END $$;
        """
    )
    op.drop_constraint(
        op.f("ck_environment_credentials_isolation_for_signoz"), "environment_credentials"
    )
    op.drop_constraint(
        op.f("ck_environment_credentials_telemetryisolation"), "environment_credentials"
    )
    op.drop_column("environment_credentials", "isolation")
    op.drop_index(op.f("ix_signoz_tenant_keys_organization_id"), table_name="signoz_tenant_keys")
    op.drop_table("signoz_tenant_keys")
