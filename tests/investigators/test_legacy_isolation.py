"""Phase 7D-2B closeout (D038): SigNoz connections that fail the isolation policy are
refused wherever credentials are resolved: starting a turn, resuming a checkpoint,
recovery, every tool call and idempotent replay. Cross-organization reuse saved before D037
is simulated by storing without the admission check. A real loopback SigNoz reached as
127.0.0.1 and as localhost stands in for two DNS names of one backend. Committed rows,
deleted afterwards; models are scripted, no provider is called."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from src.credentials import resolver
from src.credentials.settings import load_credential_config
from src.db.session import SessionLocal
from src.investigators import runner
from src.models import (
    CredentialKind,
    Environment,
    EnvironmentCredential,
    Evidence,
    Investigation,
    InvestigationTurn,
    ToolCallStatus,
    TurnStatus,
)
from src.services import telemetry_keys
from tests.credential_fixtures import (
    configure_credentials,
    signoz_server,
    store_before_d037,
    store_committed,
)
from tests.investigators.conftest import MCP_SETTINGS, ScriptedModel
from tests.investigators.test_main_graph import ANSWER, Crash, investigate, local_script
from tests.investigators.test_runtime_credentials import (  # noqa: F401
    Provider,
    Tenant,
    _call,
    exec_tenants,
    happy,
    nebius_key,
    owners,
    run,
    signoz_key,
    tenants,
)
from tests.mcp_server.conftest import mcp_app  # noqa: F401
from tests.tenancy import Member, api_client
from tests.tools.conftest import make_world, trace_stats_args  # noqa: F401

REFUSED = "telemetry_isolation_unverified"


async def reuse(member: Member, environment_id, origin: str, tag: str) -> None:
    """The other organization's SigNoz connection, saved before D037."""
    async with SessionLocal() as s:
        await store_before_d037(s, member.user_id, environment_id, tag=tag, signoz_url=origin,
                                kinds=(CredentialKind.SIGNOZ,))  # fmt: skip


async def verify(member: Member, origin: str) -> None:
    actor = "operator:test (legacy isolation)"
    async with SessionLocal() as s:
        plan = await telemetry_keys.verify_dedicated(
            s, load_credential_config(), member.organization_id, origin, actor=actor, apply=True
        )
    # Its audit event names the organization: the owners fixture removes it.
    assert plan.applied


async def start(t: Tenant) -> tuple[int, str]:
    async with api_client(t.member) as client:
        response = await client.post(
            f"/investigations/{t.world.investigation_id}/turns", json={"question": "again?"}
        )
    return response.status_code, response.text


async def test_legacy_reuse_is_refused_at_start_resume_and_recovery(
    tenants: list[Tenant],  # noqa: F811
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a, b = tenants
    # B's turn is interrupted after one telemetry query; its checkpoint remains.
    first = Provider(
        monkeypatch,
        {
            ("main", nebius_key("B")): ScriptedModel(
                structured=[investigate(b.world), ANSWER, Crash()]
            ),
            ("local", nebius_key("B")): local_script(b.world.service),
        },
    )
    with pytest.raises(Crash):
        await run(b.world, checkpointer, mcp_app)
    assert len(b.seen) == 1 and nebius_key("B") in first.keys("local")

    # B's connection now points at A's SigNoz, as saved before D037.
    await reuse(b.member, b.world.environment_id, a.origin, "B")
    fake = Provider(monkeypatch, {})

    # Resuming the checkpoint is refused before any model step or query.
    result = await run(b.world, checkpointer, mcp_app)
    assert result.status == TurnStatus.FAILED and result.error_code == REFUSED
    assert fake.built == [] and len(b.seen) == 1 and a.seen == []
    # Neither organization can start: neither connection is verified as its own. The
    # refusal names neither the other organization nor the connection.
    for t, other in ((a, b), (b, a)):
        status, text = await start(t)
        assert status == 409 and "telemetry isolation unverified" in text
        for private in (a.origin, str(other.member.organization_id), str(other.world.turn_id)):
            assert private not in text

    # Recovery of an accepted turn is refused the same way, without a model call.
    async with SessionLocal() as s, s.begin():
        await s.execute(
            update(InvestigationTurn)
            .where(InvestigationTurn.id == a.world.turn_id)
            .values(run_requested_at=InvestigationTurn.created_at)
        )
    await runner.recover(checkpointer, resume=True, mcp_settings=MCP_SETTINGS)
    async with SessionLocal() as s:
        turn = await s.get(InvestigationTurn, a.world.turn_id)
    assert turn.error_code == REFUSED and fake.built == [] and a.seen == []

    # History is kept: B's evidence and both credential rows are still there.
    async with SessionLocal() as s:
        evidence = list(
            await s.scalars(
                select(Evidence).where(Evidence.investigation_id == b.world.investigation_id)
            )
        )
        rows = list(
            await s.scalars(
                select(EnvironmentCredential.environment_id).where(
                    EnvironmentCredential.kind == CredentialKind.SIGNOZ,
                    EnvironmentCredential.environment_id.in_(
                        [a.world.environment_id, b.world.environment_id]
                    ),
                )
            )
        )
    assert len(evidence) == 1 and len(rows) == 2


async def test_the_verified_organization_runs_while_the_reuse_stays_refused(
    tenants: list[Tenant],  # noqa: F811
    checkpointer,
    mcp_app,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    a, b = tenants
    await reuse(b.member, b.world.environment_id, a.origin, "B")
    await verify(a.member, a.origin)
    fake = Provider(monkeypatch, happy(a))

    result = await run(a.world, checkpointer, mcp_app)

    assert result.status == TurnStatus.COMPLETED
    assert {r["headers"]["signoz-api-key"] for r in a.seen} == {signoz_key("A")}
    assert nebius_key("B") not in fake.keys("main") + fake.keys("local")
    refused = await run(b.world, checkpointer, mcp_app)
    assert refused.error_code == REFUSED and b.seen == []


async def test_tool_calls_and_replays_stop_on_legacy_reuse(exec_tenants):  # noqa: F811
    (a, owner_a, origin_a, seen_a), (b, owner_b, _, seen_b) = exec_tenants
    args = trace_stats_args(service=a.service)
    call, _ = _call(a, owner_a, "same", args)
    assert (await call).status is ToolCallStatus.SUCCEEDED

    # Another organization's connection to A's SigNoz, saved before D037.
    await reuse(owner_b, b.environment_id, origin_a, "B")
    for world, owner, key, arguments in (
        (a, owner_a, "same", args),  # an idempotent replay serves nothing either
        (a, owner_a, None, args),
        (b, owner_b, None, trace_stats_args(service=b.service)),
    ):
        call, _ = _call(world, owner, key, arguments)
        result = await call
        assert result.status is ToolCallStatus.REJECTED and not result.replayed
        assert result.error.code == f"policy.{REFUSED}"
        assert origin_a not in json.dumps(result.model_dump(mode="json"))
    assert len(seen_a) == 1 and seen_b == []
    assert len(await a.evidence()) == 1  # the earlier result is kept, not served

    # Once the operator verifies the connection as A's, A continues (the replay too).
    await verify(owner_a, origin_a)
    call, _ = _call(a, owner_a, "same", args)
    assert (await call).replayed
    call, _ = _call(b, owner_b, None, trace_stats_args(service=b.service))
    assert (await call).error.code == f"policy.{REFUSED}"
    assert len(seen_a) == 1


async def test_two_names_for_one_backend_need_verification_when_hosted(
    owners: list[Member],  # noqa: F811
    make_world,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    async with signoz_server("trace_stats_scalar.json") as (origin, seen):
        alias = origin.replace("127.0.0.1", "localhost")
        configure_credentials(monkeypatch, telemetry=[origin, alias])
        worlds = []
        for owner, url, tag in ((owners[0], origin, "A"), (owners[1], alias, "B")):
            world = await make_world()
            async with SessionLocal() as s, s.begin():
                await s.execute(
                    update(Environment)
                    .where(Environment.id == world.environment_id)
                    .values(**owner.owns)
                )
                await s.execute(
                    update(Investigation)
                    .where(Investigation.id == world.investigation_id)
                    .values(organization_id=owner.organization_id)
                )
            # Locally both are accepted: different fingerprints, one backend (D038).
            await store_committed(owner.user_id, world.environment_id, tag=tag, signoz_url=url)
            worlds.append((world, owner))
        (a, owner_a), (b, owner_b) = worlds
        for world, owner in worlds:
            call, _ = _call(world, owner)
            assert (await call).status is ToolCallStatus.SUCCEEDED
        assert [r["headers"]["signoz-api-key"] for r in seen] == [signoz_key("A"), signoz_key("B")]

        # Hosted: both origins granted to both (network policy), so isolation decides.
        monkeypatch.setattr(resolver, "get_settings", lambda: SimpleNamespace(app_env="production"))
        members = [str(owner_a.organization_id), str(owner_b.organization_id)]
        monkeypatch.setenv(
            "INQUEST_TELEMETRY_ORIGIN_ORGANIZATIONS", json.dumps({origin: members, alias: members})
        )
        load_credential_config.cache_clear()
        for world, owner in worlds:
            call, _ = _call(world, owner)
            assert (await call).error.code == f"policy.{REFUSED}"
        await verify(owner_a, origin)
        call, _ = _call(a, owner_a)
        assert (await call).status is ToolCallStatus.SUCCEEDED
        call, _ = _call(b, owner_b)
        assert (await call).error.code == f"policy.{REFUSED}"
        assert len(seen) == 3
        async with SessionLocal() as s:
            reviewed = {r.environment_id: r.problem for r in await resolver.isolation_review(s)}
        assert reviewed[a.environment_id] is None
        assert reviewed[b.environment_id] == "dedication_unverified"
