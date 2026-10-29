"""Migration 94f0df921964 on a scratch database: legacy rows seeded at the previous
revision are backfilled into turns, scope and reports, evidence and reasoning records are
untouched, and the downgrade restores the old shape. Alembic runs in a subprocess so its
settings point at the scratch database."""

import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
BEFORE, AFTER = "9ce93924a639", "94f0df921964"


def alembic(database: str, *args: str) -> None:
    env = {**os.environ, "APP_ENV": "test", "POSTGRES_TEST_DB": database}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.fixture
async def scratch() -> AsyncIterator[tuple[str, AsyncEngine]]:
    name = f"inquest_migration_{uuid.uuid4().hex[:8]}"
    settings = get_settings()
    # Creating databases is administrative; the migration role owns and migrates it (D040).
    admin = create_async_engine(settings.admin_database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        owner = settings.migration_database_url.username
        await conn.execute(text(f'CREATE DATABASE "{name}" OWNER "{owner}"'))
    engine = create_async_engine(settings.migration_database_url.set(database=name))
    try:
        yield name, engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


IDS = {k: uuid.uuid4() for k in ("e1", "e2", "A", "B", "C", "D", "A1", "B1", "B2", "C1")}
IDS |= {k: uuid.uuid4() for k in ("tc", "ev", "hy", "fd")}

LEGACY = [
    "INSERT INTO environments (id, name, type) VALUES (:e1, 'ubuntu-vm', 'local'), "
    "(:e2, 'aws-prod', 'aws')",
    # A pending, B investigating, C completed with a stored result, D failed with no work.
    "INSERT INTO investigations (id, title, question, status, trigger_type, started_at, "
    "completed_at, final_result_json) VALUES "
    "(:A, 'a', 'Why is A slow?', 'pending', 'manual', NULL, NULL, NULL), "
    "(:B, 'b', 'Why is B failing?', 'investigating', 'manual', now(), NULL, NULL), "
    "(:C, 'c', 'What broke C?', 'completed', 'manual', now() - interval '1 hour', now(), "
    '\'{"summary": "done"}\'), '
    "(:D, 'd', 'D?', 'failed', 'manual', NULL, NULL, NULL)",
    "INSERT INTO investigation_assignments (id, investigation_id, environment_id, round, "
    "objective, status) VALUES "
    "(:A1, :A, :e1, 1, 'a', 'pending'), (:B1, :B, :e1, 1, 'b', 'running'), "
    "(:B2, :B, :e2, 2, 'b2', 'running'), (:C1, :C, :e1, 1, 'c', 'completed')",
    "INSERT INTO tool_calls (id, assignment_id, tool_name, tool_category, status) "
    "VALUES (:tc, :B1, 'signoz.trace_stats', 'telemetry', 'succeeded')",
    "INSERT INTO evidence (id, assignment_id, investigation_id, environment_id, tool_call_id, "
    "evidence_type, source_provider, title, summary) "
    "VALUES (:ev, :B1, :B, :e1, :tc, 'trace', 'signoz', 'Trace stats', '62 spans p95 1.5 s')",
    "INSERT INTO hypotheses (id, assignment_id, investigation_id, environment_id, statement, "
    "status) VALUES (:hy, :B1, :B, :e1, 'One endpoint is slow', 'supported')",
    "INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, investigation_id, "
    "environment_id, stance) VALUES (:hy, :ev, :B, :e1, 'supports')",
    "INSERT INTO findings (id, assignment_id, investigation_id, environment_id, hypothesis_id, "
    "title, summary, status) VALUES (:fd, :B1, :B, :e1, :hy, '/slow is slow', 's', 'confirmed')",
    "INSERT INTO finding_evidence (finding_id, evidence_id, investigation_id, environment_id, "
    "stance) VALUES (:fd, :ev, :B, :e1, 'supports')",
]
REASONING_TABLES = ("tool_calls", "evidence", "hypotheses", "hypothesis_evidence", "findings",
                    "finding_evidence")  # fmt: skip


async def _rows(engine: AsyncEngine, sql: str, **params: object) -> list:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def _snapshot(engine: AsyncEngine) -> dict[str, list]:
    return {t: await _rows(engine, f"SELECT * FROM {t} ORDER BY 1") for t in REASONING_TABLES}


async def test_upgrade_backfills_and_downgrade_restores(scratch):
    name, engine = scratch
    alembic(name, "upgrade", BEFORE)
    async with engine.begin() as conn:
        for sql in LEGACY:
            await conn.execute(text(sql), IDS)
    before = await _snapshot(engine)

    alembic(name, "upgrade", AFTER)

    # Scope: one row per environment an investigation had work in; D had none.
    scope = await _rows(
        engine, "SELECT investigation_id, environment_id FROM investigation_environments"
    )
    assert set(scope) == {
        (IDS["A"], IDS["e1"]), (IDS["B"], IDS["e1"]), (IDS["B"], IDS["e2"]),
        (IDS["C"], IDS["e1"]),
    }  # fmt: skip
    # Turn 1 per investigation, carrying its question and mapped lifecycle.
    turns = {
        r.investigation_id: r for r in await _rows(engine, "SELECT * FROM investigation_turns")
    }
    assert set(turns) == {IDS[k] for k in "ABCD"}
    assert all((t.seq, t.kind) == (1, "initial") for t in turns.values())
    assert {k: turns[IDS[k]].status for k in "ABCD"} == {
        "A": "pending", "B": "running", "C": "completed", "D": "failed",
    }  # fmt: skip
    assert turns[IDS["B"]].question == "Why is B failing?"
    assert turns[IDS["B"]].started_at is not None
    # Every assignment points at turn 1 of its own investigation.
    links = await _rows(
        engine, "SELECT id, investigation_id, turn_id FROM investigation_assignments"
    )
    assert len(links) == 4
    assert all(turn == turns[investigation].id for _, investigation, turn in links)
    # A stored result became report version 1 of turn 1.
    (report,) = await _rows(engine, "SELECT * FROM investigation_reports")
    assert (report.investigation_id, report.turn_id, report.version) == (
        IDS["C"], turns[IDS["C"]].id, 1,
    )  # fmt: skip
    assert report.report_json == {"summary": "done"}
    assert await _rows(engine, "SELECT * FROM progress_events") == []
    # Evidence, tool calls and reasoning records are untouched.
    assert await _snapshot(engine) == before

    # idle is a valid status now; the downgrade maps it back to completed.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE investigations SET status = 'idle' WHERE id = :B"), IDS)
    alembic(name, "downgrade", BEFORE)

    (status,) = await _rows(engine, "SELECT status FROM investigations WHERE id = :B", **IDS)
    assert status == ("completed",)
    tables = {
        r[0]
        for r in await _rows(engine, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert not tables & {"investigation_turns", "investigation_environments",
                         "investigation_reports", "progress_events"}  # fmt: skip
    columns = await _rows(
        engine,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'investigation_assignments' AND column_name = 'turn_id'",
    )
    assert columns == []
    assert await _snapshot(engine) == before
    # The stored result itself was never touched.
    (result,) = await _rows(
        engine, "SELECT final_result_json FROM investigations WHERE id = :C", **IDS
    )
    assert result[0] == json.loads('{"summary": "done"}')


async def test_upgrade_on_an_empty_database(scratch):
    name, engine = scratch
    alembic(name, "upgrade", AFTER)
    (version,) = await _rows(engine, "SELECT version_num FROM alembic_version")
    assert version == (AFTER,)
