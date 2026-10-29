"""Operator decisions on who may use Inquest (D042, D043), for scripts.admit_user. There is
no HTTP equivalent: these are made on the backend host, not by signed-in users.

Two separate switches, both kept on the user's existing rows:
- admission (`user_admissions`, D042): whether this identity may use Inquest at all,
  enforced only under INQUEST_AUTH_ADMISSION=approval;
- disabling (`users.disabled_at`, D043): an account suspension under every policy, for
  incidents or a pause; it leaves the admission decision as it was, so re-enabling
  restores exactly the earlier state.

Every change is audited (ids only, never emails) and planned in the caller's
transaction, which the caller commits only with --apply. Withdrawal and disabling end the
user's sessions at once and change nothing else: memberships, organization ownership and
environment ownership stay, so no environment is orphaned or silently transferred.
Changes hold the per-user lock that session creation takes, so a sign-in racing them
cannot leave a session behind.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.sessions import revoke_user_sessions
from src.models import (
    AuditEvent,
    Environment,
    EnvironmentTransfer,
    OrganizationMembership,
    TransferStatus,
    User,
    UserAdmission,
)
from src.services import limits


class AdmissionRefused(Exception):
    """The requested change would be unsafe or makes no sense; the message says why."""


@dataclass
class Change:
    action: str
    admission_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    # Left untouched by a withdrawal; reported so the operator sees what remains.
    memberships: int = 0
    owned_environments: int = 0
    pending_transfers: int = 0
    sessions_ended: int = 0
    users: list[uuid.UUID] = field(default_factory=list)


def _audit(action: str, actor: str, **details: object) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor=actor[:200],
        details_json={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in details.items()},
    )


async def approve_email(session: AsyncSession, email: str, *, actor: str) -> Change:
    """A pre-approval the first sign-in with this provider-verified address claims."""
    email = email.strip().lower()
    if "@" not in email or len(email) > 320:
        raise AdmissionRefused("not an email address")
    pending = await session.scalar(
        select(UserAdmission).where(
            UserAdmission.email == email,
            UserAdmission.user_id.is_(None),
            UserAdmission.revoked_at.is_(None),
        )
    )
    if pending is not None:
        return Change("unchanged (already approved, not yet claimed)", pending.id)
    # A second account with an admitted user's address must not be let in by email.
    holder = await session.scalar(
        select(UserAdmission.user_id)
        .join(User, User.id == UserAdmission.user_id)
        .where(User.email == email, User.email_verified)
    )
    if holder is not None:
        raise AdmissionRefused(
            f"user {holder} with this email already has an admission; use --user {holder}"
        )
    admission = UserAdmission(email=email, source="operator")
    session.add(admission)
    await session.flush()
    session.add(_audit("admission.approve", actor, admission_id=admission.id))
    return Change("approved; claimed at the first sign-in with this verified email", admission.id)


async def _left_alone(session: AsyncSession, change: Change) -> Change:
    """What a withdrawal or disabling leaves as it was, for the operator to see."""
    user_id = change.user_id
    change.memberships = await session.scalar(
        select(func.count()).where(OrganizationMembership.user_id == user_id)
    )
    change.owned_environments = await session.scalar(
        select(func.count()).where(Environment.owner_user_id == user_id)
    )
    change.pending_transfers = await session.scalar(
        select(func.count()).where(
            EnvironmentTransfer.status == TransferStatus.PENDING,
            or_(
                EnvironmentTransfer.from_user_id == user_id,
                EnvironmentTransfer.to_user_id == user_id,
            ),
        )
    )
    return change


async def _locked_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    # The user row first, then the per-user lock: the order a sign-in takes them (its
    # profile update, then session creation), so the two cannot deadlock. NO KEY UPDATE,
    # like that profile update: rows that merely reference the user (memberships,
    # sessions) take KEY SHARE and are not blocked.
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update(key_share=True)
    )
    if user is None:
        raise AdmissionRefused("no such user")
    await limits.lock(session, "user", user_id)
    return user


async def approve_user(session: AsyncSession, user_id: uuid.UUID, *, actor: str) -> Change:
    """Admits an existing user, or restores a withdrawn admission. Sessions ended by the
    withdrawal stay ended."""
    await _locked_user(session, user_id)
    admission = await session.scalar(
        select(UserAdmission).where(UserAdmission.user_id == user_id).with_for_update()
    )
    if admission is not None and admission.revoked_at is None:
        return Change("unchanged (already admitted)", admission.id, user_id)
    if admission is None:
        admission = UserAdmission(user_id=user_id, source="operator")
        session.add(admission)
        await session.flush()
    else:
        admission.revoked_at = None
    session.add(_audit("admission.approve", actor, admission_id=admission.id, user_id=user_id))
    return Change("admitted", admission.id, user_id)


async def revoke_user(session: AsyncSession, user_id: uuid.UUID, *, actor: str) -> Change:
    """Withdraws a user's admission (recorded even if they had none, so a pending email
    approval or an invitation cannot let them back in) and ends their sessions."""
    await _locked_user(session, user_id)
    now = datetime.now(UTC)
    admission = await session.scalar(
        select(UserAdmission).where(UserAdmission.user_id == user_id).with_for_update()
    )
    if admission is None:
        admission = UserAdmission(user_id=user_id, source="operator", revoked_at=now)
        session.add(admission)
        await session.flush()
    elif admission.revoked_at is None:
        admission.revoked_at = now
    ended = await revoke_user_sessions(session, user_id)
    session.add(_audit("admission.revoke", actor, admission_id=admission.id, user_id=user_id))
    return await _left_alone(
        session, Change("withdrawn", admission.id, user_id, sessions_ended=ended)
    )


async def disable_user(session: AsyncSession, user_id: uuid.UUID, *, actor: str) -> Change:
    """Suspends an account under every admission policy: no new sign-in (whatever approval
    or invitation exists for it) and every session and open stream ends. Nothing is
    deleted or transferred; the admission decision is kept for re-enabling."""
    user = await _locked_user(session, user_id)
    ended = await revoke_user_sessions(session, user_id)
    if user.disabled_at is not None:
        change = Change("unchanged (already disabled)", user_id=user_id, sessions_ended=ended)
    else:
        user.disabled_at = datetime.now(UTC)
        session.add(_audit("user.disable", actor, user_id=user_id, sessions_ended=ended))
        change = Change("disabled", user_id=user_id, sessions_ended=ended)
    return await _left_alone(session, change)


async def enable_user(session: AsyncSession, user_id: uuid.UUID, *, actor: str) -> Change:
    """Lifts a suspension. No session comes back (any left over is ended too): the user
    signs in again. Admission is as it was before disabling."""
    user = await _locked_user(session, user_id)
    if user.disabled_at is None:
        return Change("unchanged (not disabled)", user_id=user_id)
    user.disabled_at = None
    ended = await revoke_user_sessions(session, user_id)
    session.add(_audit("user.enable", actor, user_id=user_id, sessions_ended=ended))
    return Change("enabled; the user signs in again", user_id=user_id, sessions_ended=ended)


async def revoke_email(session: AsyncSession, email: str, *, actor: str) -> Change:
    """Withdraws a pre-approval nobody has claimed yet."""
    pending = await session.scalar(
        select(UserAdmission)
        .where(
            UserAdmission.email == email.strip().lower(),
            UserAdmission.user_id.is_(None),
            UserAdmission.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if pending is None:
        raise AdmissionRefused("no unclaimed approval for this email (claimed ones: --user)")
    pending.revoked_at = datetime.now(UTC)
    session.add(_audit("admission.revoke", actor, admission_id=pending.id))
    return Change("withdrawn", pending.id)


async def admit_members(session: AsyncSession, *, actor: str) -> Change:
    """The explicit policy for users from before admission control: every user who holds a
    membership and has no admission decision yet is admitted. Never automatic."""
    users = list(
        await session.scalars(
            select(User.id)
            .where(
                User.id.in_(select(OrganizationMembership.user_id)),
                User.id.not_in(
                    select(UserAdmission.user_id).where(UserAdmission.user_id.is_not(None))
                ),
            )
            .order_by(User.created_at)
        )
    )
    for user_id in users:
        session.add(UserAdmission(user_id=user_id, source="operator"))
    if users:
        session.add(_audit("admission.backfill", actor, users=len(users)))
    return Change(f"admitted {len(users)} existing member(s)", users=users)


async def list_admissions(session: AsyncSession) -> list[tuple[UserAdmission, User | None]]:
    return list(
        (
            await session.execute(
                select(UserAdmission, User)
                .outerjoin(User, User.id == UserAdmission.user_id)
                .order_by(UserAdmission.created_at)
            )
        ).all()
    )
