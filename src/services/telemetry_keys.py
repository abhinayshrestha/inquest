"""Operator assignments of shared SigNoz workspace ingestion keys to organizations (D037).

A provider-scoped connection queries only with the key ids assigned here: the
organization's owner never chooses them, so nobody can name another tenant's key. A key
id belongs to at most one organization per workspace. Used by scripts.bind_signoz_tenant;
there is no HTTP equivalent. Changes take effect at the next query (the resolver reads
assignments per call); removing the last one stops the organization's provider-scoped
runs with telemetry_isolation_unverified.

Also the operator's verification that a connection is dedicated to one organization
(D038), required for dedicated connections outside local development, and the review of
every tenant connection under the isolation policy (resolver.isolation_review).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.credentials import crypto
from src.credentials.settings import CredentialConfig
from src.integrations.signoz.builders import TENANT_KEY_ID
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    Organization,
    SigNozDedicatedConnection,
    SigNozTenantKey,
    TelemetryIsolation,
)


class BindingRefused(Exception):
    pass


@dataclass
class BindingPlan:
    organization_id: uuid.UUID
    fingerprint: str
    add: list[str]
    remove: list[str]
    applied: bool = False
    # Bind: other organizations' dedicated connections to the workspace, refused while
    # keys are assigned on it (a shared workspace). Counted, never changed.
    others_refused: int = 0


def _audit(action: str, plan: BindingPlan, actor: str) -> AuditEvent:
    # Ids and the keyed fingerprint only: never the workspace URL.
    return AuditEvent(
        action=action,
        actor=actor[:200],
        organization_id=plan.organization_id,
        details_json={
            "connection_fingerprint": plan.fingerprint,
            "key_ids": sorted(plan.add or plan.remove),
        },
    )


async def bind(
    session: AsyncSession,
    config: CredentialConfig,
    organization_id: uuid.UUID,
    workspace_url: str,
    key_ids: list[str],
    *,
    actor: str,
    apply: bool,
) -> BindingPlan:
    if await session.get(Organization, organization_id) is None:
        raise BindingRefused("organization does not exist")
    if not key_ids or not all(TENANT_KEY_ID.match(k) for k in key_ids):
        raise BindingRefused("ingestion key ids must match [A-Za-z0-9_-]{1,64}")
    fingerprint = crypto.fingerprint(config, workspace_url)
    await _lock(session, fingerprint)
    held = {
        row.key_id: row.organization_id
        for row in await session.scalars(
            select(SigNozTenantKey)
            .where(
                SigNozTenantKey.connection_fingerprint == fingerprint,
                SigNozTenantKey.key_id.in_(key_ids),
            )
            .with_for_update()
        )
    }
    if any(owner != organization_id for owner in held.values()):
        raise BindingRefused("an ingestion key id is already assigned to another organization")
    if await _verified_for(session, fingerprint) not in (None, organization_id):
        raise BindingRefused("the workspace is verified as dedicated to another organization")
    plan = BindingPlan(organization_id, fingerprint, sorted(set(key_ids) - set(held)), [])
    plan.others_refused = await _other_connections(
        session, organization_id, fingerprint, TelemetryIsolation.DEDICATED
    )
    if not apply or not plan.add:
        await session.rollback()
        return plan
    session.add_all(
        SigNozTenantKey(
            organization_id=organization_id,
            connection_fingerprint=fingerprint,
            key_id=key_id,
            created_by=actor[:200],
        )
        for key_id in plan.add
    )
    session.add(_audit("telemetry.bind", plan, actor))
    await session.commit()
    plan.applied = True
    return plan


async def unbind(
    session: AsyncSession,
    config: CredentialConfig,
    organization_id: uuid.UUID,
    workspace_url: str,
    key_ids: list[str],
    *,
    actor: str,
    apply: bool,
) -> BindingPlan:
    fingerprint = crypto.fingerprint(config, workspace_url)
    found = list(
        await session.scalars(
            select(SigNozTenantKey.key_id).where(
                SigNozTenantKey.organization_id == organization_id,
                SigNozTenantKey.connection_fingerprint == fingerprint,
                SigNozTenantKey.key_id.in_(key_ids),
            )
        )
    )
    plan = BindingPlan(organization_id, fingerprint, [], sorted(found))
    if not apply or not plan.remove:
        await session.rollback()
        return plan
    await session.execute(
        delete(SigNozTenantKey).where(
            SigNozTenantKey.organization_id == organization_id,
            SigNozTenantKey.connection_fingerprint == fingerprint,
            SigNozTenantKey.key_id.in_(plan.remove),
        )
    )
    session.add(_audit("telemetry.unbind", plan, actor))
    await session.commit()
    plan.applied = True
    return plan


# --- dedicated connections (D038) ----------------------------------------------------------


@dataclass
class DedicationPlan:
    organization_id: uuid.UUID
    fingerprint: str
    change: bool
    # Other organizations' stored connections the change refuses (verify) or leaves
    # refused (unverify): counted, for the operator to follow up.
    others_refused: int
    applied: bool = False


async def _lock(session: AsyncSession, fingerprint: str) -> None:
    # The lock credential saves take (services.credentials._admit_connection).
    await session.execute(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("inquest:signoz:" + fingerprint, 0))
        )
    )


async def _verified_for(session: AsyncSession, fingerprint: str) -> uuid.UUID | None:
    return await session.scalar(
        select(SigNozDedicatedConnection.organization_id).where(
            SigNozDedicatedConnection.connection_fingerprint == fingerprint
        )
    )


async def _other_connections(
    session: AsyncSession,
    organization_id: uuid.UUID,
    fingerprint: str,
    isolation: TelemetryIsolation | None = None,
) -> int:
    only = () if isolation is None else (EnvironmentCredential.isolation == isolation,)
    return (
        await session.scalar(
            select(func.count())
            .select_from(EnvironmentCredential)
            .join(Environment, Environment.id == EnvironmentCredential.environment_id)
            .where(
                EnvironmentCredential.kind == CredentialKind.SIGNOZ,
                EnvironmentCredential.connection_fingerprint == fingerprint,
                Environment.organization_id != organization_id,
                *only,
            )
        )
        or 0
    )


async def verify_dedicated(
    session: AsyncSession,
    config: CredentialConfig,
    organization_id: uuid.UUID,
    workspace_url: str,
    *,
    actor: str,
    apply: bool,
) -> DedicationPlan:
    """Records that the connection at workspace_url belongs to this organization alone.
    The operator establishes that out of band (who runs the instance or workspace, that
    no other organization's keys work there); Inquest cannot. Refused while the
    connection is verified for another organization or has ingestion keys assigned to
    one (a shared workspace). Other organizations' stored connections to it become
    refused (legacy reuse): counted in the plan, never changed."""
    if await session.get(Organization, organization_id) is None:
        raise BindingRefused("organization does not exist")
    fingerprint = crypto.fingerprint(config, workspace_url)
    await _lock(session, fingerprint)
    verified = await _verified_for(session, fingerprint)
    if verified not in (None, organization_id):
        raise BindingRefused("the connection is verified as dedicated to another organization")
    shared = await session.scalar(
        select(func.count()).where(
            SigNozTenantKey.connection_fingerprint == fingerprint,
            SigNozTenantKey.organization_id != organization_id,
        )
    )
    if shared:
        raise BindingRefused(
            "the connection has ingestion keys assigned to other organizations (a shared "
            "workspace): use provider-scoped isolation"
        )
    plan = DedicationPlan(
        organization_id,
        fingerprint,
        change=verified is None,
        others_refused=await _other_connections(session, organization_id, fingerprint),
    )
    if not apply or not plan.change:
        await session.rollback()
        return plan
    session.add(
        SigNozDedicatedConnection(
            organization_id=organization_id,
            connection_fingerprint=fingerprint,
            verified_by=actor[:200],
        )
    )
    session.add(_dedication_audit("telemetry.verify_dedicated", plan, actor))
    await session.commit()
    plan.applied = True
    return plan


async def unverify_dedicated(
    session: AsyncSession,
    config: CredentialConfig,
    organization_id: uuid.UUID,
    workspace_url: str,
    *,
    actor: str,
    apply: bool,
) -> DedicationPlan:
    """Removes the verification: outside local development the organization's dedicated
    connections to it stop at their next resolution."""
    fingerprint = crypto.fingerprint(config, workspace_url)
    await _lock(session, fingerprint)
    plan = DedicationPlan(
        organization_id,
        fingerprint,
        change=await _verified_for(session, fingerprint) == organization_id,
        others_refused=await _other_connections(session, organization_id, fingerprint),
    )
    if not apply or not plan.change:
        await session.rollback()
        return plan
    await session.execute(
        delete(SigNozDedicatedConnection).where(
            SigNozDedicatedConnection.organization_id == organization_id,
            SigNozDedicatedConnection.connection_fingerprint == fingerprint,
        )
    )
    session.add(_dedication_audit("telemetry.unverify_dedicated", plan, actor))
    await session.commit()
    plan.applied = True
    return plan


def _dedication_audit(action: str, plan: DedicationPlan, actor: str) -> AuditEvent:
    # The keyed fingerprint only: never the workspace URL.
    return AuditEvent(
        action=action,
        actor=actor[:200],
        organization_id=plan.organization_id,
        details_json={
            "connection_fingerprint": plan.fingerprint,
            "other_connections_refused": plan.others_refused,
        },
    )
