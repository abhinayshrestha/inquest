"""Transfers racing each other, credential writes and membership removal (Phase 7C-3).
Committed rows and one session per actor, so the database's locks decide. Each race is
forced in both orders: one transaction takes its locks and is held at commit, the other
starts and must be seen waiting on a lock in PostgreSQL, then the first commits. Every
race ends in one consistent outcome, never a deadlock, an orphaned owner or a previous
owner's credential on the transferred environment."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import delete, or_, select, text

from src.auth.settings import load_auth_settings
from src.credentials.settings import load_credential_config
from src.db.session import SessionLocal
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    EnvironmentTransfer,
    EnvironmentType,
    OrganizationMembership,
    TransferStatus,
    User,
)
from src.services import credentials, organizations, transfers
from tests.credential_fixtures import NEBIUS_URL, configure_credentials, store_committed
from tests.database import OwnerSession
from tests.tenancy import Member, committed_members, configure_auth


@dataclass
class Race:
    alice: Member  # owns the environment
    bob: Member  # recipient
    carol: Member
    environment_id: uuid.UUID
    transfer_id: uuid.UUID


@pytest.fixture
async def race(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Race]:
    configure_auth(monkeypatch)
    configure_credentials(monkeypatch)
    async with committed_members(3) as (alice, bob, carol):
        environment_id = uuid.uuid4()
        try:
            async with SessionLocal() as s, s.begin():
                s.add(Environment(id=environment_id, name=f"env-{environment_id.hex[:8]}",
                                  type=EnvironmentType.LOCAL, process_credentials=True,
                                  **alice.owns))  # fmt: skip
            await store_committed(alice.user_id, environment_id)
            async with SessionLocal() as s:
                transfer = await transfers.propose(
                    s, await s.get(User, alice.user_id), environment_id, bob.user_id
                )
            yield Race(alice, bob, carol, environment_id, transfer.id)
        finally:
            users = [alice.user_id, bob.user_id, carol.user_id]
            # Audit rows are append-only for the runtime role.
            async with OwnerSession() as s, s.begin():
                await s.execute(delete(Environment).where(Environment.id == environment_id))
                await s.execute(
                    delete(AuditEvent).where(
                        or_(
                            AuditEvent.actor_user_id.in_(users),
                            AuditEvent.organization_id == alice.organization_id,
                        )
                    )
                )
    load_auth_settings.cache_clear()
    load_credential_config.cache_clear()


Action = Callable[[Any, User], Any]


@dataclass
class Hold:
    """Holds a transaction at commit, with every lock it took, until released."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


async def _as(member: Member, action: Action, hold: Hold | None = None) -> Any:
    """Runs `action(session, user)` in its own session; returns the result or exception."""
    async with SessionLocal() as s:
        if hold is not None:
            commit = s.commit

            async def held_commit() -> None:
                # Every write executed, so every lock it needs is held; only the commit waits.
                await s.flush()
                hold.reached.set()
                await hold.release.wait()
                await commit()

            s.commit = held_commit  # type: ignore[method-assign]
        try:
            return await action(s, await s.get(User, member.user_id))
        except Exception as exc:
            await s.rollback()
            return exc


async def _blocked_on_a_lock() -> bool:
    async with SessionLocal() as s:
        for _ in range(250):
            waiting = await s.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock'"
                )
            )
            await s.rollback()
            if waiting:
                return True
            await asyncio.sleep(0.02)
    return False


async def _held(first: tuple[Member, Action]) -> tuple[Hold, asyncio.Task[Any]]:
    """Starts `first` and returns once it waits at its commit, holding its locks."""
    hold = Hold()
    holder = asyncio.create_task(_as(*first, hold=hold))
    reached = asyncio.create_task(hold.reached.wait())
    await asyncio.wait({holder, reached}, return_when=asyncio.FIRST_COMPLETED)
    reached.cancel()
    assert hold.reached.is_set(), f"the first transaction ended early: {holder.result()!r}"
    return hold, holder


async def _ordered(first: tuple[Member, Action], second: tuple[Member, Action]) -> list[Any]:
    """`first` reaches its commit holding its locks; `second` starts and must wait for
    them; then `first` commits. Returns [first's result, second's result]."""
    hold, holder = await _held(first)
    waiter = asyncio.create_task(_as(*second))
    blocked = await _blocked_on_a_lock()
    hold.release.set()
    results = [await holder, await waiter]
    assert blocked, f"the second transaction never waited for the first: {results!r}"
    return results


async def _state(r: Race) -> tuple[uuid.UUID, list[uuid.UUID], TransferStatus, bool]:
    async with SessionLocal() as s:
        environment = await s.get(Environment, r.environment_id)
        owners = list(
            await s.scalars(
                select(EnvironmentCredential.owner_user_id).where(
                    EnvironmentCredential.environment_id == r.environment_id
                )
            )
        )
        transfer = await s.get(EnvironmentTransfer, r.transfer_id)
        member = await s.scalar(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == environment.organization_id,
                OrganizationMembership.user_id == environment.owner_user_id,
            )
        )
    assert member is not None, "the owner is always a member"
    return environment.owner_user_id, owners, transfer.status, environment.process_credentials


def _accept(r: Race) -> tuple[Member, Action]:
    return r.bob, lambda s, u: transfers.accept(s, u, r.transfer_id)


def _rotate(r: Race) -> tuple[Member, Action]:
    def action(s, u):
        return credentials.store_credential(
            s, r.environment_id, u, CredentialKind.NEBIUS,
            secret={"api_key": "rotated-key-0123456789", "base_url": NEBIUS_URL},
            public={"main_model": "m", "local_model": "l"}, config=load_credential_config(),
        )  # fmt: skip

    return r.alice, action


def _clear(r: Race) -> tuple[Member, Action]:
    return r.alice, lambda s, u: credentials.clear_credential(
        s, r.environment_id, u, CredentialKind.SIGNOZ
    )


def _remove_recipient(r: Race) -> tuple[Member, Action]:
    return r.carol, lambda s, u: organizations.remove_member(
        s, r.alice.organization_id, u, r.bob.user_id
    )


async def test_concurrent_acceptances_succeed_once(race: Race):
    accepted, replayed = await _ordered(_accept(race), _accept(race))
    assert not isinstance(accepted, Exception)
    assert isinstance(replayed, transfers.TransferConflict)
    others = await asyncio.gather(*(_as(*_accept(race)) for _ in range(3)))
    assert all(isinstance(e, transfers.TransferConflict) for e in others)
    owner, rows, status, granted = await _state(race)
    assert (owner, rows, status, granted) == (race.bob.user_id, [], TransferStatus.ACCEPTED, False)
    async with SessionLocal() as s:
        actions = list(
            await s.scalars(
                select(AuditEvent.action).where(AuditEvent.actor_user_id == race.bob.user_id)
            )
        )
    assert sorted(actions) == ["credentials.revoke", "transfer.accept"]


@pytest.mark.parametrize("write", [_rotate, _clear], ids=["rotate", "clear"])
async def test_acceptance_first_refuses_the_previous_owners_credential_write(race: Race, write):
    accepted, written = await _ordered(_accept(race), write(race))
    assert not isinstance(accepted, Exception)
    assert isinstance(written, credentials.NotEnvironmentOwner)
    owner, rows, status, _ = await _state(race)
    assert (owner, rows, status) == (race.bob.user_id, [], TransferStatus.ACCEPTED)


@pytest.mark.parametrize("write", [_rotate, _clear], ids=["rotate", "clear"])
async def test_a_credential_write_first_is_deleted_by_the_acceptance(race: Race, write):
    written, accepted = await _ordered(write(race), _accept(race))
    assert not isinstance(written, Exception) and not isinstance(accepted, Exception)
    owner, rows, status, _ = await _state(race)
    assert (owner, rows, status) == (race.bob.user_id, [], TransferStatus.ACCEPTED)


async def test_acceptance_first_keeps_the_new_owner_from_being_removed(race: Race):
    accepted, removed = await _ordered(_accept(race), _remove_recipient(race))
    assert not isinstance(accepted, Exception)
    assert isinstance(removed, organizations.Conflict)
    owner, rows, status, _ = await _state(race)
    assert (owner, rows, status) == (race.bob.user_id, [], TransferStatus.ACCEPTED)


async def test_removal_first_closes_the_transfer_before_it_is_accepted(race: Race):
    removed, accepted = await _ordered(_remove_recipient(race), _accept(race))
    assert removed is None
    assert isinstance(accepted, transfers.TransferConflict)
    owner, rows, status, granted = await _state(race)
    assert (owner, len(rows), status, granted) == (
        race.alice.user_id,
        2,
        TransferStatus.CANCELLED,
        True,
    )


async def test_the_previous_owner_can_leave_only_after_acceptance(race: Race):
    def leave(s, u):
        return organizations.remove_member(s, race.alice.organization_id, u, race.alice.user_id)

    hold, accepting = await _held(_accept(race))
    leaving = asyncio.create_task(_as(race.alice, leave))
    await asyncio.wait({leaving}, timeout=1)
    hold.release.set()
    left = await leaving
    assert not isinstance(await accepting, Exception)
    # Refused while the acceptance is uncommitted, or, when leaving had to wait for the
    # owner rows the acceptance locked, allowed once the environment is no longer theirs.
    assert left is None or isinstance(left, organizations.Conflict)
    if left is not None:
        assert await _as(race.alice, leave) is None
    owner, rows, status, _ = await _state(race)
    assert (owner, rows, status) == (race.bob.user_id, [], TransferStatus.ACCEPTED)


async def test_concurrent_proposals_leave_one_pending(race: Race):
    async with SessionLocal() as s:
        await transfers.cancel(s, await s.get(User, race.alice.user_id), race.transfer_id)

    def propose(to: Member) -> tuple[Member, Action]:
        return race.alice, lambda s, u: transfers.propose(s, u, race.environment_id, to.user_id)

    first, second = await _ordered(propose(race.bob), propose(race.carol))
    assert not isinstance(first, Exception)
    assert isinstance(second, transfers.TransferConflict)
    async with SessionLocal() as s:
        pending = await s.scalars(
            select(EnvironmentTransfer.to_user_id).where(
                EnvironmentTransfer.environment_id == race.environment_id,
                EnvironmentTransfer.status == TransferStatus.PENDING,
            )
        )
        assert list(pending) == [race.bob.user_id]
