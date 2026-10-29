"""The single credential resolver (7C-2, D034): which credentials a run may use, and every
fail-closed refusal. Rolled-back sessions; nothing contacts a provider."""

import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.credentials import resolver
from src.credentials.resolver import CredentialRefused, environment_credentials
from src.credentials.settings import load_credential_config
from src.integrations.signoz.settings import SigNozSettings
from src.models import (
    CredentialKind,
    Environment,
    EnvironmentCredential,
    EnvironmentType,
    Investigation,
    InvestigationEnvironment,
    TelemetryIsolation,
    TriggerType,
)
from src.services import adoption
from tests.credential_fixtures import (
    KEY_1,
    KEY_2,
    NEBIUS_SECRET,
    SIGNOZ_ORIGIN,
    SIGNOZ_SECRET,
    allow_process_credentials,
    bind_tenant_keys,
    configure_credentials,
    credential_storage,  # noqa: F401
    store_credentials,
)
from tests.tenancy import Member, make_member

pytestmark = pytest.mark.usefixtures("credential_storage")


def _env(owner: Member | None, **fields) -> Environment:
    return Environment(
        name=f"env-{uuid.uuid4().hex[:8]}",
        type=EnvironmentType.LOCAL,
        **(owner.owns if owner else {}),
        **fields,
    )


async def _added(session: AsyncSession, environment: Environment) -> Environment:
    session.add(environment)
    await session.flush()
    return environment


async def _refusal(session: AsyncSession, environment: Environment) -> str:
    with pytest.raises(CredentialRefused) as caught:
        await environment_credentials(session, environment)
    return caught.value.code


def production(monkeypatch: pytest.MonkeyPatch, app_env: str = "production") -> None:
    monkeypatch.setattr(resolver, "get_settings", lambda: SimpleNamespace(app_env=app_env))


# --- sources -------------------------------------------------------------------------------


async def test_own_credentials_are_used_exclusively_and_never_printed(
    session: AsyncSession, tenant: Member
):
    environment = await _added(session, _env(tenant, process_credentials=True))
    await store_credentials(session, tenant.user_id, environment.id)

    found = await environment_credentials(session, environment)

    assert found.source == "environment"  # the grant is ignored
    assert found.nebius == {
        "api_key": f"{NEBIUS_SECRET}-A",
        "base_url": "https://api.tokenfactory.nebius.com/v1/",
        "main_model": "main-A",
        "local_model": "local-A",
    }
    assert found.signoz == {"api_key": f"{SIGNOZ_SECRET}-A", "base_url": SIGNOZ_ORIGIN}
    assert len(found.fingerprint or "") == 64
    assert set(found.secrets()) == {f"{NEBIUS_SECRET}-A", f"{SIGNOZ_SECRET}-A"}
    assert "SENTINEL" not in repr(found) and SIGNOZ_ORIGIN not in repr(found)


async def test_one_missing_kind_is_never_filled_from_the_process_settings(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    allow_process_credentials(monkeypatch)
    for kind in CredentialKind:
        environment = await _added(session, _env(tenant, process_credentials=True))
        await store_credentials(session, tenant.user_id, environment.id, kinds=(kind,))
        assert await _refusal(session, environment) == "configuration_required"


async def test_nothing_configured_is_configuration_required(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    allow_process_credentials(monkeypatch)
    environment = await _added(session, _env(tenant))
    assert await _refusal(session, environment) == "configuration_required"


# Allowlisted local modes: any other APP_ENV is treated as hosted.
HOSTED = pytest.mark.parametrize("app_env", ["production", "staging", "hosted-demo"])


@HOSTED
async def test_process_grants_need_the_operator_opt_in_and_only_run_locally(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch, app_env: str
):
    environment = await _added(session, _env(tenant, process_credentials=True))
    assert await _refusal(session, environment) == "process_credentials_not_permitted"

    allow_process_credentials(monkeypatch)
    found = await environment_credentials(session, environment)
    assert (found.source, found.nebius, found.signoz) == ("process", {}, {})

    production(monkeypatch, app_env)
    assert await _refusal(session, environment) == "process_credentials_not_permitted"


@HOSTED
async def test_legacy_environments_stay_host_only_and_only_run_locally(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, app_env: str
):
    legacy = await _added(session, _env(None))
    # No opt-in needed: ownerless rows are reachable only by host-side tooling (D032).
    assert (await environment_credentials(session, legacy)).source == "process"
    production(monkeypatch, app_env)
    assert await _refusal(session, legacy) == "process_credentials_not_permitted"


# --- fail closed ---------------------------------------------------------------------------


async def test_unavailable_key_ring_and_unknown_keys_fail_closed(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    environment = await _added(session, _env(tenant, process_credentials=True))
    await store_credentials(session, tenant.user_id, environment.id)
    allow_process_credentials(monkeypatch)

    configure_credentials(monkeypatch, keys={"k1": "not-base64"})
    assert await _refusal(session, environment) == "credentials_unavailable"
    # The key that sealed them is gone from the ring.
    configure_credentials(monkeypatch, keys={"k2": KEY_2}, active="k2")
    assert await _refusal(session, environment) == "credentials_unavailable"
    configure_credentials(monkeypatch, keys={"k1": KEY_1})
    assert (await environment_credentials(session, environment)).source == "environment"


async def test_tampered_or_copied_ciphertext_fails_closed(session: AsyncSession, tenant: Member):
    first = await _added(session, _env(tenant))
    second = await _added(session, _env(tenant))
    await store_credentials(session, tenant.user_id, first.id, tag="A")
    await store_credentials(session, tenant.user_id, second.id, tag="B")

    # Another environment's sealed key, copied over: bound to the other environment.
    source = await session.get(EnvironmentCredential, (first.id, CredentialKind.SIGNOZ))
    await session.execute(
        update(EnvironmentCredential)
        .where(
            EnvironmentCredential.environment_id == second.id,
            EnvironmentCredential.kind == CredentialKind.SIGNOZ,
        )
        .values(nonce=source.nonce, ciphertext=source.ciphertext, key_id=source.key_id)
    )
    assert await _refusal(session, second) == "credentials_unavailable"

    row = await session.get(EnvironmentCredential, (first.id, CredentialKind.NEBIUS))
    row.ciphertext = bytes([row.ciphertext[0] ^ 1]) + row.ciphertext[1:]
    await session.flush()
    assert await _refusal(session, first) == "credentials_unavailable"


async def test_destinations_are_rechecked_against_the_current_policy(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    environment = await _added(session, _env(tenant))
    await store_credentials(session, tenant.user_id, environment.id)
    assert (await environment_credentials(session, environment)).source == "environment"

    # The operator narrowed the allowlists after the credentials were saved.
    configure_credentials(monkeypatch, telemetry=[])
    assert await _refusal(session, environment) == "destination_not_allowed"
    configure_credentials(monkeypatch, nebius=["https://other.example"])
    assert await _refusal(session, environment) == "destination_not_allowed"

    # A public https telemetry endpoint needs no allowlist entry (addresses are vetted
    # when connecting).
    configure_credentials(monkeypatch, telemetry=[])
    public = await _added(session, _env(tenant))
    await store_credentials(
        session, tenant.user_id, public.id, signoz_url="https://signoz.example.com"
    )
    assert (await environment_credentials(session, public)).source == "environment"


# --- investigations ------------------------------------------------------------------------


async def _investigation(session: AsyncSession, *environments: Environment) -> uuid.UUID:
    investigation = Investigation(
        title="t",
        question="q",
        trigger_type=TriggerType.MANUAL,
        organization_id=environments[0].organization_id,
    )
    session.add(investigation)
    await session.flush()
    for environment in environments:
        session.add(InvestigationEnvironment(investigation=investigation, environment=environment))
    await session.flush()
    return investigation.id


async def test_main_resolves_through_its_single_environment(session: AsyncSession, tenant: Member):
    environment = await _added(session, _env(tenant))
    await store_credentials(session, tenant.user_id, environment.id)
    found = await resolver.investigation_credentials(
        session, await _investigation(session, environment)
    )
    assert (found.environment_id, found.nebius["main_model"]) == (environment.id, "main-A")


async def test_no_credential_owner_is_chosen_across_environments(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    colleague = await make_member(session, organization_id=tenant.organization_id)
    mine = await _added(session, _env(tenant))
    theirs = await _added(session, _env(colleague))
    await store_credentials(session, tenant.user_id, mine.id, tag="A")
    await store_credentials(session, colleague.user_id, theirs.id, tag="B")
    both = await _investigation(session, mine, theirs)
    assert await resolver.investigation_refusal(session, both) == "multi_environment_unsupported"

    # Several environments on the one process-wide connection choose nobody's credentials.
    allow_process_credentials(monkeypatch)
    granted = [await _added(session, _env(tenant, process_credentials=True)) for _ in range(2)]
    assert await resolver.investigation_refusal(
        session, await _investigation(session, *granted)
    ) is None  # fmt: skip
    legacy = [await _added(session, _env(None)) for _ in range(2)]
    found = await resolver.investigation_credentials(
        session, await _investigation(session, *legacy)
    )
    assert found.source == "process"


async def test_an_investigation_without_environments_is_refused(session: AsyncSession):
    investigation = Investigation(title="t", question="q", trigger_type=TriggerType.MANUAL)
    session.add(investigation)
    await session.flush()
    assert (
        await resolver.investigation_refusal(session, investigation.id) == "configuration_required"
    )


# --- shared telemetry ----------------------------------------------------------------------


async def test_ambiguity_peers_are_environments_of_one_organization_on_one_connection(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    other_org = await make_member(session, name="other")
    a = await _added(session, _env(tenant))
    twin = await _added(session, _env(tenant))
    foreign = await _added(session, _env(other_org))
    elsewhere = await _added(session, _env(tenant))
    legacy = await _added(session, _env(None))
    # Two organizations on the same backend: possible only provider-scoped (D037, D038).
    await bind_tenant_keys(session, tenant.organization_id, SIGNOZ_ORIGIN, "key-a")
    for environment in (a, twin):
        await store_credentials(
            session, tenant.user_id, environment.id, isolation=TelemetryIsolation.PROVIDER_SCOPED
        )
    await bind_tenant_keys(session, other_org.organization_id, SIGNOZ_ORIGIN, "key-b")
    await store_credentials(
        session, other_org.user_id, foreign.id, tag="B",
        isolation=TelemetryIsolation.PROVIDER_SCOPED,
    )  # fmt: skip
    configure_credentials(monkeypatch, telemetry=[SIGNOZ_ORIGIN, "http://elsewhere.test:8080"])
    await store_credentials(
        session, tenant.user_id, elsewhere.id, signoz_url="http://elsewhere.test:8080"
    )
    monkeypatch.setattr(
        resolver,
        "load_signoz_settings",
        lambda: SigNozSettings(base_url="http://process.test:8080", api_key="x" * 10),
    )
    ids = {a.id, twin.id, foreign.id, elsewhere.id, legacy.id}

    async def peers(environment: Environment) -> set[uuid.UUID]:
        credentials = await environment_credentials(session, environment)
        found = await session.scalars(
            select(Environment.id).where(resolver.shares_telemetry(credentials))
        )
        return set(found) & ids

    # Same organization and backend only: another organization never counts.
    assert await peers(a) == {a.id, twin.id}
    assert await peers(foreign) == {foreign.id}
    assert await peers(legacy) == {legacy.id}
    # The process-wide connection reaches `a`'s backend, but legacy records have no
    # organization: still not peers of an organization's environments.
    monkeypatch.setattr(
        resolver,
        "load_signoz_settings",
        lambda: SigNozSettings(base_url=SIGNOZ_ORIGIN + "/", api_key="x" * 10),
    )
    assert await peers(a) == {a.id, twin.id}
    assert await peers(legacy) == {legacy.id}


# --- startup and grants --------------------------------------------------------------------


async def test_startup_report_names_settings_never_values(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    environment = await _added(session, _env(tenant))
    await store_credentials(session, tenant.user_id, environment.id)
    await _added(session, _env(tenant, process_credentials=True))

    configure_credentials(monkeypatch, keys={"k2": KEY_2}, active="k2")
    problems = await resolver.startup_problems(session)
    assert any("missing from INQUEST_CREDENTIAL_KEYS" in p for p in problems)
    assert any("INQUEST_ALLOW_PROCESS_CREDENTIALS is off" in p for p in problems)

    monkeypatch.setenv("INQUEST_CREDENTIAL_KEYS", "{broken")
    load_credential_config.cache_clear()
    production(monkeypatch)
    problems = await resolver.startup_problems(session)
    assert any("credential storage is not configured" in p for p in problems)
    assert any("refused outside local development (APP_ENV)" in p for p in problems)
    text = json.dumps(problems)
    assert KEY_1 not in text and KEY_2 not in text and "broken" not in text


async def test_startup_report_is_logged(
    session: AsyncSession,
    tenant: Member,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    from src.investigators import runtime

    async def problems(_):
        return ["2 stored credentials use encryption keys missing from INQUEST_CREDENTIAL_KEYS"]

    monkeypatch.setattr(resolver, "startup_problems", problems)
    with caplog.at_level(logging.WARNING, logger="src.investigators.runtime"):
        await runtime.log_credential_readiness()
    assert "credentials: 2 stored credentials use encryption keys missing" in caplog.text


@HOSTED
async def test_adoption_never_records_a_grant_outside_local_development(
    session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch, app_env: str
):
    legacy = await _added(session, _env(None))
    production(monkeypatch, app_env)
    plan = await adoption.plan_adoption(
        session,
        environment_ids=[legacy.id],
        organization_id=tenant.organization_id,
        owner_user_id=tenant.user_id,
        process_credentials=True,
    )
    assert "process-wide credential grants are permitted only in local development" in plan.problems
    without = await adoption.plan_adoption(
        session,
        environment_ids=[legacy.id],
        organization_id=tenant.organization_id,
        owner_user_id=tenant.user_id,
        process_credentials=False,
    )
    assert without.problems == []
