"""add turns, investigation environments, reports, progress events and idle status

Revision ID: 94f0df921964
Revises: 9ce93924a639
Create Date: 2026-10-17 17:39:34.934836

Hand-ordered: tables first, then backfills of existing investigations, then the
NOT NULL / FK constraints that the backfills make satisfiable. Existing evidence,
hypotheses, findings and assignments are untouched apart from gaining a turn_id.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "94f0df921964"
down_revision: str | Sequence[str] | None = "9ce93924a639"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_CHECK = "ck_investigations_investigationstatus"
OLD_STATUSES = "('pending', 'investigating', 'correlating', 'completed', 'failed', 'cancelled')"
NEW_STATUSES = (
    "('pending', 'investigating', 'correlating', 'idle', 'completed', 'failed', 'cancelled')"
)
TURN_STATUSES = "('pending', 'running', 'completed', 'failed', 'cancelled')"


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate does not detect CHECK changes.
    op.drop_constraint(op.f(STATUS_CHECK), "investigations", type_="check")
    op.create_check_constraint(op.f(STATUS_CHECK), "investigations", f"status IN {NEW_STATUSES}")

    op.create_table(
        "investigation_environments",
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("environment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_investigation_environments_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_environments_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "investigation_id", "environment_id", name=op.f("pk_investigation_environments")
        ),
    )
    op.create_index(
        op.f("ix_investigation_environments_environment_id"),
        "investigation_environments",
        ["environment_id"],
    )

    op.create_table(
        "investigation_turns",
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("answer_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
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
            "(seq = 1) = (kind = 'initial')", name=op.f("ck_investigation_turns_initial_is_first")
        ),
        sa.CheckConstraint(
            "answer_json IS NULL OR status = 'completed'",
            name=op.f("ck_investigation_turns_answer_when_done"),
        ),
        sa.CheckConstraint(
            "decision IN ('reused_evidence', 'investigated', 'clarification')",
            name=op.f("ck_investigation_turns_turndecision"),
        ),
        sa.CheckConstraint(
            "kind IN ('initial', 'follow_up')", name=op.f("ck_investigation_turns_turnkind")
        ),
        sa.CheckConstraint(
            f"status IN {TURN_STATUSES}", name=op.f("ck_investigation_turns_turnstatus")
        ),
        sa.CheckConstraint(
            "char_length(question) BETWEEN 1 AND 10000",
            name=op.f("ck_investigation_turns_question_length"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name=op.f("ck_investigation_turns_completed_after_started"),
        ),
        sa.CheckConstraint(
            "octet_length(answer_json::text) <= 65536",
            name=op.f("ck_investigation_turns_answer_json_size"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_investigation_turns_seq_positive")),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_turns_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_turns")),
        sa.UniqueConstraint("investigation_id", "id", name="uq_investigation_turns_scope"),
        sa.UniqueConstraint("investigation_id", "seq", name="uq_investigation_turns_seq"),
    )
    op.create_index(
        "uq_investigation_turns_one_active",
        "investigation_turns",
        ["investigation_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )

    op.create_table(
        "investigation_reports",
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "octet_length(report_json::text) <= 262144",
            name=op.f("ck_investigation_reports_report_json_size"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_investigation_reports_version_positive")),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_reports_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "investigation_id"],
            ["investigation_turns.id", "investigation_turns.investigation_id"],
            name=op.f("fk_investigation_reports_turn_id_investigation_turns"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_reports")),
        sa.UniqueConstraint("investigation_id", "version", name="uq_investigation_reports_version"),
        sa.UniqueConstraint("turn_id", name="uq_investigation_reports_turn_id"),
    )

    # Before progress_events: its assignment FK targets this constraint.
    op.create_unique_constraint(
        "uq_investigation_assignments_investigation_id_id",
        "investigation_assignments",
        ["investigation_id", "id"],
    )
    op.create_table(
        "progress_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investigation_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column(
            "payload_json",
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
        sa.CheckConstraint(
            "type ~ '^[a-z][a-z_]*(\\.[a-z][a-z_]*)+$'", name=op.f("ck_progress_events_type_format")
        ),
        sa.CheckConstraint(
            "octet_length(payload_json::text) <= 8192", name=op.f("ck_progress_events_payload_size")
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id", "investigation_id"],
            ["investigation_assignments.id", "investigation_assignments.investigation_id"],
            name=op.f("fk_progress_events_assignment_id_investigation_assignments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_progress_events_investigation_id_investigations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id", "investigation_id"],
            ["investigation_turns.id", "investigation_turns.investigation_id"],
            name=op.f("fk_progress_events_turn_id_investigation_turns"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_progress_events")),
    )
    op.create_index(
        "ix_progress_events_investigation_id_id", "progress_events", ["investigation_id", "id"]
    )

    # --- backfill existing investigations ------------------------------------------------
    # Scope: every environment an investigation already has an assignment for.
    op.execute(
        """
        INSERT INTO investigation_environments (investigation_id, environment_id, created_at)
        SELECT investigation_id, environment_id, min(created_at)
        FROM investigation_assignments
        GROUP BY investigation_id, environment_id
        """
    )
    # Turn 1 for every investigation: its original question, its lifecycle mapped onto the
    # turn's. investigating/correlating mean the question is still being worked on.
    op.execute(
        """
        INSERT INTO investigation_turns
            (id, investigation_id, seq, kind, question, status, started_at, completed_at,
             created_at, updated_at)
        SELECT gen_random_uuid(), id, 1, 'initial', left(question, 10000),
               CASE status
                   WHEN 'pending' THEN 'pending'
                   WHEN 'investigating' THEN 'running'
                   WHEN 'correlating' THEN 'running'
                   ELSE status
               END,
               started_at, completed_at, created_at, now()
        FROM investigations
        """
    )
    op.add_column("investigation_assignments", sa.Column("turn_id", sa.Uuid(), nullable=True))
    # Every existing assignment was created for the original question.
    op.execute(
        """
        UPDATE investigation_assignments a
        SET turn_id = t.id
        FROM investigation_turns t
        WHERE t.investigation_id = a.investigation_id AND t.seq = 1
        """
    )
    # A stored result becomes report version 1 of turn 1; the column stays as the snapshot.
    op.execute(
        """
        INSERT INTO investigation_reports
            (id, investigation_id, turn_id, version, report_json, created_at)
        SELECT gen_random_uuid(), i.id, t.id, 1, i.final_result_json,
               coalesce(i.completed_at, i.updated_at)
        FROM investigations i
        JOIN investigation_turns t ON t.investigation_id = i.id AND t.seq = 1
        WHERE i.final_result_json IS NOT NULL
        """
    )

    op.alter_column("investigation_assignments", "turn_id", nullable=False)
    op.create_index(
        "ix_investigation_assignments_turn_id", "investigation_assignments", ["turn_id"]
    )
    op.create_foreign_key(
        op.f("fk_investigation_assignments_turn_id_investigation_turns"),
        "investigation_assignments",
        "investigation_turns",
        ["turn_id", "investigation_id"],
        ["id", "investigation_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_investigation_assignments_scope",
        "investigation_assignments",
        "investigation_environments",
        ["investigation_id", "environment_id"],
        ["investigation_id", "environment_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    """Downgrade schema. Follow-up turns, reports and events are dropped; assignments of
    follow-up turns remain (they lose only turn_id). idle becomes completed."""
    op.drop_constraint(
        op.f("fk_investigation_assignments_turn_id_investigation_turns"),
        "investigation_assignments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_investigation_assignments_scope", "investigation_assignments", type_="foreignkey"
    )
    op.drop_index("ix_investigation_assignments_turn_id", table_name="investigation_assignments")
    op.drop_column("investigation_assignments", "turn_id")
    op.drop_index("ix_progress_events_investigation_id_id", table_name="progress_events")
    op.drop_table("progress_events")
    op.drop_constraint(
        "uq_investigation_assignments_investigation_id_id",
        "investigation_assignments",
        type_="unique",
    )
    op.drop_table("investigation_reports")
    op.drop_index(
        "uq_investigation_turns_one_active",
        table_name="investigation_turns",
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.drop_table("investigation_turns")
    op.drop_index(
        op.f("ix_investigation_environments_environment_id"),
        table_name="investigation_environments",
    )
    op.drop_table("investigation_environments")

    op.drop_constraint(op.f(STATUS_CHECK), "investigations", type_="check")
    op.execute("UPDATE investigations SET status = 'completed' WHERE status = 'idle'")
    op.create_check_constraint(op.f(STATUS_CHECK), "investigations", f"status IN {OLD_STATUSES}")
