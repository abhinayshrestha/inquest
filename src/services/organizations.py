"""Organizations, memberships and invitations (D025, D031).

Every function takes the acting user from the verified session and checks that user's
membership and role here, on the server. A user who is not a member of an organization
gets NotFound for it, so outsiders cannot even learn that it exists.

Roles: owner — everything, including roles; admin — invite members, revoke invitations,
remove members; member — see the organization and its members. Organization roles grant
no access to any environment's credentials (D029).

A member who owns environments can neither leave nor be removed until they transferred
them (D035): checked here with a clear message, and enforced by the database (the
environments → memberships foreign key), whatever the race.
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
    Environment,
    EnvironmentTransfer,
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    OrganizationRole,
    TransferStatus,
    User,
)
from src.services import limits, transfers

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


async def require_role(
    session: AsyncSession, organization_id: uuid.UUID, user: User, allowed: frozenset
) -> OrganizationRole:
    role = await membership_role(session, organization_id, user)
    if role not in allowed:
        raise Forbidden(role)
    return role


async def _memberships(session: AsyncSession, user_id: uuid.UUID) -> int | None:
    return await session.scalar(
        select(func.count()).where(OrganizationMembership.user_id == user_id)
    )


async def _pending_invitations(
    session: AsyncSession, organization_id: uuid.UUID, now: datetime
) -> int | None:
    return await session.scalar(
        select(func.count()).where(
            OrganizationInvitation.organization_id == organization_id,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
            OrganizationInvitation.expires_at > now,
        )
    )


async def _members(session: AsyncSession, organization_id: uuid.UUID) -> int | None:
    return await session.scalar(
        select(func.count()).where(OrganizationMembership.organization_id == organization_id)
    )


async def create_organization(session: AsyncSession, user: User, name: str) -> Organization:
    """QuotaExceeded beyond the organizations a user may create or belong to; counted
    under the user's quota lock, so concurrent creations cannot overshoot."""
    name = " ".join(name.split())
    if not name:
        raise Conflict("name is empty")
    await limits.lock(session, "user", user.id)
    limits.require(
        "organizations_per_user",
        await session.scalar(
            select(func.count()).where(Organization.created_by_user_id == user.id)
        ),
    )
    limits.require("memberships_per_user", await _memberships(session, user.id))
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
    await require_role(session, organization_id, user, frozenset({R.OWNER}))
    membership = await _locked_membership(session, organization_id, target_user_id)
    if membership.role == R.OWNER and role != R.OWNER:
        if await _owner_count(session, organization_id) <= 1:
            raise Conflict("an organization needs at least one owner")
    membership.role = role
    await session.commit()
    return membership


OWNS_ENVIRONMENTS = "this member owns environments here; transfer them before leaving or removal"


async def remove_member(
    session: AsyncSession, organization_id: uuid.UUID, user: User, target_user_id: uuid.UUID
) -> None:
    acting = await membership_role(session, organization_id, user)
    # Pending transfers to the member close with the membership. Locked before it: transfer
    # acceptance locks the transfer and then (through the owner foreign key) the membership.
    incoming = list(
        await session.scalars(
            select(EnvironmentTransfer)
            .join(Environment, Environment.id == EnvironmentTransfer.environment_id)
            .where(
                Environment.organization_id == organization_id,
                EnvironmentTransfer.to_user_id == target_user_id,
                EnvironmentTransfer.status == TransferStatus.PENDING,
            )
            .with_for_update(of=EnvironmentTransfer)
        )
    )
    membership = await _locked_membership(session, organization_id, target_user_id)
    leaving = target_user_id == user.id
    # Members may leave; admins remove members; owners remove anyone.
    if not leaving and not (
        acting == R.OWNER or (acting == R.ADMIN and membership.role == R.MEMBER)
    ):
        raise Forbidden(acting)
    if membership.role == R.OWNER and await _owner_count(session, organization_id) <= 1:
        raise Conflict("an organization needs at least one owner")
    owned = await session.scalar(
        select(func.count()).where(
            Environment.organization_id == organization_id,
            Environment.owner_user_id == target_user_id,
        )
    )
    if owned:
        raise Conflict(OWNS_ENVIRONMENTS)
    for transfer in incoming:
        transfers.close(transfer, TransferStatus.CANCELLED, user)
        session.add(
            transfers.audit(
                "transfer.cancel", organization_id, user, transfer, reason="recipient_not_member"
            )
        )
    await session.delete(membership)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # An environment became theirs after the check above (a transfer accepted meanwhile).
        if exc.orig.diag.constraint_name == transfers.OWNER_MEMBERSHIP_FK:
            raise Conflict(OWNS_ENVIRONMENTS) from exc
        raise


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
    """Returns the invitation and its raw token (to build the link once; never stored).
    QuotaExceeded beyond the organization's pending invitations, or members plus pending
    invitations; counted under its quota lock."""
    acting = await require_role(session, organization_id, user, MANAGERS)
    if role == R.OWNER or (role == R.ADMIN and acting != R.OWNER):
        raise Forbidden("only owners invite admins; nobody is invited as owner")
    await limits.lock(session, "organization", organization_id)
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
    pending = await _pending_invitations(session, organization_id, now) or 0
    limits.require("pending_invitations_per_organization", pending)
    limits.require(
        "members_per_organization", (await _members(session, organization_id) or 0) + pending
    )
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
    await require_role(session, organization_id, user, MANAGERS)
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
    await require_role(session, organization_id, user, MANAGERS)
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
    or leaked link is useless to anyone else. QuotaExceeded beyond the organization's
    members or the user's memberships; quota locks (organization, then user) are taken
    before the invitation row, in the order invitation creation takes them."""
    organization = await accept_invitation_uncommitted(session, user, digest(token))
    await session.commit()
    return organization


async def accept_invitation_uncommitted(
    session: AsyncSession, user: User, token_hash: str
) -> Organization:
    """accept_invitation by token hash, leaving the commit to the caller: admission at
    sign-in (D042) creates the user, its admission and the membership in one transaction."""
    organization_id = await session.scalar(
        select(OrganizationInvitation.organization_id).where(
            OrganizationInvitation.token_hash == token_hash
        )
    )
    if organization_id is None:
        raise InvitationUnusable()
    await limits.lock(session, "organization", organization_id)
    await limits.lock(session, "user", user.id)
    invitation = await session.scalar(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.token_hash == token_hash)
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
    limits.require("members_per_organization", await _members(session, organization_id))
    limits.require("memberships_per_user", await _memberships(session, user.id))
    session.add(
        OrganizationMembership(
            organization_id=invitation.organization_id, user_id=user.id, role=invitation.role
        )
    )
    invitation.accepted_at, invitation.accepted_by_user_id = now, user.id
    return await session.get(Organization, invitation.organization_id)
