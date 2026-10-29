"""Environment credentials (D026, D034): owner-only writes, status reads, key rotation.

Plaintext exists only in the request that sets a credential (validated, then sealed here)
and in deterministic code that re-seals or uses it. Nothing here returns, logs or audits
it: callers get CredentialStatus, audit rows name the environment and kind only.

Who may write: the environment's owner, while a member of its organization. Organization
owners and admins who do not own the environment may not (D029). Everyone else in the
organization may read statuses; outside it the environment does not exist (NoAccess).
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.credentials import crypto, resolver
from src.credentials.settings import CredentialConfig
from src.credentials.validation import CredentialValidationFailed
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    SigNozDedicatedConnection,
    SigNozTenantKey,
    TelemetryIsolation,
    User,
)
from src.schemas.credentials import CredentialStatus
from src.services import access

KINDS = (CredentialKind.NEBIUS, CredentialKind.SIGNOZ)
# Save-time probes with other tenants' keys: enough to catch an over-broad role, bounded.
MAX_FOREIGN_PROBES = 20


class NotEnvironmentOwner(Exception):
    pass


def _status(
    kind: CredentialKind, row: EnvironmentCredential | None, config: CredentialConfig | None
) -> CredentialStatus:
    if row is None:
        return CredentialStatus(
            kind=kind.value, configured=False, state="not_configured", updated_at=None,
            updated_by=None,
        )  # fmt: skip
    # Without the row's key this server cannot use it: reported, never worked around.
    state = "configured" if config is not None and row.key_id in config.keys else "unavailable"
    return CredentialStatus(
        kind=kind.value,
        configured=state == "configured",
        state=state,
        updated_at=row.updated_at,
        updated_by=str(row.updated_by_user_id),
        main_model=row.public_json.get("main_model"),
        local_model=row.public_json.get("local_model"),
        isolation=row.isolation.value if row.isolation else None,
    )


async def credential_statuses(
    session: AsyncSession, environment_id: uuid.UUID, config: CredentialConfig | None
) -> list[CredentialStatus]:
    rows = {
        row.kind: row
        for row in await session.scalars(
            select(EnvironmentCredential).where(
                EnvironmentCredential.environment_id == environment_id
            )
        )
    }
    return [_status(kind, rows.get(kind), config) for kind in KINDS]


async def owned_environment(
    session: AsyncSession, user: User, environment_id: uuid.UUID, *, lock: bool = False
) -> Environment:
    """NoAccess outside the caller's organizations; NotEnvironmentOwner for members who do
    not own it. With lock, the environment row stays locked until the transaction ends, so
    ownership cannot change between this check and the write."""
    environment = await access.environment_for(session, user, environment_id)
    if lock:
        environment = await session.scalar(
            select(Environment)
            .where(Environment.id == environment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert environment is not None
    if environment.owner_user_id != user.id:
        raise NotEnvironmentOwner(environment_id)
    return environment


async def tenant_key_ids(
    session: AsyncSession, environment: Environment, fingerprint: str
) -> tuple[str, ...]:
    """The ingestion keys a provider-scoped connection of this environment may query:
    only the operator's assignments to its own organization (nothing about others).
    CredentialValidationFailed("tenant_keys_missing") when there are none."""
    keys = await resolver.bound_key_ids(session, environment.organization_id, fingerprint)
    if not keys:
        raise CredentialValidationFailed("tenant_keys_missing")
    return keys


async def foreign_key_ids(
    session: AsyncSession, environment: Environment, fingerprint: str
) -> tuple[str, ...]:
    """Ingestion keys of the workspace assigned to other organizations, for the save-time
    check that a provider-scoped key cannot read them. Used server-side only."""
    return tuple(
        await session.scalars(
            select(SigNozTenantKey.key_id)
            .where(
                SigNozTenantKey.connection_fingerprint == fingerprint,
                SigNozTenantKey.organization_id != environment.organization_id,
            )
            .order_by(SigNozTenantKey.key_id)
            .limit(MAX_FOREIGN_PROBES)
        )
    )


async def require_verified(
    session: AsyncSession, environment: Environment, fingerprint: str
) -> None:
    """Outside local development a dedicated connection needs an operator's verification
    for this organization (D038): an owner's URL proves nothing, other DNS names can reach
    the same backend. CredentialValidationFailed("dedication_unverified") otherwise, the
    same answer whoever else uses the connection."""
    if resolver.local_development():
        return
    verified = await session.scalar(
        select(
            exists().where(
                SigNozDedicatedConnection.organization_id == environment.organization_id,
                SigNozDedicatedConnection.connection_fingerprint == fingerprint,
            )
        )
    )
    if not verified:
        raise CredentialValidationFailed("dedication_unverified")


async def _admit_connection(
    session: AsyncSession, environment: Environment, fingerprint: str, isolation: TelemetryIsolation
) -> None:
    """Under a per-connection lock, so two organizations saving the same connection at
    once cannot both pass. A dedicated connection must be verified outside local
    development, and must not be one another organization already reaches (its
    credentials, assigned keys, verification or development grants; detected by
    fingerprint: equivalent spellings match, other DNS names for the same backend do not)
    unless the operator verified it as this organization's. Existing connections are
    never changed here: an unsafe attempt is refused, the other organization's
    configuration stays as it was."""
    await session.execute(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("inquest:signoz:" + fingerprint, 0))
        )
    )
    if isolation is TelemetryIsolation.PROVIDER_SCOPED:
        await tenant_key_ids(session, environment, fingerprint)
        return
    await require_verified(session, environment, fingerprint)
    if await resolver.isolation_problem(
        session, environment.organization_id, fingerprint, TelemetryIsolation.DEDICATED
    ):
        raise CredentialValidationFailed("connection_not_isolated")


def _audit(action: str, environment: Environment, user: User, kind: CredentialKind) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor=f"user:{user.id}",
        actor_user_id=user.id,
        organization_id=environment.organization_id,
        details_json={"environment_id": str(environment.id), "kind": kind.value},
    )


async def store_credential(
    session: AsyncSession,
    environment_id: uuid.UUID,
    user: User,
    kind: CredentialKind,
    *,
    secret: dict[str, str],
    public: dict[str, str],
    config: CredentialConfig,
    isolation: TelemetryIsolation | None = None,
) -> CredentialStatus:
    """Sets or replaces (rotates) one credential. Call only after validation succeeded: a
    failed validation must leave the existing credential untouched. SigNoz needs an
    isolation mode, checked again here (CredentialValidationFailed) under the
    connection's lock."""
    environment = await owned_environment(session, user, environment_id, lock=True)
    fingerprint = None
    if kind is CredentialKind.SIGNOZ:
        fingerprint = crypto.fingerprint(config, secret["base_url"])
        await _admit_connection(
            session, environment, fingerprint, isolation or TelemetryIsolation.DEDICATED
        )
    sealed = crypto.seal(
        config, secret, environment_id=environment.id, kind=kind.value, owner_user_id=user.id
    )
    row = await session.get(EnvironmentCredential, (environment.id, kind))
    replaced = row is not None
    if row is None:
        row = EnvironmentCredential(environment_id=environment.id, kind=kind)
        session.add(row)
    row.owner_user_id = user.id
    row.key_id = sealed.key_id
    row.nonce = sealed.nonce
    row.ciphertext = sealed.ciphertext
    row.public_json = public
    row.connection_fingerprint = fingerprint
    row.isolation = (isolation or TelemetryIsolation.DEDICATED) if fingerprint else None
    row.updated_by_user_id = user.id
    session.add(
        _audit("credentials.replace" if replaced else "credentials.set", environment, user, kind)
    )
    await session.commit()
    await session.refresh(row)
    return _status(kind, row, config)


async def clear_credential(
    session: AsyncSession, environment_id: uuid.UUID, user: User, kind: CredentialKind
) -> Environment | None:
    """Deletes one credential. Returns the environment when a credential was removed, so
    the caller can stop the turns still running on it (runtime.cancel_environment_turns,
    after this commit); None when there was nothing to remove."""
    environment = await owned_environment(session, user, environment_id, lock=True)
    row = await session.get(EnvironmentCredential, (environment.id, kind))
    if row is not None:
        await session.delete(row)
        session.add(_audit("credentials.clear", environment, user, kind))
    await session.commit()
    return environment if row is not None else None


# --- operator: re-encryption with the active key -----------------------------------------


@dataclass
class RekeyReport:
    total: int = 0
    reencrypt: int = 0
    refingerprint: int = 0
    # (environment_id, kind) of rows this key ring cannot decrypt; never their contents.
    unreadable: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    applied: bool = False


async def rekey(
    session: AsyncSession, config: CredentialConfig, *, actor: str, apply: bool
) -> RekeyReport:
    """Re-seals rows not yet under the active key and recomputes connection fingerprints
    (needed only if the fingerprint key itself was replaced). All or nothing: refused while
    any row is unreadable, so a missing old key is noticed before it is dropped."""
    rows = list(
        await session.scalars(
            select(EnvironmentCredential)
            .order_by(EnvironmentCredential.environment_id, EnvironmentCredential.kind)
            .with_for_update()
        )
    )
    report = RekeyReport(total=len(rows))
    changes: list[tuple[EnvironmentCredential, dict[str, object]]] = []
    for row in rows:
        kind = row.kind.value
        try:
            payload = crypto.unseal(
                config,
                crypto.Sealed(row.key_id, row.nonce, row.ciphertext),
                environment_id=row.environment_id,
                kind=kind,
                owner_user_id=row.owner_user_id,
            )
        except crypto.CredentialUnreadable:
            report.unreadable.append((row.environment_id, kind))
            continue
        values: dict[str, object] = {}
        if row.key_id != config.active_key_id:
            sealed = crypto.seal(
                config,
                payload,
                environment_id=row.environment_id,
                kind=kind,
                owner_user_id=row.owner_user_id,
            )
            values |= {
                "key_id": sealed.key_id,
                "nonce": sealed.nonce,
                "ciphertext": sealed.ciphertext,
            }
            report.reencrypt += 1
        if row.kind is CredentialKind.SIGNOZ:
            fingerprint = crypto.fingerprint(config, payload["base_url"])
            if fingerprint != row.connection_fingerprint:
                values["connection_fingerprint"] = fingerprint
                report.refingerprint += 1
        if values:
            changes.append((row, values))
    if not apply or report.unreadable or not changes:
        await session.rollback()
        return report
    for row, values in changes:
        # updated_at stays: it tells owners when they last set the credential.
        await session.execute(
            update(EnvironmentCredential)
            .where(
                EnvironmentCredential.environment_id == row.environment_id,
                EnvironmentCredential.kind == row.kind,
            )
            .values(**values, updated_at=EnvironmentCredential.updated_at)
        )
    session.add(
        AuditEvent(
            action="credentials.rekey",
            actor=actor[:200],
            details_json={
                "active_key_id": config.active_key_id,
                "reencrypted": report.reencrypt,
                "refingerprinted": report.refingerprint,
            },
        )
    )
    await session.commit()
    report.applied = True
    return report
