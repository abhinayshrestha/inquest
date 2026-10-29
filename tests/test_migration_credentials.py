"""Migration 461f6726a70e on a scratch database: environment credentials are added without
changing any existing row; credentials need an owned environment and pin its owner; the
downgrade refuses to discard stored credentials."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.test_migration_turns import alembic, scratch  # noqa: F401  (fixture)

BEFORE, AFTER = "f2b7c1d9a4e3", "461f6726a70e"
IDS = {k: uuid.uuid4() for k in ("user", "other", "org", "env", "legacy")}

SETUP = [
    "INSERT INTO users (id, issuer, subject) VALUES (:user, 'iss', 'a'), (:other, 'iss', 'b')",
    "INSERT INTO organizations (id, name, slug, created_by_user_id) "
    "VALUES (:org, 'Org', 'org', :user)",
    "INSERT INTO environments (id, name, type, organization_id, owner_user_id) "
    "VALUES (:env, 'owned', 'local', :org, :user)",
    "INSERT INTO environments (id, name, type) VALUES (:legacy, 'legacy', 'local')",
]
CREDENTIAL = (
    "INSERT INTO environment_credentials (environment_id, kind, owner_user_id, key_id, nonce, "
    "ciphertext, updated_by_user_id) VALUES (:e, 'nebius', :o, 'k1', :n, :c, :o)"
)


async def _snapshot(engine) -> list[tuple]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT md5(t::text) FROM environments t ORDER BY id"))
        return [tuple(r) for r in rows]


async def test_credentials_migration_is_additive_and_guarded(scratch):  # noqa: F811
    name, engine = scratch
    alembic(name, "upgrade", BEFORE)
    async with engine.begin() as conn:
        for sql in SETUP:
            await conn.execute(text(sql), IDS)
    before = await _snapshot(engine)

    alembic(name, "upgrade", AFTER)
    assert await _snapshot(engine) == before

    credential = {"e": IDS["env"], "o": IDS["user"], "n": b"0" * 12, "c": b"sealed"}
    # Ownerless environments cannot hold credentials; neither can a non-owner's row.
    for bad in ({"e": IDS["legacy"]}, {"o": IDS["other"]}, {"n": b"short"}):
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(text(CREDENTIAL), credential | bad)
    async with engine.begin() as conn:
        await conn.execute(text(CREDENTIAL), credential)
    # The owner cannot change while the previous owner's credentials remain.
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE environments SET owner_user_id = :other WHERE id = :env"), IDS
            )

    with pytest.raises(AssertionError, match="refusing to drop"):
        alembic(name, "downgrade", BEFORE)

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM environment_credentials"))
        await conn.execute(
            text("UPDATE environments SET owner_user_id = :other WHERE id = :env"), IDS
        )
    alembic(name, "downgrade", BEFORE)
    async with engine.connect() as conn:
        tables = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'environment_credentials'"
            )
        )
        assert tables.first() is None
        columns = await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'audit_events' AND column_name = 'actor_user_id'"
            )
        )
        assert columns.first() is None
