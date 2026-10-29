"""prepare checkpoint schema

LangGraph's checkpoint tables (schema `langgraph`) used to be created by the application
at startup (CREATE SCHEMA and AsyncPostgresSaver.setup()), which required DDL rights for
the runtime role. They are prepared here instead, as the migration role (Phase 7D-2C-2,
D040). Idempotent: existing checkpoint tables and threads are kept; only LangGraph
migrations not yet recorded in checkpoint_migrations run. A newer LangGraph version with
new checkpoint migrations needs a new revision calling prepare_checkpoint_schema again.

The downgrade keeps the schema and its checkpoints: they existed before this revision
managed them, and dropping them would lose resumable runs.

Revision ID: 8a49eb4cbecd
Revises: 0b9ac8fb6d48
Create Date: 2026-10-28 21:32:31.517735

"""

from collections.abc import Sequence

from alembic import op

from src.investigators.checkpoint import prepare_checkpoint_schema

# revision identifiers, used by Alembic.
revision: str = "8a49eb4cbecd"
down_revision: str | Sequence[str] | None = "0b9ac8fb6d48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # LangGraph creates some indexes concurrently, which cannot run in a transaction.
    with op.get_context().autocommit_block():
        prepare_checkpoint_schema(op.get_bind())


def downgrade() -> None:
    """Downgrade schema."""
