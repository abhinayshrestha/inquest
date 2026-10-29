"""Environment ownership transfers (Phase 7C-3, D029, D035).

The environment's owner proposes another active member of its organization; only that
recipient accepts or declines; the proposer may cancel. Nothing changes before acceptance.
Acceptance, in one transaction: the previous owner's credentials are deleted (never shown
or handed over), the process-credential grant is cleared, the owner changes, and both are
audited. Investigations, turns, evidence, reports and checkpoints stay attached to the
environment, which is configuration-required until the new owner sets credentials. The
caller then cancels turns still running on the old credentials
(runtime.cancel_environment_turns).

Concurrency: the environment row is locked first, then the transfer row. Membership is
not locked here: the environments → memberships foreign key (an owner is always a member)
locks the recipient's membership when the owner changes, and removing a member locks
their pending transfers before their membership (organizations.remove_member), so the two
never wait on each other in a cycle. At most one pending transfer per environment (partial
unique index); a transfer is resolved once, under its row lock, so a replayed or
concurrent acceptance is refused.

Errors never disclose: a transfer the caller is not a party to, in another organization or
of an environment they cannot see is TransferNotFound, like any guessed id.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AuditEvent,
    Environment,
    EnvironmentCredential,
    EnvironmentTransfer,
    OrganizationMembership,
    TransferStatus,
    User,
)
from src.services import access
from src.services.credentials import owned_environment

PENDING = TransferStatus.PENDING
OWNER_MEMBERSHIP_FK = "fk_environments_organization_id_organization_memberships"


class TransferNotFound(Exception):
    pass


class TransferConflict(Exception):
    """The request is valid but the transfer's state does not allow it. The message is
    fixed text, safe to show."""


def _active_member(organization_id: Any, user_id: Any) -> ColumnElement[bool]:
    return exists().where(
        OrganizationMembership.organization_id == organization_id,
        OrganizationMembership.user_id == user_id,
        User.id == OrganizationMembership.user_id,
        User.disabled_at.is_(None),
    )


def audit(
    action: str,
    organization_id: uuid.UUID | None,
    user: User,
    transfer: EnvironmentTransfer,
    **extra: Any,
) -> AuditEvent:
    """Ids and fixed reason codes only; never credentials, URLs or names."""
    return AuditEvent(
        action=action,
        actor=f"user:{user.id}",
        actor_user_id=user.id,
        organization_id=organization_id,
        details_json={
            "environment_id": str(transfer.environment_id),
            "transfer_id": str(transfer.id),
            "from_user_id": str(transfer.from_user_id),
            "to_user_id": str(transfer.to_user_id),
            **extra,
        },
    )


def close(transfer: EnvironmentTransfer, status: TransferStatus, user: User) -> None:
    transfer.status = status
    transfer.resolved_at = datetime.now(UTC)
    transfer.resolved_by_user_id = user.id


async def propose(
    session: AsyncSession, user: User, environment_id: uuid.UUID, to_user_id: uuid.UUID
) -> EnvironmentTransfer:
    """NoAccess / NotEnvironmentOwner like credential writes; TransferConflict for a bad
    recipient or an already pending transfer."""
    environment = await owned_environment(session, user, environment_id, lock=True)
    organization_id = environment.organization_id
    if to_user_id == user.id or not await session.scalar(
        select(_active_member(organization_id, to_user_id))
    ):
        raise TransferConflict("the recipient must be another active member of this organization")
    existing = await session.scalar(
        select(EnvironmentTransfer)
        .where(EnvironmentTransfer.environment_id == environment.id)
        .where(EnvironmentTransfer.status == PENDING)
        .with_for_update()
    )
    if existing is not None:
        if await session.scalar(select(_active_member(organization_id, existing.to_user_id))):
            raise TransferConflict("this environment already has a pending transfer")
        # Its recipient left the organization (a race with their removal): void.
        close(existing, TransferStatus.CANCELLED, user)
        session.add(
            audit("transfer.cancel", organization_id, user, existing, reason="recipient_not_member")
        )
        await session.flush()
    transfer = EnvironmentTransfer(
        id=uuid.uuid4(), environment_id=environment.id, from_user_id=user.id, to_user_id=to_user_id
    )
    session.add(transfer)
    session.add(audit("transfer.propose", organization_id, user, transfer))
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise TransferConflict("this environment already has a pending transfer") from exc
    return transfer


async def _party_transfer(
    session: AsyncSession, user: User, transfer_id: uuid.UUID, *, recipient: bool
) -> tuple[EnvironmentTransfer, Environment]:
    """The transfer and its environment, both locked (environment first), for its
    recipient or its proposer only, while they can still see the environment."""
    transfer = await session.get(EnvironmentTransfer, transfer_id)
    party = transfer and (transfer.to_user_id if recipient else transfer.from_user_id)
    if transfer is None or party != user.id:
        raise TransferNotFound(transfer_id)
    try:
        await access.environment_for(session, user, transfer.environment_id)
    except access.NoAccess as exc:
        raise TransferNotFound(transfer_id) from exc
    environment = await session.scalar(
        select(Environment)
        .where(Environment.id == transfer.environment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    transfer = await session.scalar(
        select(EnvironmentTransfer)
        .where(EnvironmentTransfer.id == transfer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert environment is not None and transfer is not None
    if transfer.status != PENDING:
        raise TransferConflict("this transfer is no longer pending")
    return transfer, environment


async def accept(
    session: AsyncSession, user: User, transfer_id: uuid.UUID
) -> tuple[EnvironmentTransfer, Environment]:
    """Makes the recipient the owner. Returns the transfer and the environment, whose
    running turns the caller must then cancel."""
    transfer, environment = await _party_transfer(session, user, transfer_id, recipient=True)
    # Only the owner proposes and only acceptance changes the owner, so this holds unless
    # something outside this workflow changed it; refuse rather than act on a stale offer.
    if environment.owner_user_id != transfer.from_user_id:
        raise TransferConflict("this transfer no longer matches the environment's owner")
    # Never decrypted, shown or re-bound: the previous owner's credentials are deleted.
    kinds = sorted(
        kind.value
        for kind in await session.scalars(
            delete(EnvironmentCredential)
            .where(EnvironmentCredential.environment_id == environment.id)
            .returning(EnvironmentCredential.kind)
        )
    )
    granted = environment.process_credentials
    environment.owner_user_id = user.id
    environment.process_credentials = False
    close(transfer, TransferStatus.ACCEPTED, user)
    session.add(audit("transfer.accept", environment.organization_id, user, transfer))
    session.add(
        audit(
            "credentials.revoke",
            environment.organization_id,
            user,
            transfer,
            reason="ownership_transfer",
            kinds=kinds,
            process_credentials_cleared=granted,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # The recipient's membership ended concurrently: they cannot become the owner.
        if exc.orig.diag.constraint_name == OWNER_MEMBERSHIP_FK:
            raise TransferNotFound(transfer_id) from exc
        raise
    return transfer, environment


async def decline(session: AsyncSession, user: User, transfer_id: uuid.UUID) -> EnvironmentTransfer:
    transfer, environment = await _party_transfer(session, user, transfer_id, recipient=True)
    close(transfer, TransferStatus.DECLINED, user)
    session.add(audit("transfer.decline", environment.organization_id, user, transfer))
    await session.commit()
    return transfer


async def cancel(session: AsyncSession, user: User, transfer_id: uuid.UUID) -> EnvironmentTransfer:
    transfer, environment = await _party_transfer(session, user, transfer_id, recipient=False)
    close(transfer, TransferStatus.CANCELLED, user)
    session.add(
        audit("transfer.cancel", environment.organization_id, user, transfer, reason="proposer")
    )
    await session.commit()
    return transfer


async def pending_transfers(
    session: AsyncSession, user: User
) -> list[tuple[EnvironmentTransfer, Environment]]:
    """Pending transfers to or from the caller, in organizations they belong to, that can
    still be accepted (the proposer still owns the environment, the recipient is still an
    active member)."""
    rows = await session.execute(
        select(EnvironmentTransfer, Environment)
        .join(Environment, Environment.id == EnvironmentTransfer.environment_id)
        .where(
            EnvironmentTransfer.status == PENDING,
            or_(
                EnvironmentTransfer.to_user_id == user.id,
                EnvironmentTransfer.from_user_id == user.id,
            ),
            Environment.organization_id.in_(
                select(OrganizationMembership.organization_id).where(
                    OrganizationMembership.user_id == user.id
                )
            ),
            Environment.owner_user_id == EnvironmentTransfer.from_user_id,
            _active_member(Environment.organization_id, EnvironmentTransfer.to_user_id),
        )
        .order_by(EnvironmentTransfer.created_at)
    )
    return [(t, e) for t, e in rows.all()]
