"""add tenant ownership

Organization ownership of environments and investigations, environment owners, turn and
investigation creators, operator audit events (Phase 7B, D032/D033).

Additive for existing data: every existing row keeps its values and stays ownerless
(organization_id NULL, quarantined). The only relaxed constraint is environment name
uniqueness, now per organization (still global among ownerless rows).

A deferred constraint trigger keeps every investigation's environments in the
investigation's organization, and forbids moving an owned row to another organization.
Assignments, evidence, hypotheses and findings inherit that through their existing scope
foreign keys onto investigation_environments.

Revision ID: f2b7c1d9a4e3
Revises: 815a7fcfbb5e
Create Date: 2026-10-26 21:12:00.479063

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2b7c1d9a4e3"
down_revision: str | Sequence[str] | None = "815a7fcfbb5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_SCOPE_FUNCTION = """
CREATE FUNCTION inquest_check_tenant_scope() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    inv uuid;
    env uuid;
BEGIN
    -- Fields are resolved per table: each branch only touches its own row type.
    IF TG_TABLE_NAME = 'investigation_environments' THEN
        inv := NEW.investigation_id;
        env := NEW.environment_id;
    ELSE
        IF OLD.organization_id IS NOT NULL
           AND NEW.organization_id IS DISTINCT FROM OLD.organization_id THEN
            RAISE EXCEPTION 'the organization of % % cannot change', TG_TABLE_NAME, NEW.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF TG_TABLE_NAME = 'investigations' THEN
            inv := NEW.id;
        ELSE
            env := NEW.id;
        END IF;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM investigation_environments ie
        JOIN investigations i ON i.id = ie.investigation_id
        JOIN environments e ON e.id = ie.environment_id
        WHERE i.organization_id IS DISTINCT FROM e.organization_id
          AND (inv IS NULL OR ie.investigation_id = inv)
          AND (env IS NULL OR ie.environment_id = env)
    ) THEN
        RAISE EXCEPTION 'investigation and environment belong to different organizations'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;
"""

# Deferred to commit: adoption moves an environment and its investigations in one
# transaction, and creation adds the scope row before or after either side.
TRIGGERS = {
    "investigation_environments": "AFTER INSERT OR UPDATE",
    "investigations": "AFTER UPDATE OF organization_id",
    "environments": "AFTER UPDATE OF organization_id",
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "audit_events",
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column(
            "details_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
            name=op.f("fk_audit_events_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_organization_id"), "audit_events", ["organization_id"], unique=False
    )

    op.add_column("environments", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.add_column("environments", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "environments",
        sa.Column(
            "process_credentials", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.drop_constraint(op.f("uq_environments_name"), "environments", type_="unique")
    op.create_index(
        op.f("ix_environments_organization_id"), "environments", ["organization_id"], unique=False
    )
    op.create_index(
        op.f("ix_environments_owner_user_id"), "environments", ["owner_user_id"], unique=False
    )
    op.create_unique_constraint(
        "uq_environments_organization_id_name",
        "environments",
        ["organization_id", "name"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_foreign_key(
        op.f("fk_environments_organization_id_organizations"),
        "environments",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_environments_owner_user_id_users"),
        "environments",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_environments_owner_with_organization"),
        "environments",
        "(organization_id IS NULL) = (owner_user_id IS NULL)",
    )

    op.add_column("investigation_turns", sa.Column("created_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_investigation_turns_created_by_user_id_users"),
        "investigation_turns",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column("investigations", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.add_column("investigations", sa.Column("created_by_user_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_investigations_organization_id_created_at",
        "investigations",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_investigations_created_by_user_id_users"),
        "investigations",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_investigations_organization_id_organizations"),
        "investigations",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_investigations_creator_with_organization"),
        "investigations",
        "created_by_user_id IS NULL OR organization_id IS NOT NULL",
    )

    op.execute(TENANT_SCOPE_FUNCTION)
    for table, when in TRIGGERS.items():
        op.execute(
            f"CREATE CONSTRAINT TRIGGER tenant_scope {when} ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION inquest_check_tenant_scope()"
        )


def downgrade() -> None:
    """Downgrade schema. Refused once tenant data exist: dropping the columns would leave
    owned records looking ownerless, and global name uniqueness may no longer hold."""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM environments WHERE organization_id IS NOT NULL)
               OR EXISTS (SELECT 1 FROM investigations WHERE organization_id IS NOT NULL) THEN
                RAISE EXCEPTION 'tenant-owned environments or investigations exist; '
                    'refusing to drop ownership';
            END IF;
        END $$;
        """
    )
    for table in TRIGGERS:
        op.execute(f"DROP TRIGGER tenant_scope ON {table}")
    op.execute("DROP FUNCTION inquest_check_tenant_scope()")

    op.drop_constraint(
        op.f("ck_investigations_creator_with_organization"), "investigations", type_="check"
    )
    op.drop_constraint(
        op.f("fk_investigations_organization_id_organizations"),
        "investigations",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_investigations_created_by_user_id_users"), "investigations", type_="foreignkey"
    )
    op.drop_index("ix_investigations_organization_id_created_at", table_name="investigations")
    op.drop_column("investigations", "created_by_user_id")
    op.drop_column("investigations", "organization_id")
    op.drop_constraint(
        op.f("fk_investigation_turns_created_by_user_id_users"),
        "investigation_turns",
        type_="foreignkey",
    )
    op.drop_column("investigation_turns", "created_by_user_id")
    op.drop_constraint(
        op.f("ck_environments_owner_with_organization"), "environments", type_="check"
    )
    op.drop_constraint(
        op.f("fk_environments_owner_user_id_users"), "environments", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_environments_organization_id_organizations"), "environments", type_="foreignkey"
    )
    op.drop_constraint("uq_environments_organization_id_name", "environments", type_="unique")
    op.drop_index(op.f("ix_environments_owner_user_id"), table_name="environments")
    op.drop_index(op.f("ix_environments_organization_id"), table_name="environments")
    op.create_unique_constraint(op.f("uq_environments_name"), "environments", ["name"])
    op.drop_column("environments", "process_credentials")
    op.drop_column("environments", "owner_user_id")
    op.drop_column("environments", "organization_id")
    op.drop_index(op.f("ix_audit_events_organization_id"), table_name="audit_events")
    op.drop_table("audit_events")
