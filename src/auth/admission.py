"""Admission: who may use Inquest once the provider has authenticated them (D042).

A valid OIDC sign-in proves an identity; under INQUEST_AUTH_ADMISSION=approval it is not
enough. An identity is admitted when it holds a live admission:

- bound to it earlier (operator approval it claimed, or an invitation it accepted);
- an operator pre-approval for its provider-verified email that no identity has claimed
  yet, which it claims now, for good;
- or created now by accepting the invitation the sign-in was started with: the token's
  holder must also be the verified invited address, and the invitation is consumed in the
  same transaction (no admitted user without the membership, no replay).

A withdrawn admission refuses sign-in even with a fresh invitation: an organization admin
cannot override the operator. Everything else is refused, and a refusal stores nothing
(no user row, no session, no membership) and tells the browser only `not_admitted`.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.oidc import VerifiedIdentity
from src.auth.sessions import upsert_user
from src.models import AuditEvent, User, UserAdmission
from src.services import limits
from src.services import organizations as orgs

logger = logging.getLogger(__name__)
INVITATION_REFUSALS = (orgs.InvitationUnusable, orgs.Forbidden, orgs.Conflict, limits.QuotaExceeded)


class NotAdmitted(Exception):
    """`reason` is for the server log only; the browser gets one code for every case."""

    def __init__(self, reason: str):
        self.reason = reason


@dataclass(frozen=True)
class Admitted:
    user: User
    # The organization joined through the sign-in's invitation, if that admitted the user.
    joined: uuid.UUID | None = None


def _who(identity: VerifiedIdentity) -> str:
    # Enough to correlate log lines, without putting the subject or email in the logs.
    return hashlib.sha256(f"{identity.issuer}\n{identity.subject}".encode()).hexdigest()[:12]


def _audit(action: str, user: User, admission: UserAdmission, organization_id=None) -> AuditEvent:
    return AuditEvent(
        action=action,
        actor=f"user:{user.id}",
        actor_user_id=user.id,
        organization_id=organization_id,
        details_json={"admission_id": str(admission.id), "source": admission.source},
    )


async def admit(
    session: AsyncSession, identity: VerifiedIdentity, *, invitation_hash: str | None
) -> Admitted:
    """Commits the admitted user (and what admitted it); NotAdmitted after rolling back."""
    # Concurrent first sign-ins of one identity would race on the users row.
    await limits.lock(session, "identity", _who(identity))
    try:
        return await _admit(session, identity, invitation_hash)
    except NotAdmitted as refused:
        await session.rollback()
        logger.warning(
            "sign-in refused: not admitted (%s, identity %s)", refused.reason, _who(identity)
        )
        raise


async def _admit(
    session: AsyncSession, identity: VerifiedIdentity, invitation_hash: str | None
) -> Admitted:
    user = await session.scalar(
        select(User).where(User.issuer == identity.issuer, User.subject == identity.subject)
    )
    if user is not None and user.disabled_at is not None:
        return Admitted(user)  # refused by the caller as disabled; nothing changes
    if user is not None:
        own = await session.scalar(select(UserAdmission).where(UserAdmission.user_id == user.id))
        if own is not None:
            if own.revoked_at is not None:
                raise NotAdmitted("withdrawn")
            user = await upsert_user(session, identity)
            await session.commit()
            return Admitted(user)
    verified = identity.email if identity.email_verified else None
    if verified is None:
        raise NotAdmitted("no verified email")
    pending = await session.scalar(
        select(UserAdmission)
        .where(
            UserAdmission.email == verified,
            UserAdmission.user_id.is_(None),
            UserAdmission.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if pending is not None:
        user = await upsert_user(session, identity)
        pending.user_id = user.id
        session.add(_audit("admission.claim", user, pending))
        await session.commit()
        logger.info("sign-in admitted: operator approval claimed (identity %s)", _who(identity))
        return Admitted(user)
    if invitation_hash is None:
        raise NotAdmitted("not approved")
    user = await upsert_user(session, identity)
    admission = UserAdmission(user_id=user.id, source="invitation")
    session.add(admission)
    await session.flush()
    try:
        organization = await orgs.accept_invitation_uncommitted(session, user, invitation_hash)
    except INVITATION_REFUSALS as exc:
        raise NotAdmitted(f"invitation refused ({type(exc).__name__})") from None
    session.add(_audit("admission.invitation", user, admission, organization.id))
    await session.commit()
    logger.info("sign-in admitted: invitation accepted (identity %s)", _who(identity))
    return Admitted(user, joined=organization.id)
