"""Environment read endpoints (Phase 6D) and the planned credential contract."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    InvestigationAssignment,
    ToolCall,
    ToolCallStatus,
    ToolCategory,
)
from src.schemas.credentials import CredentialStatus, NebiusCredentialsInput, SigNozCredentialsInput
from tests.credential_fixtures import (
    KEY_2,
    allow_process_credentials,
    configure_credentials,
    credential_storage,  # noqa: F401
    store_credentials,
)
from tests.records import add_assignment
from tests.tenancy import Member

NOW = datetime(2026, 10, 21, 17, 46, tzinfo=UTC)
SCOPE = {"signoz": {"hosts": ["vm-a"], "services": ["demo"], "deployment_environment": None}}


def _env(tenant: Member, name: str | None = None, **fields) -> Environment:
    return Environment(
        name=name or f"env-{uuid.uuid4().hex[:8]}",
        type=EnvironmentType.LOCAL,
        **tenant.owns,
        **fields,
    )


async def _assignment(
    session: AsyncSession, environment: Environment, tenant: Member
) -> InvestigationAssignment:
    assignment = await add_assignment(session, environment=environment, tenant=tenant)
    await session.flush()
    return assignment


def _call(
    assignment: InvestigationAssignment,
    tool: str,
    meta: dict | None,
    minutes: int,
    status: ToolCallStatus = ToolCallStatus.SUCCEEDED,
) -> ToolCall:
    at = NOW + timedelta(minutes=minutes)
    return ToolCall(
        assignment=assignment, tool_name=tool, tool_category=ToolCategory.TELEMETRY,
        status=status, output_metadata_json=meta, started_at=at, completed_at=at,
    )  # fmt: skip


def _meta(identity: str, value: str, outcome: str | None, available: str | None) -> dict:
    return {
        "scope": {"identity": identity, "values": [value], "deployment_environment": None},
        "outcome": outcome,
        "telemetry_available": available,
        "time_range": {"start": "2026-10-21T17:16:00Z", "end": "2026-10-21T17:46:00Z"},
    }


@pytest.fixture(autouse=True)
def grants_permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Environments here that hold an operator grant run on the development credentials.
    allow_process_credentials(monkeypatch)


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch):
    """Configured providers with recognisable secrets, read from the process environment
    exactly as the backend reads them."""
    monkeypatch.setenv("SIGNOZ_BASE_URL", "http://signoz.internal:8080")
    monkeypatch.setenv("SIGNOZ_API_KEY", "signoz-secret-value-123")
    monkeypatch.setenv("NEBIUS_API_KEY", "nebius-secret-value-456")
    monkeypatch.setenv("NEBIUS_MAIN_MODEL", "nvidia/main")
    monkeypatch.setenv("NEBIUS_LOCAL_MODEL", "nvidia/local")


async def test_list_has_counts_and_no_configuration(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    env = _env(tenant, metadata_json=SCOPE)
    await _assignment(session, env, tenant)

    listed = (
        await client.get("/environments", params={"organization_id": str(tenant.organization_id)})
    ).json()

    mine = next(e for e in listed if e["id"] == str(env.id))
    assert mine["investigation_count"] == 1
    assert mine["last_investigation_at"] is not None
    assert set(mine) == {
        "id", "name", "type", "status", "created_at", "investigation_count",
        "last_investigation_at",
    }  # fmt: skip
    assert "vm-a" not in json.dumps(listed)


async def test_detail_reports_scope_stats_and_provider_status_without_secrets(
    client: AsyncClient, session: AsyncSession, tenant: Member, providers
):
    # An operator granted this environment the process-wide development credentials.
    env = _env(tenant, metadata_json=SCOPE, process_credentials=True)
    assignment = await _assignment(session, env, tenant)
    session.add(
        _call(assignment, "signoz.trace_stats", _meta("service", "demo", "observations", None), 0)
    )
    await session.flush()

    response = await client.get(f"/environments/{env.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == {
        "status": "configured", "hosts": ["vm-a"], "services": ["demo"],
        "deployment_environment": None, "problems": [],
    }  # fmt: skip
    assert body["stats"]["investigations"] == 1
    assert body["stats"]["tool_calls"] == 1
    assert body["telemetry_provider"] == {
        "provider": "signoz", "source": "process_environment", "configured": True, "issues": [],
    }  # fmt: skip
    assert body["ai_provider"]["configured"] is True
    assert body["ai_provider"]["main_model"] == "nvidia/main"
    assert body["per_environment_credentials"] == "not_available"
    assert body["credentials"] == "process_development"
    assert body["owner_user_id"] == str(tenant.user_id)
    for secret in ("signoz-secret-value-123", "nebius-secret-value-456", "signoz.internal"):
        assert secret not in response.text


async def test_tenant_environment_without_grant_is_configuration_required(
    client: AsyncClient, session: AsyncSession, tenant: Member, providers
):
    env = _env(tenant, metadata_json=SCOPE)
    session.add(env)
    await session.flush()

    body = (await client.get(f"/environments/{env.id}")).json()

    # The backend's own settings are configured, but they are not this environment's.
    assert body["credentials"] == "configuration_required"
    assert body["telemetry_provider"]["configured"] is False
    assert body["telemetry_provider"]["source"] == "environment"
    assert body["ai_provider"]["configured"] is False
    assert body["ai_provider"]["main_model"] is None


async def test_created_environment_is_owned_and_configuration_required(
    client: AsyncClient, tenant: Member
):
    response = await client.post(
        "/environments",
        json={"organization_id": str(tenant.organization_id), "name": "prod", "type": "aws"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["organization_id"] == str(tenant.organization_id)
    assert body["owner_user_id"] == str(tenant.user_id)
    detail = (await client.get(f"/environments/{body['id']}")).json()
    assert detail["credentials"] == "configuration_required"
    # Names are unique per organization.
    again = await client.post(
        "/environments",
        json={"organization_id": str(tenant.organization_id), "name": "prod", "type": "aws"},
    )
    assert again.status_code == 409


async def test_missing_provider_settings_are_named_not_echoed(
    client: AsyncClient, session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    # An https rule violation: the loader names the setting, never its value.
    monkeypatch.setenv("NEBIUS_API_KEY", "nebius-secret-value-456")
    monkeypatch.setenv("NEBIUS_BASE_URL", "http://insecure.example/v1")
    env = _env(tenant, metadata_json=SCOPE, process_credentials=True)
    session.add(env)
    await session.flush()

    body = (await client.get(f"/environments/{env.id}")).json()

    assert body["ai_provider"]["configured"] is False
    assert body["ai_provider"]["issues"] == ["NEBIUS_BASE_URL"]
    assert "insecure.example" not in json.dumps(body)


@pytest.mark.parametrize(
    ("metadata", "status", "code"),
    [
        ({}, "missing", "policy.scope_missing"),
        ({"signoz": {"hosts": "vm"}}, "invalid", "policy.scope_invalid"),
        ({"signoz": {"hosts": [], "services": []}}, "configured", "policy.scope_missing_identity"),
    ],
)
async def test_scope_problems_match_executor_rejections(
    client: AsyncClient, session: AsyncSession, tenant: Member, metadata, status, code
):
    env = _env(tenant, metadata_json=metadata)
    session.add(env)
    await session.flush()

    scope = (await client.get(f"/environments/{env.id}")).json()["scope"]

    assert scope["status"] == status
    assert [p["code"] for p in scope["problems"]] == [code]


async def test_identity_claimed_by_another_active_environment_is_reported(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    host = f"shared-{uuid.uuid4().hex[:6]}"
    granted = {"process_credentials": True}
    env = _env(tenant, metadata_json={"signoz": {"hosts": [host]}}, **granted)
    other = _env(tenant, metadata_json={"signoz": {"hosts": [host]}}, **granted)
    retired = _env(
        tenant,
        metadata_json={"signoz": {"services": ["svc-x"]}},
        status=EnvironmentStatus.DISABLED,
        **granted,
    )
    # Not sharing the development telemetry connection: claims nothing on it.
    ungranted = _env(tenant, metadata_json={"signoz": {"services": ["svc-x"]}})
    env.metadata_json["signoz"]["services"] = ["svc-x"]
    session.add_all([env, other, retired, ungranted])
    await session.flush()

    problems = (await client.get(f"/environments/{env.id}")).json()["scope"]["problems"]

    # The host overlap is ambiguous; a disabled environment claims nothing.
    assert [(p["code"], host in p["message"]) for p in problems] == [
        ("policy.scope_ambiguous", True)
    ]


async def test_telemetry_coverage_is_latest_recorded_result_per_signal(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    env = _env(tenant, metadata_json=SCOPE)
    assignment = await _assignment(session, env, tenant)
    other = await _assignment(
        session, _env(tenant, metadata_json={"signoz": {"hosts": ["vm-z"]}}), tenant
    )
    session.add_all(
        [
            _call(assignment, "signoz.query_metrics", _meta("host", "vm-a", "empty", "no"), 0),
            _call(
                assignment, "signoz.query_metrics", _meta("host", "vm-a", "observations", None), 5
            ),
            _call(assignment, "signoz.search_logs", _meta("service", "demo", "empty", "no"), 1),
            _call(
                assignment, "signoz.trace_stats", _meta("service", "demo", "empty", "unknown"), 2
            ),
            _call(assignment, "signoz.trace_stats", None, 9, ToolCallStatus.FAILED),
            _call(other, "signoz.search_logs", _meta("host", "vm-z", "observations", None), 3),
        ]
    )
    await session.flush()

    body = (await client.get(f"/environments/{env.id}/telemetry")).json()

    assert [(s["signal"], s["value"], s["status"]) for s in body["signals"]] == [
        ("logs", "demo", "no_data"),
        ("metrics", "vm-a", "observed"),  # the later observation wins
        ("traces", "demo", "unknown"),
    ]
    metrics = body["signals"][1]
    assert metrics["window_start"] == "2026-10-21T17:16:00Z"
    assert metrics["investigation_id"] == str(assignment.investigation_id)
    assert (body["queries_checked"], body["queries_failed"]) == (4, 1)


async def test_unknown_environment_is_404(client: AsyncClient):
    assert (await client.get(f"/environments/{uuid.uuid4()}")).status_code == 404
    assert (await client.get(f"/environments/{uuid.uuid4()}/telemetry")).status_code == 404


async def test_investigations_can_be_listed_per_environment(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    env = _env(tenant, metadata_json=SCOPE)
    mine = await _assignment(session, env, tenant)
    await _assignment(session, _env(tenant), tenant)

    body = (
        await client.get(
            "/investigations",
            params={"environment_id": str(env.id), "organization_id": str(tenant.organization_id)},
        )
    ).json()

    assert [i["id"] for i in body["items"]] == [str(mine.investigation_id)]


# --- credential contract (routes: tests/test_credentials_api.py) ----------------------------


def test_credential_inputs_never_serialize_secrets():
    nebius = NebiusCredentialsInput(
        api_key="nebius-secret-value-456", main_model="nvidia/main", local_model="nvidia/local"
    )
    signoz = SigNozCredentialsInput(base_url="http://localhost:8080", api_key="signoz-secret")
    for model in (nebius, signoz):
        assert "secret" not in repr(model)
        assert "secret" not in model.model_dump_json()
    assert CredentialStatus(
        kind="nebius", configured=True, state="configured", updated_at=None, updated_by=None
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"base_url": "http://api.tokenfactory.nebius.com/v1/"},
        {"base_url": "https://user:pass@api.example/v1/"},
        {"api_key": "short"},
        {"main_model": "bad model id"},
        {"unexpected": "x"},
    ],
)
def test_nebius_credential_validation(fields: dict):
    valid = {"api_key": "x" * 32, "main_model": "nvidia/main", "local_model": "nvidia/local"}
    with pytest.raises(ValidationError):
        NebiusCredentialsInput(**(valid | fields))


def test_signoz_credential_validation():
    with pytest.raises(ValidationError):
        SigNozCredentialsInput(base_url="ftp://signoz", api_key="x" * 16)
    with pytest.raises(ValidationError):
        SigNozCredentialsInput(base_url="https://a:b@signoz", api_key="x" * 16)


async def test_detail_reports_own_credentials_without_values(
    client: AsyncClient,
    session: AsyncSession,
    tenant: Member,
    credential_storage,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    env = _env(tenant, metadata_json=SCOPE, process_credentials=True)
    session.add(env)
    await session.flush()
    await store_credentials(session, tenant.user_id, env.id)

    response = await client.get(f"/environments/{env.id}")
    body = response.json()
    # Own credentials win over the grant (D034); model ids are not secrets.
    assert body["credentials"] == "environment"
    assert body["ai_provider"] == {
        "provider": "nebius_token_factory", "source": "environment", "configured": True,
        "issues": [], "main_model": "main-A", "local_model": "local-A",
    }  # fmt: skip
    assert body["telemetry_provider"]["configured"] is True
    assert "SENTINEL" not in response.text and "signoz.test" not in response.text

    configure_credentials(monkeypatch, keys={"k2": KEY_2}, active="k2")
    body = (await client.get(f"/environments/{env.id}")).json()
    assert body["credentials"] == "unavailable"
    assert body["ai_provider"]["issues"] == ["credentials_unavailable"]
    assert body["scope"]["problems"] == []  # an environment that queries nothing


async def test_grants_need_the_operator_opt_in_to_count(
    client: AsyncClient, session: AsyncSession, tenant: Member, monkeypatch: pytest.MonkeyPatch
):
    allow_process_credentials(monkeypatch, allowed=False)
    env = _env(tenant, metadata_json=SCOPE, process_credentials=True)
    session.add(env)
    await session.flush()
    body = (await client.get(f"/environments/{env.id}")).json()
    assert body["credentials"] == "configuration_required"
    assert body["telemetry_provider"]["issues"] == ["process_credentials_not_permitted"]
