"""Migration 815a7fcfbb5e on a scratch database: identity and organization tables are
added without touching any legacy record. Environments and investigations stay
ownerless (no owner or organization columns, nobody assigned), and the downgrade removes
only the new tables. LangGraph checkpoints live in their own schema, outside Alembic."""

import uuid

from sqlalchemy import text

from tests.test_migration_turns import alembic, scratch  # noqa: F401  (fixture)

BEFORE, AFTER = "25ecb11a412f", "815a7fcfbb5e"
IDS = {k: uuid.uuid4() for k in ("env", "inv", "turn", "asg", "tc", "ev", "rep")}

LEGACY = [
    "INSERT INTO environments (id, name, type, metadata_json) VALUES "
    """(:env, 'ubuntu-vm', 'local', '{"signoz": {"hosts": ["vm"]}}')""",
    "INSERT INTO investigations (id, title, question, status, trigger_type, current_round) "
    "VALUES (:inv, 'Latency', 'Why is it slow?', 'idle', 'manual', 1)",
    "INSERT INTO investigation_environments (investigation_id, environment_id) VALUES (:inv, :env)",
    "INSERT INTO investigation_turns (id, investigation_id, seq, kind, question, status, "
    "decision, answer_json, started_at, completed_at, presentation_json) VALUES "
    "(:turn, :inv, 1, 'initial', 'Why is it slow?', 'completed', 'investigated', "
    """'{"answer_md": "GET /slow [ev_x]"}', now(), now(), '{"version": 1}')""",
    "INSERT INTO investigation_assignments (id, investigation_id, environment_id, turn_id, "
    "round, objective, status) VALUES (:asg, :inv, :env, :turn, 1, 'o', 'completed')",
    "INSERT INTO tool_calls (id, assignment_id, tool_name, tool_category, status) "
    "VALUES (:tc, :asg, 'signoz.trace_stats', 'telemetry', 'succeeded')",
    "INSERT INTO evidence (id, assignment_id, investigation_id, environment_id, tool_call_id, "
    "evidence_type, source_provider, title, summary) "
    "VALUES (:ev, :asg, :inv, :env, :tc, 'trace', 'signoz', 'Trace stats', 'p95 1.5 s')",
    "INSERT INTO investigation_reports (id, investigation_id, turn_id, version, report_json) "
    """VALUES (:rep, :inv, :turn, 1, '{"outcome": "inconclusive"}')""",
    "INSERT INTO progress_events (investigation_id, turn_id, type, payload_json) "
    """VALUES (:inv, :turn, 'turn.completed', '{"decision": "investigated"}')""",
]
LEGACY_TABLES = (
    "environments", "investigations", "investigation_environments", "investigation_turns",
    "investigation_assignments", "tool_calls", "evidence", "investigation_reports",
    "progress_events",
)  # fmt: skip
NEW_TABLES = (
    "users", "auth_sessions", "auth_login_states", "organizations", "organization_memberships",
    "organization_invitations",
)  # fmt: skip


async def _fingerprints(engine) -> dict[str, tuple[int, str]]:
    """Row count and an order-independent checksum of every column of every row."""
    out = {}
    async with engine.connect() as conn:
        for table in LEGACY_TABLES:
            count, digest = (
                await conn.execute(
                    text(
                        f"SELECT count(*), coalesce(md5(string_agg(md5(t::text), '' ORDER BY "
                        f"md5(t::text))), '') FROM {table} t"
                    )
                )
            ).one()
            out[table] = (count, digest)
    return out


async def _columns(engine, table: str) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
            {"t": table},
        )
        return {r[0] for r in rows}


async def _tables(engine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
        return {r[0] for r in rows}


async def test_identity_migration_preserves_and_never_assigns_legacy_records(scratch):  # noqa: F811
    name, engine = scratch
    alembic(name, "upgrade", BEFORE)
    async with engine.begin() as conn:
        for sql in LEGACY:
            await conn.execute(text(sql), IDS)
    before = await _fingerprints(engine)
    assert all(count == 1 for count, _ in before.values())
    env_columns = await _columns(engine, "environments")
    investigation_columns = await _columns(engine, "investigations")

    alembic(name, "upgrade", AFTER)

    assert await _fingerprints(engine) == before
    assert set(NEW_TABLES) <= await _tables(engine)
    async with engine.connect() as conn:
        for table in NEW_TABLES:
            assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 0
    # No ownership was added to legacy records: same columns, nobody assigned.
    assert await _columns(engine, "environments") == env_columns
    assert await _columns(engine, "investigations") == investigation_columns

    alembic(name, "downgrade", BEFORE)

    assert await _fingerprints(engine) == before
    assert not set(NEW_TABLES) & await _tables(engine)
