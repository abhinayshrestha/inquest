"""Organizations, memberships and invitations (D025, D031).

Every function takes the acting user from the verified session and checks that user's
membership and role here, on the server. A user who is not a member of an organization
gets NotFound for it, so outsiders cannot even learn that it exists.

Roles: owner — everything, including roles; admin — invite members, revoke invitations,
remove members; member — see the organization and its members. Organization roles grant
no access to any environment's credentials (D029).
"""

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.auth.sessions import digest
from src.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
    User,
)

INVITATION_DAYS = 7
R = OrganizationRole
MANAGERS = frozenset({R.OWNER, R.ADMIN})


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Conflict(Exception):
    pass


class InvitationUnusable(Exception):
    """Expired, revoked or already used. One message for all three: a token holder learns
    nothing more than that the link no longer works."""


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50].strip("-")
    return slug or "organization"


async def membership_role(
    session: AsyncSession, organization_id: uuid.UUID, user: User
) -> OrganizationRole:
    role = await session.scalar(
        select(OrganizationMembership.role).where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user.id,
        )
    )
    if role is None:
        raise NotFound(organization_id)
    return role


async def _require(
    session: AsyncSession, organization_id: uuid.UUID, user: User, allowed: frozenset
) -> OrganizationRole:
    role = await membership_role(session, organization_id, user)
    if role not in allowed:
        raise Forbidden(role)
    return role


async def create_organization(session: AsyncSession, user: User, name: str) -> Organization:
    name = " ".join(name.split())
    if not name:
        raise Conflict("name is empty")
    base = _slug(name)
    taken = set(
        await session.scalars(select(Organization.slug).where(Organization.slug.like(f"{base}%")))
    )
    slug = (
        base
        if base not in taken
        else next(f"{base}-{n}" for n in range(2, 10_000) if f"{base}-{n}" not in taken)
    )
    organization = Organization(name=name, slug=slug, created_by_user_id=user.id)
    session.add(organization)
    await session.flush()
    session.add(
        OrganizationMembership(organization_id=organization.id, user_id=user.id, role=R.OWNER)
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("slug taken, try again") from exc
    return organization


async def my_memberships(
    session: AsyncSession, user: User
) -> list[tuple[Organization, OrganizationRole]]:
    rows = await session.execute(
        select(Organization, OrganizationMembership.role)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(OrganizationMembership.user_id == user.id)
        .order_by(Organization.name)
    )
    return [(o, r) for o, r in rows.all()]


async def get_organization(
    session: AsyncSession, organization_id: uuid.UUID, user: User
) -> tuple[Organization, OrganizationRole, int]:
    role = await membership_role(session, organization_id, user)
    organization = await session.get(Organization, organization_id)
    count = await session.scalar(
        select(func.count()).where(OrganizationMembership.organization_id == organization_id)
    )
    return organization, role, count or 0


async def list_members(
    session: AsyncSession, organization_id: uuid.UUID, user: User
) -> list[tuple[OrganizationMembership, User]]:
    await membership_role(session, organization_id, user)
    rows = await session.execute(
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.organization_id == organization_id)
        .order_by(OrganizationMembership.created_at)
    )
    return [(m, u) for m, u in rows.all()]


async def _locked_membership(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMembership:
    membership = await session.scalar(
        select(OrganizationMembership)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.user_id == user_id,
        )
        .with_for_update()
    )
    if membership is None:
        raise NotFound(user_id)
    return membership


async def _owner_count(session: AsyncSession, organization_id: uuid.UUID) -> int:
    # Locks the owner rows so two concurrent demotions cannot both leave zero owners.
    owners = await session.scalars(
        select(OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == organization_id,
            OrganizationMembership.role == R.OWNER,
        )
        .with_for_update()
    )
    return len(list(owners))


async def change_role(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user: User,
    target_user_id: uuid.UUID,
    role: OrganizationRole,
) -> OrganizationMembership:
    await _require(session, organization_id, user, frozenset({R.OWNER}))
    membership = await _locked_membership(session, organization_id, target_user_id)
    if membership.role == R.OWNER and role != R.OWNER:
        if await _owner_count(session, organization_id) <= 1:
            raise Conflict("an organization needs at least one owner")
    membership.role = role
    await session.commit()
    return membership


async def remove_member(
    session: AsyncSession, organization_id: uuid.UUID, user: User, target_user_id: uuid.UUID
) -> None:
    acting = await membership_role(session, organization_id, user)
    membership = await _locked_membership(session, organization_id, target_user_id)
    leaving = target_user_id == user.id
    # Members may leave; admins remove members; owners remove anyone.
    if not leaving and not (
        acting == R.OWNER or (acting == R.ADMIN and membership.role == R.MEMBER)
    ):
        raise Forbidden(acting)
    if membership.role == R.OWNER and await _owner_count(session, organization_id) <= 1:
        raise Conflict("an organization needs at least one owner")
    await session.delete(membership)
    await session.commit()


def invitation_status(invitation: OrganizationInvitation, now: datetime) -> str:
    if invitation.accepted_at:
        return "accepted"
    if invitation.revoked_at:
        return "revoked"
    return "expired" if invitation.expires_at <= now else "pending"


async def create_invitation(
    session: AsyncSession,
    organization_id: uuid.UUID,
    user: User,
    email: str,
    role: OrganizationRole,
) -> tuple[OrganizationInvitation, str]:
    """Returns the invitation and its raw token (to build the link once; never stored)."""
    acting = await _require(session, organization_id, user, MANAGERS)
    if role == R.OWNER or (role == R.ADMIN and acting != R.OWNER):
        raise Forbidden("only owners invite admins; nobody is invited as owner")
    email = email.strip().lower()
    member = await session.scalar(
        select(OrganizationMembership.user_id)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(OrganizationMembership.organization_id == organization_id, User.email == email)
    )
    if member is not None:
        raise Conflict("already a member")
    now = datetime.now(UTC)
    # A still-pending invitation to the same address is replaced, so one link is live.
    previous = await session.scalar(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.email == email,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if previous is not None:
        previous.revoked_at, previous.revoked_by_user_id = now, user.id
        await session.flush()
    token = secrets.token_urlsafe(32)
    invitation = OrganizationInvitation(
        organization_id=organization_id,
        email=email,
        role=role,
        token_hash=digest(token),
        invited_by_user_id=user.id,
        created_at=now,
        expires_at=now + timedelta(days=INVITATION_DAYS),
    )
    session.add(invitation)
    await session.commit()
    return invitation, token


async def list_invitations(
    session: AsyncSession, organization_id: uuid.UUID, user: User
) -> list[tuple[OrganizationInvitation, User | None]]:
    await _require(session, organization_id, user, MANAGERS)
    inviter = aliased(User)
    rows = await session.execute(
        select(OrganizationInvitation, inviter)
        .outerjoin(inviter, inviter.id == OrganizationInvitation.invited_by_user_id)
        .where(OrganizationInvitation.organization_id == organization_id)
        .order_by(OrganizationInvitation.created_at.desc())
        .limit(100)
    )
    return [(i, u) for i, u in rows.all()]


async def revoke_invitation(
    session: AsyncSession, organization_id: uuid.UUID, user: User, invitation_id: uuid.UUID
) -> None:
    await _require(session, organization_id, user, MANAGERS)
    invitation = await session.scalar(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.organization_id == organization_id,
        )
        .with_for_update()
    )
    if invitation is None:
        raise NotFound(invitation_id)
    if invitation_status(invitation, datetime.now(UTC)) != "pending":
        raise Conflict("invitation is no longer pending")
    invitation.revoked_at, invitation.revoked_by_user_id = datetime.now(UTC), user.id
    await session.commit()


async def accept_invitation(session: AsyncSession, user: User, token: str) -> Organization:
    """Single use: the row is locked, checked and marked accepted in one transaction. The
    signed-in user's provider-verified email must be the invited address, so a forwarded
    or leaked link is useless to anyone else."""
    invitation = await session.scalar(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.token_hash == digest(token))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if invitation is None or invitation_status(invitation, now) != "pending":
        raise InvitationUnusable()
    if not user.email_verified or user.email != invitation.email:
        raise Forbidden("signed in with a different or unverified email")
    existing = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == invitation.organization_id,
            OrganizationMembership.user_id == user.id,
        )
    )
    if existing is not None:
        raise Conflict("already a member")
    session.add(
        OrganizationMembership(
            organization_id=invitation.organization_id, user_id=user.id, role=invitation.role
        )
    )
    invitation.accepted_at, invitation.accepted_by_user_id = now, user.id
    organization = await session.get(Organization, invitation.organization_id)
    await session.commit()
    return organization
