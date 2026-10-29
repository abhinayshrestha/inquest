"""Per-environment credential API (Phase 7C-1, D034): owner-only writes, member-visible
statuses, validation before storage, no read-back, nothing secret in responses, errors,
logs, audit rows or plaintext columns. Providers are mock transports only."""

import json
import logging
import uuid
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.routes.credentials import provider_client
from src.credentials import crypto, network
from src.credentials.settings import load_credential_config
from src.db.session import get_session
from src.main import app
from src.models import (
    AuditEvent,
    CredentialKind,
    Environment,
    EnvironmentCredential,
    EnvironmentType,
    OrganizationRole,
)
from tests.credential_fixtures import (
    KEY_2,
    NEBIUS_SECRET,
    NEBIUS_URL,
    SIGNOZ_ORIGIN,
    SIGNOZ_SECRET,
    configure_credentials,
    credential_storage,  # noqa: F401  (fixture)
)
from tests.tenancy import Member, api_client, make_member

pytestmark = pytest.mark.usefixtures("credential_storage")

NEBIUS_BODY = {
    "api_key": NEBIUS_SECRET,
    "base_url": NEBIUS_URL,
    "main_model": "nvidia/main",
    "local_model": "nvidia/local",
}
SIGNOZ_BODY = {"base_url": SIGNOZ_ORIGIN, "api_key": SIGNOZ_SECRET}
MODELS = {"data": [{"id": "nvidia/main"}, {"id": "nvidia/local"}, {"id": "other"}]}
QUERY_OK = {"status": "success", "data": {"type": "scalar", "data": {"results": []}}}


class Provider:
    """Mock provider: records every request and answers with `respond`."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self.respond = respond or self.ok

    @staticmethod
    def ok(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        return httpx.Response(200, json=QUERY_OK)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


@pytest.fixture
def provider() -> Provider:
    mock = Provider()
    app.dependency_overrides[provider_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(mock), follow_redirects=False
    )
    return mock


async def _environment(session: AsyncSession, owner: Member) -> Environment:
    environment = Environment(
        name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL, **owner.owns
    )
    session.add(environment)
    await session.flush()
    return environment


def _url(environment: Environment, kind: str | None = None) -> str:
    return f"/environments/{environment.id}/credentials" + (f"/{kind}" if kind else "")


async def _row(session: AsyncSession, environment: Environment, kind: str):
    return await session.get(
        EnvironmentCredential, (environment.id, CredentialKind(kind)), populate_existing=True
    )


async def _plaintext_dump(session: AsyncSession) -> str:
    """Every credential and audit row rendered as text (bytea as hex)."""
    rows = await session.execute(
        text(
            "SELECT coalesce(string_agg(t::text, ' '), '') FROM environment_credentials t "
            "UNION ALL SELECT coalesce(string_agg(a::text, ' '), '') FROM audit_events a"
        )
    )
    return " ".join(r[0] for r in rows)


def _audits(member: Member):
    return select(AuditEvent).where(AuditEvent.actor_user_id == member.user_id)


def _assert_no_secret(*texts: str) -> None:
    for value in texts:
        for secret in (NEBIUS_SECRET, SIGNOZ_SECRET):
            assert secret not in value
            assert secret.encode().hex() not in value


# --- owner writes ----------------------------------------------------------------------------


async def test_owner_sets_nebius_after_validation_encrypted_and_never_read_back(
    client, session, tenant, provider, caplog
):
    caplog.set_level(logging.DEBUG)
    environment = await _environment(session, tenant)

    response = await client.put(_url(environment, "nebius"), json=NEBIUS_BODY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "kind": "nebius", "configured": True, "state": "configured",
        "updated_at": body["updated_at"], "updated_by": str(tenant.user_id),
        "main_model": "nvidia/main", "local_model": "nvidia/local", "isolation": None,
    }  # fmt: skip
    # Validated with exactly one authenticated catalog read.
    [request] = provider.requests
    assert (request.method, str(request.url)) == ("GET", NEBIUS_URL + "models")
    assert request.headers["authorization"] == f"Bearer {NEBIUS_SECRET}"

    row = await _row(session, environment, "nebius")
    assert row.key_id == "k1" and row.owner_user_id == tenant.user_id
    assert NEBIUS_SECRET.encode() not in row.ciphertext
    assert b"tokenfactory" not in row.ciphertext
    assert row.public_json == {"main_model": "nvidia/main", "local_model": "nvidia/local"}
    payload = crypto.unseal(
        load_credential_config(),
        crypto.Sealed(row.key_id, row.nonce, row.ciphertext),
        environment_id=environment.id, kind="nebius", owner_user_id=tenant.user_id,
    )  # fmt: skip
    assert payload == {"api_key": NEBIUS_SECRET, "base_url": NEBIUS_URL}

    statuses = await client.get(_url(environment))
    assert statuses.status_code == 200
    assert [s["kind"] for s in statuses.json()] == ["nebius", "signoz"]
    assert statuses.json()[1]["state"] == "not_configured"
    audit = (await session.scalars(_audits(tenant))).all()
    assert [(a.action, a.details_json) for a in audit] == [
        ("credentials.set", {"environment_id": str(environment.id), "kind": "nebius"})
    ]
    _assert_no_secret(response.text, statuses.text, caplog.text, await _plaintext_dump(session))
    assert "tokenfactory" not in statuses.text


async def test_owner_sets_signoz_with_fingerprint(client, session, tenant, provider):
    environment = await _environment(session, tenant)

    response = await client.put(_url(environment, "signoz"), json=SIGNOZ_BODY)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "configured" and response.json()["main_model"] is None
    [request] = provider.requests
    assert (request.method, request.url.path) == ("POST", "/api/v5/query_range")
    assert request.headers["signoz-api-key"] == SIGNOZ_SECRET
    row = await _row(session, environment, "signoz")
    assert row.connection_fingerprint == crypto.fingerprint(load_credential_config(), SIGNOZ_ORIGIN)
    _assert_no_secret(response.text, await _plaintext_dump(session))
    assert "signoz.test" not in response.text and "signoz.test" not in await _plaintext_dump(
        session
    )


async def test_rotation_replaces_and_failed_validation_preserves_working_credentials(
    client, session, tenant, provider
):
    environment = await _environment(session, tenant)
    assert (await client.put(_url(environment, "nebius"), json=NEBIUS_BODY)).status_code == 200
    before = await _row(session, environment, "nebius")
    kept = (before.ciphertext, before.nonce, before.updated_at)

    provider.respond = lambda r: httpx.Response(401, json={"error": f"bad key {NEBIUS_SECRET}x"})
    rejected = await client.put(
        _url(environment, "nebius"), json=NEBIUS_BODY | {"api_key": NEBIUS_SECRET + "x"}
    )
    assert rejected.status_code == 422
    assert rejected.json() == {
        "detail": {"code": "auth_failed", "message": "the provider rejected these credentials"}
    }
    after = await _row(session, environment, "nebius")
    assert (after.ciphertext, after.nonce, after.updated_at) == kept

    provider.respond = Provider.ok
    rotated = await client.put(
        _url(environment, "nebius"), json=NEBIUS_BODY | {"api_key": NEBIUS_SECRET + "2"}
    )
    assert rotated.status_code == 200
    assert (await _row(session, environment, "nebius")).ciphertext != kept[0]
    actions = [a.action for a in await session.scalars(_audits(tenant))]
    assert sorted(actions) == ["credentials.replace", "credentials.set"]
    _assert_no_secret(rejected.text, rotated.text, await _plaintext_dump(session))


async def test_owner_clears_credentials(client, session, tenant, provider):
    environment = await _environment(session, tenant)
    await client.put(_url(environment, "signoz"), json=SIGNOZ_BODY)

    assert (await client.delete(_url(environment, "signoz"))).status_code == 204
    assert await _row(session, environment, "signoz") is None
    # Idempotent; only real changes are audited.
    assert (await client.delete(_url(environment, "signoz"))).status_code == 204
    actions = [a.action for a in await session.scalars(_audits(tenant))]
    assert sorted(actions) == ["credentials.clear", "credentials.set"]
    assert (await client.delete(_url(environment, "aws"))).status_code == 422


# --- who may write ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role", [OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER]
)
async def test_only_the_environment_owner_writes_whatever_their_role(
    session, tenant, provider, role
):
    owner = await make_member(session, organization_id=tenant.organization_id, name="envowner",
                              role=OrganizationRole.MEMBER)  # fmt: skip
    other = await make_member(session, organization_id=tenant.organization_id, role=role)
    environment = await _environment(session, owner)
    app.dependency_overrides[get_session] = lambda: session
    try:
        async with api_client(other) as c:
            responses = [
                await c.put(_url(environment, "nebius"), json=NEBIUS_BODY),
                await c.put(_url(environment, "signoz"), json=SIGNOZ_BODY),
                await c.delete(_url(environment, "nebius")),
            ]
            statuses = await c.get(_url(environment))
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert [r.status_code for r in responses] == [403, 403, 403]
    # A non-owner never makes the server contact a provider.
    assert provider.requests == []
    assert await _row(session, environment, "nebius") is None
    # Members may read configuration status.
    assert statuses.status_code == 200 and len(statuses.json()) == 2


async def test_other_organizations_and_legacy_environments_do_not_exist(
    client, session, tenant, provider
):
    stranger = await make_member(session, name="stranger")
    foreign = await _environment(session, stranger)
    legacy = Environment(name=f"legacy-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL)
    session.add(legacy)
    await session.flush()
    for environment in (foreign, legacy):
        assert (await client.get(_url(environment))).status_code == 404
        assert (await client.put(_url(environment, "nebius"), json=NEBIUS_BODY)).status_code == 404
        assert (await client.delete(_url(environment, "nebius"))).status_code == 404
    assert (await client.get(_url(Environment(id=uuid.uuid4())))).status_code == 404
    assert provider.requests == []


async def test_writes_need_csrf_and_a_session(session, tenant, provider):
    environment = await _environment(session, tenant)
    app.dependency_overrides[get_session] = lambda: session
    try:
        async with api_client(tenant) as c:
            del c.headers["X-CSRF-Token"]
            no_csrf = await c.put(_url(environment, "nebius"), json=NEBIUS_BODY)
        async with api_client(None) as c:
            anonymous = await c.put(_url(environment, "nebius"), json=NEBIUS_BODY)
            anonymous_read = await c.get(_url(environment))
    finally:
        app.dependency_overrides.pop(get_session, None)
    assert (no_csrf.status_code, anonymous.status_code, anonymous_read.status_code) == (
        403, 401, 401,
    )  # fmt: skip
    assert provider.requests == []


async def test_ownership_cannot_change_while_credentials_exist(client, session, tenant, provider):
    environment = await _environment(session, tenant)
    await client.put(_url(environment, "nebius"), json=NEBIUS_BODY)
    other = await make_member(session, organization_id=tenant.organization_id)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                text("UPDATE environments SET owner_user_id = :u WHERE id = :e"),
                {"u": other.user_id, "e": environment.id},
            )
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(
                EnvironmentCredential(
                    environment_id=environment.id,
                    kind=CredentialKind.SIGNOZ,
                    owner_user_id=other.user_id,
                    key_id="k1",
                    nonce=b"0" * 12,
                    ciphertext=b"x",
                    connection_fingerprint="f" * 64,
                    updated_by_user_id=other.user_id,
                )  # fmt: skip
            )


# --- fail-closed storage ---------------------------------------------------------------------


async def test_unconfigured_storage_refuses_writes_and_reports_unavailable(
    client, session, tenant, provider, monkeypatch
):
    environment = await _environment(session, tenant)
    assert (await client.put(_url(environment, "nebius"), json=NEBIUS_BODY)).status_code == 200

    configure_credentials(monkeypatch, keys={})
    refused = await client.put(_url(environment, "signoz"), json=SIGNOZ_BODY)
    assert refused.status_code == 503
    assert "INQUEST" not in refused.text
    nebius = (await client.get(_url(environment))).json()[0]
    assert (nebius["state"], nebius["configured"]) == ("unavailable", False)

    # The stored key id is no longer in the ring: unavailable, not silently re-keyed.
    configure_credentials(monkeypatch, keys={"k2": KEY_2}, active="k2")
    nebius = (await client.get(_url(environment))).json()[0]
    assert (nebius["state"], nebius["configured"]) == ("unavailable", False)

    # Unusable credentials can still be removed.
    configure_credentials(monkeypatch, keys={})
    assert (await client.delete(_url(environment, "nebius"))).status_code == 204
    assert len(provider.requests) == 1


# --- secret-safe errors ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(NEBIUS_BODY | {"api_key": NEBIUS_SECRET[:12]}),  # too short
        json.dumps(NEBIUS_BODY | {"api_key": NEBIUS_SECRET * 30}),  # too long
        json.dumps(NEBIUS_BODY | {"apiKey": NEBIUS_SECRET}),  # unexpected field
        json.dumps(NEBIUS_BODY | {NEBIUS_SECRET: 1}),  # secret pasted as a key
        json.dumps(NEBIUS_BODY | {"api_key": {"value": NEBIUS_SECRET}}),  # wrong type
        json.dumps(NEBIUS_BODY | {"base_url": f"https://x:{NEBIUS_SECRET}@h/v1/"}),
        json.dumps(NEBIUS_BODY | {"main_model": NEBIUS_SECRET + " x"}),
        '{"api_key": "' + NEBIUS_SECRET + '", ',  # malformed JSON
    ],
)
async def test_request_validation_errors_never_echo_values(client, session, tenant, provider, body):
    environment = await _environment(session, tenant)
    response = await client.put(
        _url(environment, "nebius"), content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert all(set(e) == {"type", "loc", "msg"} for e in response.json()["detail"])
    _assert_no_secret(response.text)
    assert NEBIUS_SECRET[:12] not in response.text
    assert provider.requests == []


def _status(code: int, body: object = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda r: httpx.Response(code, json=body if body is not None else {})


def _raise(r: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"cannot reach {r.url} with {NEBIUS_SECRET}")


@pytest.mark.parametrize(
    "respond, code",
    [
        (_status(401, {"detail": f"invalid key {NEBIUS_SECRET}"}), "auth_failed"),
        (_status(403), "auth_failed"),
        (_status(429), "unreachable"),
        (_status(503, {"detail": NEBIUS_SECRET}), "unreachable"),
        (_status(400, {"detail": f"echo {NEBIUS_SECRET}"}), "provider_error"),
        (
            lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/"}),
            "provider_error",
        ),  # fmt: skip
        (_status(200, {"data": [{"id": "nvidia/main"}]}), "model_not_found"),
        (lambda r: httpx.Response(200, text="<html>SigNoz</html>"), "provider_error"),
        (lambda r: httpx.Response(200, content=b"x" * 2_000_001), "provider_error"),
        (_raise, "unreachable"),
    ],
)
async def test_nebius_validation_failures_are_codes_only(
    client, session, tenant, provider, caplog, respond, code
):
    caplog.set_level(logging.DEBUG)
    environment = await _environment(session, tenant)
    provider.respond = respond
    response = await client.put(_url(environment, "nebius"), json=NEBIUS_BODY)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    # Redirects are never followed.
    assert len(provider.requests) == 1
    assert await _row(session, environment, "nebius") is None
    _assert_no_secret(response.text, caplog.text)


@pytest.mark.parametrize(
    "respond, code",
    [
        (_status(401), "auth_failed"),
        (_status(500, {"error": {"message": SIGNOZ_SECRET}}), "unreachable"),
        (
            _status(400, {"error": {"code": "x", "message": f"bad {SIGNOZ_SECRET}"}}),
            "provider_error",
        ),  # fmt: skip
        (lambda r: httpx.Response(200, text="<html>app</html>"), "provider_error"),
        (
            lambda r: httpx.Response(307, headers={"location": "http://127.0.0.1/"}),
            "provider_error",
        ),  # fmt: skip
    ],
)
async def test_signoz_validation_failures_are_codes_only(
    client, session, tenant, provider, respond, code
):
    environment = await _environment(session, tenant)
    provider.respond = respond
    response = await client.put(_url(environment, "signoz"), json=SIGNOZ_BODY)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    assert len(provider.requests) == 1
    _assert_no_secret(response.text)


# --- destinations ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "https://evil.example.com/v1/",
        "https://api.tokenfactory.nebius.com:8443/v1/",
        "https://169.254.169.254/v1/",
    ],
)
async def test_nebius_endpoints_must_be_allowlisted(
    client, session, tenant, provider, monkeypatch, base_url
):
    async def resolve(host, port):
        return ["93.184.216.34"]

    # Public and https: only the allowlist refuses these.
    monkeypatch.setattr(network, "resolve", resolve)
    environment = await _environment(session, tenant)
    response = await client.put(
        _url(environment, "nebius"), json=NEBIUS_BODY | {"base_url": base_url}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "destination_not_allowed"
    assert provider.requests == []


@pytest.mark.parametrize(
    "base_url, answer",
    [
        ("http://signoz.example.com", ["93.184.216.34"]),  # public needs https
        ("https://signoz.example.com", ["10.0.0.5"]),
        ("https://signoz.example.com", ["93.184.216.34", "127.0.0.1"]),
        ("https://127.0.0.1:8080", ["127.0.0.1"]),
        ("https://[::ffff:169.254.169.254]", ["::ffff:169.254.169.254"]),
        ("http://signoz.test:9000", ["127.0.0.1"]),  # allowlist entries are exact origins
    ],
)
async def test_private_telemetry_destinations_are_refused(
    client, session, tenant, provider, monkeypatch, base_url, answer
):
    async def resolve(host, port):
        return answer

    monkeypatch.setattr(network, "resolve", resolve)
    environment = await _environment(session, tenant)
    response = await client.put(
        _url(environment, "signoz"), json=SIGNOZ_BODY | {"base_url": base_url}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "destination_not_allowed"
    assert "10.0.0.5" not in response.text and "127.0.0.1" not in response.text
    assert provider.requests == []


async def test_public_https_telemetry_is_accepted(client, session, tenant, provider, monkeypatch):
    async def resolve(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(network, "resolve", resolve)
    environment = await _environment(session, tenant)
    response = await client.put(
        _url(environment, "signoz"),
        json=SIGNOZ_BODY | {"base_url": "https://signoz.example.com/"},
    )
    assert response.status_code == 200, response.text
    assert provider.requests[0].url.host == "signoz.example.com"


async def test_unresolvable_telemetry_host_is_unreachable(
    client, session, tenant, provider, monkeypatch
):
    async def resolve(host, port):
        raise OSError("no such host")

    monkeypatch.setattr(network, "resolve", resolve)
    environment = await _environment(session, tenant)
    response = await client.put(
        _url(environment, "signoz"), json=SIGNOZ_BODY | {"base_url": "https://nope.example.com"}
    )
    assert response.json()["detail"]["code"] == "unreachable"
    assert provider.requests == []
