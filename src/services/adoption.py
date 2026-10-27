"""Operator-only adoption of ownerless legacy records into an organization (D032).

Never reachable over HTTP and never automatic: an operator on the backend host names the
environments, the organization and the owner (scripts.adopt_legacy). Investigations move
with their environments; one whose scope reaches an environment that is not being
adopted blocks the whole adoption, so nothing ends up split across tenants. Records are
moved, not copied or rewritten: evidence, turns, reports and checkpoints keep their ids
and contents. Every adoption writes an audit event in the same transaction.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AuditEvent,
    Environment,
    Investigation,
    InvestigationEnvironment,
    OrganizationMembership,
)

ACTION = "legacy.adopt"


class AdoptionRefused(Exception):
    pass


@dataclass
class AdoptionPlan:
    organization_id: uuid.UUID
    owner_user_id: uuid.UUID
    environments: list[Environment]
    investigation_ids: list[uuid.UUID]
    process_credentials: bool
    problems: list[str] = field(default_factory=list)


async def plan_adoption(
    session: AsyncSession,
    *,
    environment_ids: list[uuid.UUID],
    organization_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    process_credentials: bool,
) -> AdoptionPlan:
    """Read-only: what adoption would move, and every reason it cannot."""
    environments = list(
        await session.scalars(select(Environment).where(Environment.id.in_(environment_ids)))
    )
    plan = AdoptionPlan(organization_id, owner_user_id, environments, [], process_credentials)
    missing = set(environment_ids) - {e.id for e in environments}
    plan.problems += [f"environment {i} does not exist" for i in sorted(missing, key=str)]
    plan.problems += [
        f"environment {e.id} already belongs to an organization"
        for e in environments
        if e.organization_id is not None
    ]
    member = await session.scalar(
        select(OrganizationMembership.user_id).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == owner_user_id,
        )
    )
    if member is None:
        plan.problems.append("the owner is not a member of the organization")
    taken = set(
        await session.scalars(
            select(Environment.name).where(
                Environment.organization_id == organization_id,
                Environment.name.in_([e.name for e in environments]),
            )
        )
    )
    plan.problems += [f"the organization already has an environment named {n!r}" for n in taken]

    adopted = {e.id for e in environments}
    scopes: dict[uuid.UUID, set[uuid.UUID]] = {}
    rows = await session.execute(
        select(
            InvestigationEnvironment.investigation_id, InvestigationEnvironment.environment_id
        ).where(
            InvestigationEnvironment.investigation_id.in_(
                select(InvestigationEnvironment.investigation_id).where(
                    InvestigationEnvironment.environment_id.in_(adopted)
                )
            )
        )
    )
    for investigation_id, environment_id in rows.all():
        scopes.setdefault(investigation_id, set()).add(environment_id)
    owned = set(
        await session.scalars(
            select(Investigation.id).where(
                Investigation.id.in_(scopes), Investigation.organization_id.is_not(None)
            )
        )
    )
    for investigation_id, scope in sorted(scopes.items(), key=lambda kv: str(kv[0])):
        if investigation_id in owned:
            plan.problems.append(f"investigation {investigation_id} already has an organization")
        elif not scope <= adopted:
            plan.problems.append(
                f"investigation {investigation_id} also covers environments not being adopted"
            )
        else:
            plan.investigation_ids.append(investigation_id)
    return plan


async def adopt(session: AsyncSession, plan: AdoptionPlan, *, actor: str) -> AuditEvent:
    """Applies a plan without problems in one transaction, with its audit event. The
    database re-checks tenant consistency at commit (deferred trigger)."""
    if plan.problems:
        raise AdoptionRefused("; ".join(plan.problems))
    for environment in plan.environments:
        environment.organization_id = plan.organization_id
        environment.owner_user_id = plan.owner_user_id
        environment.process_credentials = plan.process_credentials
    for investigation in await session.scalars(
        select(Investigation).where(Investigation.id.in_(plan.investigation_ids))
    ):
        investigation.organization_id = plan.organization_id
    event = AuditEvent(
        action=ACTION,
        actor=actor[:200],
        organization_id=plan.organization_id,
        details_json={
            "environment_ids": [str(e.id) for e in plan.environments],
            "investigation_ids": [str(i) for i in plan.investigation_ids],
            "owner_user_id": str(plan.owner_user_id),
            "process_credentials": plan.process_credentials,
        },
    )
    session.add(event)
    await session.commit()
    return event
