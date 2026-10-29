"""Operator re-encryption (scripts.rotate_credential_key, D034): rows move to the active
key without changing what owners see, fingerprints are recomputed only when their key
changes, and nothing happens while any row is unreadable."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.credentials import crypto
from src.credentials.settings import load_credential_config
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    EnvironmentType,
    User,
)
from src.services.credentials import rekey, store_credential
from tests.credential_fixtures import (
    KEY_1,
    KEY_2,
    NEBIUS_SECRET,
    SIGNOZ_ORIGIN,
    SIGNOZ_SECRET,
    b64,
    configure_credentials,
)
from tests.tenancy import Member


@pytest.fixture(autouse=True)
def _clear_cache():
    yield
    load_credential_config.cache_clear()


def _config(monkeypatch, **kw):
    configure_credentials(monkeypatch, **kw)
    return load_credential_config()


async def _stored(session: AsyncSession, tenant: Member, config, signoz_config=None) -> uuid.UUID:
    """The environment's id: rekey's dry run rolls back, which expires ORM objects."""
    environment = Environment(
        name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL, **tenant.owns
    )
    session.add(environment)
    await session.flush()
    user = await session.get(User, tenant.user_id)
    await store_credential(
        session, environment.id, user, CredentialKind.NEBIUS, config=config,
        secret={"api_key": NEBIUS_SECRET, "base_url": "https://api.tokenfactory.nebius.com/v1/"},
        public={"main_model": "m", "local_model": "l"},
    )  # fmt: skip
    await store_credential(
        session, environment.id, user, CredentialKind.SIGNOZ, config=signoz_config or config,
        secret={"api_key": SIGNOZ_SECRET, "base_url": SIGNOZ_ORIGIN}, public={},
    )  # fmt: skip
    return environment.id


async def _rows(session: AsyncSession, environment_id: uuid.UUID) -> list[EnvironmentCredential]:
    return list(
        await session.scalars(
            select(EnvironmentCredential)
            .where(EnvironmentCredential.environment_id == environment_id)
            .order_by(EnvironmentCredential.kind)
            .execution_options(populate_existing=True)
        )
    )


async def _rekey_audits(session: AsyncSession) -> list[AuditEvent]:
    return list(
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "credentials.rekey"))
    )


async def test_rotation_reencrypts_under_the_active_key(session, tenant, monkeypatch):
    old = _config(monkeypatch)
    environment_id = await _stored(session, tenant, old)
    # Tests share one transaction, where now() never moves: backdate to see any change.
    await session.execute(
        update(EnvironmentCredential)
        .where(EnvironmentCredential.environment_id == environment_id)
        .values(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await session.commit()
    before = {
        (r.kind, r.ciphertext, r.updated_at, r.connection_fingerprint)
        for r in await _rows(session, environment_id)
    }
    audits_before = len(await _rekey_audits(session))

    new = _config(monkeypatch, keys={"k1": KEY_1, "k2": KEY_2}, active="k2")
    dry = await rekey(session, new, actor="op", apply=False)
    assert (dry.reencrypt, dry.refingerprint, dry.unreadable, dry.applied) == (2, 0, [], False)
    assert {r.key_id for r in await _rows(session, environment_id)} == {"k1"}

    report = await rekey(session, new, actor="Op (os:test)", apply=True)

    assert report.applied and report.reencrypt == 2
    rows = await _rows(session, environment_id)
    assert {r.key_id for r in rows} == {"k2"}
    # Owners still see when they set it; fingerprints are independent of the key.
    assert {(r.kind, r.updated_at, r.connection_fingerprint) for r in rows} == {
        (k, u, f) for k, _, u, f in before
    }
    assert not {r.ciphertext for r in rows} & {c for _, c, _, _ in before}
    nebius = rows[0]
    assert crypto.unseal(
        _config(monkeypatch, keys={"k2": KEY_2}, active="k2"),
        crypto.Sealed(nebius.key_id, nebius.nonce, nebius.ciphertext),
        environment_id=environment_id, kind="nebius", owner_user_id=tenant.user_id,
    )["api_key"] == NEBIUS_SECRET  # fmt: skip
    audits = await _rekey_audits(session)
    assert len(audits) == audits_before + 1
    assert audits[-1].actor == "Op (os:test)" and audits[-1].details_json["reencrypted"] >= 2
    assert NEBIUS_SECRET not in str(audits[-1].details_json)

    again = await rekey(session, new, actor="op", apply=True)
    assert (again.reencrypt, again.applied) == (0, False)


async def test_new_fingerprint_key_recomputes_fingerprints(session, tenant, monkeypatch):
    old = _config(monkeypatch)
    environment_id = await _stored(session, tenant, old)
    new = _config(monkeypatch, fingerprint=b64(b"f2"))
    report = await rekey(session, new, actor="op", apply=True)
    assert report.applied and report.refingerprint >= 1
    signoz = (await _rows(session, environment_id))[1]
    assert signoz.connection_fingerprint == crypto.fingerprint(new, SIGNOZ_ORIGIN)
    assert signoz.connection_fingerprint != crypto.fingerprint(old, SIGNOZ_ORIGIN)


async def test_unreadable_rows_refuse_the_whole_rotation(session, tenant, monkeypatch):
    k1 = _config(monkeypatch)
    k2 = _config(monkeypatch, keys={"k1": KEY_1, "k2": KEY_2}, active="k2")
    environment_id = await _stored(session, tenant, k1, signoz_config=k2)
    before = [(r.key_id, r.ciphertext) for r in await _rows(session, environment_id)]

    # k1 was dropped too early; the SigNoz row (k2) alone could still be re-encrypted.
    new = _config(monkeypatch, keys={"k2": KEY_2, "k3": b64(b"k3")}, active="k3")
    report = await rekey(session, new, actor="op", apply=True)

    assert not report.applied and report.reencrypt == 1
    assert (environment_id, "nebius") in report.unreadable
    assert (environment_id, "signoz") not in report.unreadable
    assert [(r.key_id, r.ciphertext) for r in await _rows(session, environment_id)] == before
