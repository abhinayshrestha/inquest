"""Tenant access (D033): which environments and investigations a signed-in user may reach.

A user reaches a record only through a current membership in the record's organization,
read from the database on every call; organization ids sent by the browser only select
which membership to check. Ownerless legacy records (organization_id NULL, D032) are
reachable by nobody. Everything outside the caller's reach is NoAccess, which routes turn
into 404 so a guessed id reveals nothing.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AuthSession,
    Environment,
    Investigation,
    InvestigationEnvironment,
    OrganizationMembership,
    User,
    can_use_process_credentials,
)


class NoAccess(Exception):
    pass


def _member_of(user_id: uuid.UUID):
    return select(OrganizationMembership.organization_id).where(
        OrganizationMembership.user_id == user_id
    )


async def require_member(session: AsyncSession, organization_id: uuid.UUID, user: User) -> None:
    found = await session.scalar(
        select(OrganizationMembership.organization_id).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user.id,
        )
    )
    if found is None:
        raise NoAccess(organization_id)


async def environment_for(
    session: AsyncSession, user: User, environment_id: uuid.UUID
) -> Environment:
    environment = await session.scalar(
        select(Environment).where(
            Environment.id == environment_id,
            Environment.organization_id.in_(_member_of(user.id)),
        )
    )
    if environment is None:
        raise NoAccess(environment_id)
    return environment


async def investigation_for(
    session: AsyncSession, user: User, investigation_id: uuid.UUID
) -> Investigation:
    investigation = await session.scalar(
        select(Investigation).where(
            Investigation.id == investigation_id,
            Investigation.organization_id.in_(_member_of(user.id)),
        )
    )
    if investigation is None:
        raise NoAccess(investigation_id)
    return investigation


async def still_allowed(
    session: AsyncSession, auth_session_id: uuid.UUID, investigation_id: uuid.UUID
) -> bool:
    """For long-lived responses (SSE): the browser session is still valid and its user is
    still a member of the investigation's organization."""
    now = datetime.now(UTC)
    found = await session.scalar(
        select(Investigation.id)
        .join(
            OrganizationMembership,
            OrganizationMembership.organization_id == Investigation.organization_id,
        )
        .join(AuthSession, AuthSession.user_id == OrganizationMembership.user_id)
        .join(User, User.id == AuthSession.user_id)
        .where(
            Investigation.id == investigation_id,
            AuthSession.id == auth_session_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
            User.disabled_at.is_(None),
        )
    )
    return found is not None


async def investigation_runnable(session: AsyncSession, investigation_id: uuid.UUID) -> bool:
    """Every environment in scope may run with the credentials available to this backend.
    Until per-environment credentials exist (7C) that means process-wide development
    credentials, which tenant environments only get through an operator grant."""
    environments = await session.scalars(
        select(Environment)
        .join(InvestigationEnvironment, InvestigationEnvironment.environment_id == Environment.id)
        .where(InvestigationEnvironment.investigation_id == investigation_id)
    )
    found = list(environments)
    return bool(found) and all(can_use_process_credentials(e) for e in found)
