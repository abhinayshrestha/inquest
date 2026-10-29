"""Environment ownership transfers and the owner leave/removal guard (Phase 7C-3, D035).
Two organizations, several users per organization, everything through the API on the
rolled-back test session. Providers are mock transports only."""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.credentials import provider_client
from src.auth.settings import load_auth_settings
from src.credentials import resolver
from src.db.session import get_session
from src.main import app
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    EnvironmentTransfer,
    EnvironmentType,
    Investigation,
    InvestigationEnvironment,
    OrganizationMembership,
    OrganizationRole,
    TransferStatus,
    TriggerType,
    User,
)
from src.schemas.environment import EnvironmentCreate
from src.services import access, environments
from tests.credential_fixtures import (
    NEBIUS_SECRET,
    NEBIUS_URL,
    credential_storage,  # noqa: F401  (fixture)
    store_credentials,
)
from tests.tenancy import Member, api_client, configure_auth, make_member

pytestmark = pytest.mark.usefixtures("credential_storage")

R = OrganizationRole
MODELS = {"data": [{"id": "main-N"}, {"id": "local-N"}]}


@dataclass
class People:
    alice: Member  # organization owner, owns the environment
    bob: Member  # member
    carol: Member  # admin
    olga: Member  # second organization owner
    erin: Member  # member of the other organization
    environment: Environment
    session: AsyncSession


@pytest.fixture
async def people(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[People]:
    configure_auth(monkeypatch)
    alice = await make_member(session, name="alice")
    org = alice.organization_id
    bob = await make_member(session, organization_id=org, role=R.MEMBER, name="bob")
    carol = await make_member(session, organization_id=org, role=R.ADMIN, name="carol")
    olga = await make_member(session, organization_id=org, role=R.OWNER, name="olga")
    erin = await make_member(session, name="erin")
    environment = Environment(
        name=f"env-{uuid.uuid4().hex[:8]}",
        type=EnvironmentType.LOCAL,
        process_credentials=True,
        **alice.owns,
    )
    session.add(environment)
    await session.flush()
    await store_credentials(session, alice.user_id, environment.id)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[provider_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=MODELS))
    )
    yield People(alice, bob, carol, olga, erin, environment, session)
    app.dependency_overrides.clear()
    load_auth_settings.cache_clear()


async def _call(member: Member, method: str, url: str, body: object = None) -> httpx.Response:
    async with api_client(member) as client:
        return await client.request(method, url, json=body)


async def _propose(p: People, by: Member, to: Member | uuid.UUID) -> httpx.Response:
    to_id = to if isinstance(to, uuid.UUID) else to.user_id
    return await _call(
        by, "POST", f"/environments/{p.environment.id}/transfers", {"to_user_id": str(to_id)}
    )


async def _act(member: Member, transfer_id: str, action: str) -> httpx.Response:
    return await _call(member, "POST", f"/transfers/{transfer_id}/{action}")


async def _environment(p: People) -> Environment:
    found = await p.session.get(Environment, p.environment.id, populate_existing=True)
    assert found is not None
    return found


async def _audit(p: People) -> list[AuditEvent]:
    # Rows written in one test transaction share created_at (now() is the transaction's
    # start), so their order among themselves is not defined: assert by action, not index.
    return list(
        await p.session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == p.alice.organization_id)
            .order_by(AuditEvent.created_at)
        )
    )


def _no_secret(*texts: str) -> None:
    for value in texts:
        assert "SENTINEL" not in value and NEBIUS_URL not in value


# --- the workflow -------------------------------------------------------------------------


async def test_accepting_moves_ownership_and_deletes_the_previous_owners_credentials(
    people: People,
):
    p = people
    investigation = Investigation(
        title="kept", question="why?", trigger_type=TriggerType.MANUAL,
        organization_id=p.alice.organization_id, created_by_user_id=p.alice.user_id,
    )  # fmt: skip
    p.session.add(investigation)
    await p.session.flush()
    p.session.add(
        InvestigationEnvironment(investigation_id=investigation.id, environment_id=p.environment.id)
    )
    await p.session.flush()

    proposed = await _propose(p, p.alice, p.bob)
    assert proposed.status_code == 201, proposed.text
    body = proposed.json()
    assert (body["status"], body["direction"], body["to_user_id"]) == (
        "pending",
        "outgoing",
        str(p.bob.user_id),
    )
    # Nothing changes before acceptance.
    assert (await _environment(p)).owner_user_id == p.alice.user_id

    incoming = (await _call(p.bob, "GET", "/transfers")).json()
    assert [(t["id"], t["direction"]) for t in incoming] == [(body["id"], "incoming")]
    assert (await _call(p.alice, "GET", "/transfers")).json()[0]["direction"] == "outgoing"
    for outsider in (p.carol, p.olga, p.erin):
        assert (await _call(outsider, "GET", "/transfers")).json() == []

    accepted = await _act(p.bob, body["id"], "accept")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted" and accepted.json()["resolved_at"]

    environment = await _environment(p)
    assert (environment.owner_user_id, environment.process_credentials) == (p.bob.user_id, False)
    rows = await p.session.scalars(
        select(EnvironmentCredential).where(EnvironmentCredential.environment_id == environment.id)
    )
    assert list(rows) == []
    # History stays attached to the environment and readable by members.
    assert (await _call(p.carol, "GET", f"/investigations/{investigation.id}")).status_code == 200

    detail = (await _call(p.bob, "GET", f"/environments/{environment.id}")).json()
    assert detail["credentials"] == "configuration_required"
    statuses = (await _call(p.bob, "GET", f"/environments/{environment.id}/credentials")).json()
    assert {s["state"] for s in statuses} == {"not_configured"}
    with pytest.raises(resolver.CredentialRefused) as refused:
        await resolver.environment_credentials(p.session, environment)
    assert refused.value.code == "configuration_required"

    audits = await _audit(p)
    actions = [a.action for a in audits]
    assert {"transfer.propose", "transfer.accept", "credentials.revoke"} <= set(actions)
    (revoked,) = [a for a in audits if a.action == "credentials.revoke"]
    revoke = revoked.details_json
    assert revoke["kinds"] == ["nebius", "signoz"] and revoke["process_credentials_cleared"]
    assert revoke["reason"] == "ownership_transfer" and revoked.actor_user_id == p.bob.user_id
    _no_secret(proposed.text, accepted.text, json.dumps([a.details_json for a in audits]))


async def test_the_new_owner_sets_credentials_and_the_previous_owner_cannot(people: People):
    p = people
    transfer = (await _propose(p, p.alice, p.bob)).json()
    await _act(p.bob, transfer["id"], "accept")
    nebius = {"api_key": NEBIUS_SECRET + "-N", "base_url": NEBIUS_URL,
              "main_model": "main-N", "local_model": "local-N"}  # fmt: skip
    url = f"/environments/{p.environment.id}/credentials"

    # The previous owner (still an organization owner) and admins have no say any more.
    for other in (p.alice, p.carol, p.olga):
        assert (await _call(other, "PUT", f"{url}/nebius", nebius)).status_code == 403
        assert (await _call(other, "DELETE", f"{url}/nebius")).status_code == 403
        assert (await _propose(p, other, p.carol)).status_code == 403
    assert (await _call(p.erin, "PUT", f"{url}/nebius", nebius)).status_code == 404

    stored = await _call(p.bob, "PUT", f"{url}/nebius", nebius)
    assert stored.status_code == 200 and stored.json()["main_model"] == "main-N"
    row = await p.session.get(EnvironmentCredential, (p.environment.id, CredentialKind.NEBIUS))
    assert row is not None and row.owner_user_id == p.bob.user_id


async def test_previous_ciphertext_cannot_be_reattached_after_a_transfer(people: People):
    p = people
    old = await p.session.get(EnvironmentCredential, (p.environment.id, CredentialKind.NEBIUS))
    assert old is not None
    sealed = (old.key_id, old.nonce, old.ciphertext, dict(old.public_json))
    transfer = (await _propose(p, p.alice, p.bob)).json()
    await _act(p.bob, transfer["id"], "accept")

    # Under the old owner: the (environment, owner) foreign key refuses the row.
    with pytest.raises(IntegrityError):
        async with p.session.begin_nested():
            p.session.add(_row(p, p.alice.user_id, sealed))
    # Relabelled with the new owner: it no longer authenticates (owner in associated data).
    p.session.add(_row(p, p.bob.user_id, sealed))
    await store_credentials(p.session, p.bob.user_id, p.environment.id, tag="B",
                            kinds=(CredentialKind.SIGNOZ,))  # fmt: skip
    with pytest.raises(resolver.CredentialRefused) as refused:
        await resolver.environment_credentials(p.session, await _environment(p))
    assert refused.value.code == "credentials_unavailable"


def _row(p: People, owner: uuid.UUID, sealed: tuple) -> EnvironmentCredential:
    key_id, nonce, ciphertext, public = sealed
    return EnvironmentCredential(
        environment_id=p.environment.id, kind=CredentialKind.NEBIUS, owner_user_id=owner,
        key_id=key_id,
        nonce=nonce, ciphertext=ciphertext, public_json=public, updated_by_user_id=owner,
    )  # fmt: skip


async def test_decline_and_cancel_close_without_changing_anything(people: People):
    p = people
    first = (await _propose(p, p.alice, p.bob)).json()
    declined = await _act(p.bob, first["id"], "decline")
    assert declined.status_code == 200 and declined.json()["status"] == "declined"

    second = (await _propose(p, p.alice, p.carol)).json()
    cancelled = await _act(p.alice, second["id"], "cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"

    environment = await _environment(p)
    assert environment.owner_user_id == p.alice.user_id and environment.process_credentials
    rows = await p.session.scalars(
        select(EnvironmentCredential.kind).where(
            EnvironmentCredential.environment_id == environment.id
        )
    )
    assert len(list(rows)) == 2
    actions = [a.action for a in await _audit(p)]
    assert sorted(a for a in actions if a.startswith("transfer.")) == [
        "transfer.cancel",
        "transfer.decline",
        "transfer.propose",
        "transfer.propose",
    ]
    for t in (first, second):
        for who, action in ((p.bob, "accept"), (p.alice, "cancel")):
            assert (await _act(who, t["id"], action)).status_code in (404, 409)


async def test_only_the_parties_may_act_and_others_learn_nothing(people: People):
    p = people
    transfer = (await _propose(p, p.alice, p.bob)).json()["id"]
    unknown = str(uuid.uuid4())

    # Accept and decline: the recipient only. Cancel: the proposer only. Everyone else,
    # including organization owners and admins, gets the same 404 as for a guessed id.
    for who in (p.alice, p.carol, p.olga, p.erin):
        for action in ("accept", "decline"):
            response = await _act(who, transfer, action)
            assert (response.status_code, response.json()) == (404, {"detail": "not found"})
    for who in (p.bob, p.carol, p.olga, p.erin):
        assert (await _act(who, transfer, "cancel")).status_code == 404
    assert (await _act(p.bob, unknown, "accept")).json() == {"detail": "not found"}
    assert (await _environment(p)).owner_user_id == p.alice.user_id


async def test_proposals_need_the_owner_and_another_active_member(people: People):
    p = people
    # Not the environment's owner: members, admins and organization owners alike.
    for who in (p.bob, p.carol, p.olga):
        response = await _propose(p, who, p.bob)
        assert (response.status_code, response.json()["detail"]) == (
            403,
            "only the environment owner can transfer it",
        )
    assert (await _propose(p, p.erin, p.erin)).status_code == 404

    disabled = await make_member(p.session, organization_id=p.alice.organization_id,
                                 role=R.MEMBER, name="gone")  # fmt: skip
    await p.session.execute(
        update(User).where(User.id == disabled.user_id).values(disabled_at=datetime.now(UTC))
    )
    for recipient in (p.alice, p.erin, disabled, uuid.uuid4()):
        response = await _propose(p, p.alice, recipient)
        assert (response.status_code, response.json()["detail"]) == (
            409,
            "the recipient must be another active member of this organization",
        )
    assert (await _propose(p, p.alice, p.bob)).status_code == 201
    duplicate = await _propose(p, p.alice, p.carol)
    assert (duplicate.status_code, duplicate.json()["detail"]) == (
        409,
        "this environment already has a pending transfer",
    )


async def test_acceptance_is_single_use(people: People):
    p = people
    transfer = (await _propose(p, p.alice, p.bob)).json()["id"]
    assert (await _act(p.bob, transfer, "accept")).status_code == 200
    for action in ("accept", "decline"):
        replay = await _act(p.bob, transfer, action)
        assert (replay.status_code, replay.json()["detail"]) == (
            409,
            "this transfer is no longer pending",
        )
    # The new owner can hand it on; the previous owner cannot propose any more.
    assert (await _propose(p, p.alice, p.carol)).status_code == 403
    assert (await _propose(p, p.bob, p.alice)).status_code == 201


async def test_a_transfer_from_a_former_owner_cannot_be_accepted(people: People):
    p = people
    transfer = (await _propose(p, p.alice, p.bob)).json()["id"]
    # Ownership changed outside the workflow (as an operator might): the offer is stale.
    rows = EnvironmentCredential.environment_id == p.environment.id
    await p.session.execute(delete(EnvironmentCredential).where(rows))
    await p.session.execute(
        update(Environment)
        .where(Environment.id == p.environment.id)
        .values(owner_user_id=p.carol.user_id)
    )
    for party in (p.alice, p.bob):
        assert (await _call(party, "GET", "/transfers")).json() == []
    response = await _act(p.bob, transfer, "accept")
    assert (response.status_code, response.json()["detail"]) == (
        409,
        "this transfer no longer matches the environment's owner",
    )
    assert (await _environment(p)).owner_user_id == p.carol.user_id


async def test_an_environment_cannot_be_created_for_a_departed_member(people: People):
    p = people
    await p.session.execute(
        delete(OrganizationMembership).where(
            OrganizationMembership.organization_id == p.alice.organization_id,
            OrganizationMembership.user_id == p.bob.user_id,
        )
    )
    # The route checked membership just before it ended: the database still refuses.
    with pytest.raises(access.NoAccess):
        await environments.create_environment(
            p.session, EnvironmentCreate(name="late", type="local"),
            organization_id=p.alice.organization_id, owner_user_id=p.bob.user_id,
        )  # fmt: skip


async def test_writes_need_the_csrf_header(people: People):
    p = people
    transfer = (await _propose(p, p.alice, p.bob)).json()["id"]
    async with api_client(p.bob) as client:
        del client.headers["X-CSRF-Token"]
        assert (await client.post(f"/transfers/{transfer}/accept")).status_code == 403
        assert (await client.get("/transfers")).status_code == 200
    async with api_client(None) as anonymous:
        assert (await anonymous.get("/transfers")).status_code == 401
    assert (await _environment(p)).owner_user_id == p.alice.user_id


async def test_transfers_never_cross_organizations(people: People):
    p = people
    foreign = Environment(name="theirs", type=EnvironmentType.LOCAL, **p.erin.owns)
    p.session.add(foreign)
    await p.session.flush()
    # A member of another organization cannot be offered an environment, nor offer theirs.
    assert (await _propose(p, p.alice, p.erin)).status_code == 409
    response = await _call(p.erin, "POST", f"/environments/{foreign.id}/transfers",
                           {"to_user_id": str(p.alice.user_id)})  # fmt: skip
    assert response.status_code == 409
    response = await _call(p.alice, "POST", f"/environments/{foreign.id}/transfers",
                           {"to_user_id": str(p.bob.user_id)})  # fmt: skip
    assert response.status_code == 404
    # A transfer row pointing across organizations (never created by the API) is useless.
    stray = EnvironmentTransfer(environment_id=foreign.id, from_user_id=p.erin.user_id,
                                to_user_id=p.alice.user_id)  # fmt: skip
    p.session.add(stray)
    await p.session.flush()
    assert (await _act(p.alice, str(stray.id), "accept")).status_code == 404
    assert (await _call(p.alice, "GET", "/transfers")).json() == []
    assert (await p.session.get(Environment, foreign.id)).owner_user_id == p.erin.user_id


# --- membership guard ---------------------------------------------------------------------


def _member_url(p: People, member: Member) -> str:
    return f"/organizations/{p.alice.organization_id}/members/{member.user_id}"


async def test_owners_of_environments_cannot_leave_or_be_removed_until_they_transfer(
    people: People,
):
    p = people
    bobs = Environment(name="bobs", type=EnvironmentType.LOCAL, **p.bob.owns)
    p.session.add(bobs)
    await p.session.flush()
    guard = "this member owns environments here; transfer them before leaving or removal"

    for actor, target in ((p.alice, p.alice), (p.olga, p.alice), (p.bob, p.bob),
                          (p.carol, p.bob), (p.olga, p.bob)):  # fmt: skip
        response = await _call(actor, "DELETE", _member_url(p, target))
        assert (response.status_code, response.json()["detail"]) == (409, guard), (actor, target)

    # Role changes keep environment ownership, and never grant credential access.
    demoted = await _call(p.olga, "PATCH", _member_url(p, p.alice), {"role": "member"})
    assert demoted.status_code == 200
    promoted = await _call(p.olga, "PATCH", _member_url(p, p.bob), {"role": "admin"})
    assert promoted.status_code == 200
    assert (await _environment(p)).owner_user_id == p.alice.user_id
    url = f"/environments/{p.environment.id}/credentials/signoz"
    assert (await _call(p.bob, "DELETE", url)).status_code == 403
    assert (await _call(p.alice, "DELETE", url)).status_code == 204

    # The database refuses it too, whatever the code path.
    with pytest.raises(IntegrityError):
        async with p.session.begin_nested():
            await p.session.execute(
                delete(OrganizationMembership).where(
                    OrganizationMembership.organization_id == p.alice.organization_id,
                    OrganizationMembership.user_id == p.alice.user_id,
                )
            )

    transfer = (await _propose(p, p.alice, p.carol)).json()["id"]
    await _act(p.carol, transfer, "accept")
    assert (await _call(p.alice, "DELETE", _member_url(p, p.alice))).status_code == 204


async def test_last_owner_protection_still_applies(people: People):
    p = people
    other = await make_member(p.session, name="solo")
    response = await _call(other, "DELETE", f"/organizations/{other.organization_id}/members/"
                                            f"{other.user_id}")  # fmt: skip
    assert (response.status_code, response.json()["detail"]) == (
        409,
        "an organization needs at least one owner",
    )


async def test_removing_a_recipient_closes_their_pending_transfer(people: People):
    p = people
    transfer = (await _propose(p, p.alice, p.bob)).json()["id"]
    assert (await _call(p.carol, "DELETE", _member_url(p, p.bob))).status_code == 204

    row = await p.session.get(EnvironmentTransfer, uuid.UUID(transfer), populate_existing=True)
    assert row is not None and row.status == TransferStatus.CANCELLED
    assert [a.details_json["reason"] for a in await _audit(p) if a.action == "transfer.cancel"] == [
        "recipient_not_member"
    ]
    # The former member cannot accept, and the owner can propose again.
    assert (await _act(p.bob, transfer, "accept")).status_code == 404
    assert (await _propose(p, p.alice, p.carol)).status_code == 201


async def test_a_transfer_left_pending_for_a_departed_member_is_void(people: People):
    p = people
    transfer = (await _propose(p, p.alice, p.bob)).json()["id"]
    # As if the membership ended in a race that left the transfer pending.
    await p.session.execute(
        delete(OrganizationMembership).where(
            OrganizationMembership.organization_id == p.alice.organization_id,
            OrganizationMembership.user_id == p.bob.user_id,
        )
    )
    assert (await _call(p.alice, "GET", "/transfers")).json() == []
    assert (await _act(p.bob, transfer, "accept")).status_code == 404
    assert (await _propose(p, p.alice, p.carol)).status_code == 201
    row = await p.session.get(EnvironmentTransfer, uuid.UUID(transfer), populate_existing=True)
    assert row is not None and row.status == TransferStatus.CANCELLED
