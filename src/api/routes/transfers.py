"""Environment ownership transfers (Phase 7C-3, D035). The owner proposes, the recipient
accepts or declines, the proposer cancels. Session, CSRF (writes) and current membership
on every call; anything the caller is not a party to is 404.

Accepting deletes the previous owner's credentials; the environment's running turns are
then cancelled (they could not resolve credentials any more anyway).
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from src.api.deps import SessionDep
from src.auth.deps import CurrentUser, WritingUser
from src.investigators import runtime
from src.models import Environment, EnvironmentTransfer, User
from src.schemas.transfers import TransferCreate, TransferRead
from src.services import access, credentials, limits, transfers

router = APIRouter(tags=["transfers"])


def _read(transfer: EnvironmentTransfer, environment: Environment, user: User) -> TransferRead:
    assert environment.organization_id is not None
    return TransferRead(
        id=transfer.id,
        environment_id=environment.id,
        environment_name=environment.name,
        organization_id=environment.organization_id,
        from_user_id=transfer.from_user_id,
        to_user_id=transfer.to_user_id,
        status=transfer.status.value,
        direction="incoming" if transfer.to_user_id == user.id else "outgoing",
        created_at=transfer.created_at,
        resolved_at=transfer.resolved_at,
    )


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, (transfers.TransferNotFound, access.NoAccess)):
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if isinstance(exc, credentials.NotEnvironmentOwner):
        return HTTPException(
            status.HTTP_403_FORBIDDEN, "only the environment owner can transfer it"
        )
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


ERRORS = (
    transfers.TransferNotFound,
    transfers.TransferConflict,
    access.NoAccess,
    credentials.NotEnvironmentOwner,
)


async def _environment(session: SessionDep, transfer: EnvironmentTransfer) -> Environment:
    environment = await session.get(Environment, transfer.environment_id)
    assert environment is not None
    return environment


@router.post(
    "/environments/{environment_id}/transfers",
    status_code=status.HTTP_201_CREATED,
    response_model=TransferRead,
)
async def propose_transfer(
    environment_id: uuid.UUID, data: TransferCreate, session: SessionDep, identity: WritingUser
) -> TransferRead:
    """Offers the environment to another active member. Nothing changes until they accept."""
    # Each proposal is a stored row, even when cancelled again.
    await limits.hit(session, "transfer", user=identity.user.id)
    try:
        transfer = await transfers.propose(session, identity.user, environment_id, data.to_user_id)
    except ERRORS as exc:
        raise _http(exc) from exc
    return _read(transfer, await _environment(session, transfer), identity.user)


@router.get("/transfers", response_model=list[TransferRead])
async def list_transfers(session: SessionDep, identity: CurrentUser) -> list[TransferRead]:
    """Pending transfers to the caller (to accept or decline) and from them (to cancel)."""
    rows = await transfers.pending_transfers(session, identity.user)
    return [_read(t, e, identity.user) for t, e in rows]


@router.post("/transfers/{transfer_id}/accept", response_model=TransferRead)
async def accept_transfer(
    transfer_id: uuid.UUID, session: SessionDep, identity: WritingUser
) -> TransferRead:
    """Makes the caller the owner. The previous owner's credentials are deleted, never
    handed over: investigations wait until the caller sets their own."""
    try:
        transfer, environment = await transfers.accept(session, identity.user, transfer_id)
    except ERRORS as exc:
        raise _http(exc) from exc
    assert environment.organization_id is not None
    await runtime.stop_environment_runs(
        environment.id, "ownership_transferred", organization_id=environment.organization_id,
        actor_user_id=identity.user.id,
    )  # fmt: skip
    return _read(transfer, environment, identity.user)


@router.post("/transfers/{transfer_id}/decline", response_model=TransferRead)
async def decline_transfer(
    transfer_id: uuid.UUID, session: SessionDep, identity: WritingUser
) -> TransferRead:
    try:
        transfer = await transfers.decline(session, identity.user, transfer_id)
    except ERRORS as exc:
        raise _http(exc) from exc
    return _read(transfer, await _environment(session, transfer), identity.user)


@router.post("/transfers/{transfer_id}/cancel", response_model=TransferRead)
async def cancel_transfer(
    transfer_id: uuid.UUID, session: SessionDep, identity: WritingUser
) -> TransferRead:
    try:
        transfer = await transfers.cancel(session, identity.user, transfer_id)
    except ERRORS as exc:
        raise _http(exc) from exc
    return _read(transfer, await _environment(session, transfer), identity.user)
