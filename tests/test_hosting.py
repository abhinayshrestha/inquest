"""Hosting boundary (D041, audit H5/M1): the private MCP listener, hosted-mode startup
refusals, the validated public listener and its proxy trust. The proxy itself and the
network layout are exercised end to end by deploy/e2e (docs/deployment.md)."""

import base64
import json
import os
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

import src.main
from src.config import Settings, get_settings
from src.deployment import check_deployment, deployment_problems
from src.mcp_server import McpSettings, build_mcp_app
from src.mcp_server.auth import LISTEN_HOST, secret_problem
from src.mcp_server.listener import serve_mcp
from src.serve import ServeSettings, serve_config, serve_problems
from src.services import limits
from tests.mcp_server.conftest import SECRET, token_for
from tests.tools.conftest import World, make_world, world  # noqa: F401

STRONG = base64.b64encode(os.urandom(48)).decode()
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind((LISTEN_HOST, 0))
        return s.getsockname()[1]


def non_loopback_address() -> str | None:
    """This host's address on its default route (a UDP connect sends nothing)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
        except OSError:
            return None
        address = s.getsockname()[0]
    return None if address.startswith("127.") else address


def hosted(**update: Any) -> Settings:
    """Production settings as a correctly configured hosted process would have them."""
    return get_settings().model_copy(
        update={
            "app_env": "production",
            "inquest_db_user": "inquest_app",
            "inquest_db_password": SecretStr("x" * 32),
            "postgres_password": None,
            "inquest_db_migration_password": None,
            **update,
        }
    )


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign-in and credential storage configured (their own validation is tested elsewhere)."""
    monkeypatch.setattr("src.deployment.load_auth_settings", lambda: None)
    monkeypatch.setattr("src.deployment.load_credential_config", lambda: None)


# --- the private MCP listener -------------------------------------------------------------


def test_the_mcp_url_is_derived_loopback_only():
    assert McpSettings(port=9123).url == "http://127.0.0.1:9123/"
    assert "url" not in McpSettings.model_fields  # MCP_URL can no longer point elsewhere


@pytest.fixture
async def listener() -> AsyncIterator[McpSettings]:
    settings = McpSettings(token_secret=SECRET, port=free_port())
    _, app = build_mcp_app(settings)
    async with serve_mcp(app, settings):
        yield settings


async def test_the_listener_serves_authenticated_investigators_on_loopback(
    listener: McpSettings,
    world: World,  # noqa: F811
):
    async with httpx2.AsyncClient(base_url=listener.url, headers=MCP_HEADERS) as client:
        anonymous = await client.post("/", json=LIST)
        token = {"authorization": f"Bearer {token_for(world)}"}
        listed = await client.post("/", json=LIST, headers=token)
    assert anonymous.status_code == 401
    assert listed.status_code == 200
    assert {t["name"] for t in listed.json()["result"]["tools"]} >= {"signoz_trace_stats"}


async def test_the_listener_is_unreachable_on_other_interfaces(listener: McpSettings):
    address = non_loopback_address()
    if address is None:
        pytest.skip("no non-loopback interface")
    with pytest.raises(OSError):
        socket.create_connection((address, listener.port), timeout=2).close()


async def test_a_taken_port_refuses_to_start():
    with socket.create_server((LISTEN_HOST, 0)) as taken:
        settings = McpSettings(token_secret=SECRET, port=taken.getsockname()[1])
        _, app = build_mcp_app(settings)
        with pytest.raises(OSError):
            async with serve_mcp(app, settings):
                pass


async def test_the_listener_stops_with_its_context():
    settings = McpSettings(token_secret=SECRET, port=free_port())
    _, app = build_mcp_app(settings)
    async with serve_mcp(app, settings):
        pass
    with pytest.raises(OSError):
        socket.create_connection((LISTEN_HOST, settings.port), timeout=2).close()


# --- MCP secret strength ------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        None,
        "change-me-to-at-least-32-random-characters",  # the .env.example placeholder
        "correct horse battery staple and more words",  # not base64
        base64.b64encode(bytes(range(100, 124))).decode(),  # 24 distinct bytes: too short
        base64.b64encode(b"ab" * 32).decode(),  # repetitive
        "A" * 64,  # valid base64, one byte value
    ],
)
def test_weak_mcp_secrets_are_unfit_for_hosting(secret: str | None):
    problem = secret_problem(McpSettings(token_secret=secret))
    assert problem is not None and "MCP_TOKEN_SECRET" in problem
    if secret:
        assert secret not in problem


def test_a_generated_mcp_secret_is_accepted():
    assert secret_problem(McpSettings(token_secret=STRONG)) is None


# --- hosted startup refusals --------------------------------------------------------------


def test_a_correctly_configured_hosted_process_has_no_problems(configured):
    assert deployment_problems(hosted(), McpSettings(token_secret=STRONG)) == []


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"inquest_db_user": None, "inquest_db_password": None}, "INQUEST_DB_USER"),
        ({"postgres_password": SecretStr("s3cret-value")}, "POSTGRES_PASSWORD"),
        ({"inquest_db_migration_password": SecretStr("s3cret-value")}, "INQUEST_DB_MIGRATION"),
    ],
)
def test_hosted_processes_refuse_database_secrets_they_must_not_hold(configured, update, expected):
    settings = hosted(**update)
    with pytest.raises(RuntimeError, match=expected) as refused:
        check_deployment(settings, McpSettings(token_secret=STRONG))
    assert "s3cret-value" not in str(refused.value)


def test_hosted_processes_refuse_a_weak_mcp_secret(configured):
    with pytest.raises(RuntimeError, match="MCP_TOKEN_SECRET"):
        check_deployment(hosted(), McpSettings(token_secret="x" * 40))


def test_hosted_processes_refuse_missing_sign_in_and_credential_storage(monkeypatch):
    from src.auth.settings import AuthNotConfigured
    from src.credentials.settings import CredentialStorageUnavailable

    def no_auth():
        raise AuthNotConfigured("missing or invalid settings: INQUEST_AUTH_ISSUER")

    def no_keys():
        raise CredentialStorageUnavailable("INQUEST_CREDENTIAL_KEYS is not set")

    monkeypatch.setattr("src.deployment.load_auth_settings", no_auth)
    monkeypatch.setattr("src.deployment.load_credential_config", no_keys)
    problems = deployment_problems(hosted(), McpSettings(token_secret=STRONG))
    assert any(p.startswith("sign-in:") for p in problems)
    assert any(p.startswith("credential storage:") for p in problems)


def test_local_development_keeps_its_defaults():
    local = hosted(app_env="local", inquest_db_user=None, postgres_password=SecretStr("x"))
    check_deployment(local, McpSettings(token_secret=None))


async def test_the_hosted_app_refuses_to_start_before_touching_anything(monkeypatch):
    monkeypatch.setattr(src.main, "get_settings", lambda: hosted(postgres_password=SecretStr("a")))
    app = src.main.create_app()
    with pytest.raises(RuntimeError, match="refusing to start"):
        async with app.router.lifespan_context(app):
            pass


async def test_the_hosted_app_publishes_no_api_explorer(monkeypatch):
    monkeypatch.setattr(src.main, "get_settings", hosted)
    app = src.main.create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404, path


async def test_the_public_app_has_no_mcp_route():
    assert not [r for r in src.main.app.routes if "mcp" in getattr(r, "path", "").lower()]


# --- the public listener and proxy trust --------------------------------------------------


@pytest.mark.parametrize(
    ("proxies", "expected"),
    [
        (["*"], "not an address"),
        (["0.0.0.0/0"], "not every address"),
        (["::/0"], "not every address"),
        (["proxy.internal"], "not an address"),
        ([], "INQUEST_TRUSTED_PROXIES is not set"),
    ],
)
def test_unsafe_proxy_trust_is_refused_when_hosted(proxies, expected):
    problems = serve_problems(ServeSettings(trusted_proxies=proxies), "production")
    assert any(expected in p for p in problems), problems


def test_extra_workers_are_refused(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert any("WEB_CONCURRENCY" in p for p in serve_problems(ServeSettings(), "local"))


def test_development_may_run_without_a_proxy():
    assert serve_problems(ServeSettings(), "local") == []


def test_the_listener_trusts_exactly_the_configured_proxy(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")  # must not widen anything
    config = serve_config(ServeSettings(trusted_proxies=["10.9.0.2"]))
    assert (config.host, config.port, config.workers) == ("127.0.0.1", 8000, 1)
    assert config.proxy_headers and config.forwarded_allow_ips == ["10.9.0.2"]
    assert config.server_header is False
    untrusting = serve_config(ServeSettings())
    assert untrusting.proxy_headers is False and untrusting.forwarded_allow_ips == []


async def test_clients_behind_the_trusted_proxy_have_independent_limits(monkeypatch):
    """Through the exact uvicorn configuration src.serve runs: forwarded addresses count
    only from the proxy; spoofed hops and direct-origin requests gain nothing."""
    monkeypatch.setenv("INQUEST_RATE_LIMITS", json.dumps({"requests.ip": "3/60"}))
    limits.limit_settings.cache_clear()
    limits.reset_process_state()
    config = serve_config(ServeSettings(trusted_proxies=["10.9.0.2"]))
    config.load()

    def peer(address: str) -> AsyncClient:
        transport = ASGITransport(app=config.loaded_app, client=(address, 4000))
        return AsyncClient(transport=transport, base_url="http://test")

    async def codes(client: AsyncClient, n: int, **headers: str) -> list[int]:
        return [(await client.get("/health", headers=headers)).status_code for _ in range(n)]

    try:
        async with peer("10.9.0.2") as proxy, peer("203.0.113.9") as direct:
            a = await codes(proxy, 4, **{"x-forwarded-for": "192.0.2.1"})
            b = await codes(proxy, 1, **{"x-forwarded-for": "192.0.2.2"})
            # A prepended hop (what an appending proxy would pass on) does not name the client.
            spoofed = await codes(proxy, 1, **{"x-forwarded-for": "192.0.2.3, 192.0.2.1"})
            # Forwarded and X-Real-IP are never read: these count against the proxy itself.
            ignored = await codes(proxy, 4, forwarded="for=192.0.2.4", **{"x-real-ip": "192.0.2.5"})
            # Reaching the origin directly, a forged header is just ignored.
            bypass = [
                (await direct.get("/health", headers={"x-forwarded-for": f"192.0.2.{i}"}))
                for i in range(10, 14)
            ]
    finally:
        limits.limit_settings.cache_clear()
        limits.reset_process_state()
    assert a == [200, 200, 200, 429]
    assert b == [200]
    assert spoofed == [429]
    assert ignored == [200, 200, 200, 429]
    assert [r.status_code for r in bypass] == [200, 200, 200, 429]


async def test_forwarded_scheme_is_honoured_only_from_the_proxy():
    seen: list[str] = []

    async def echo(scope: dict, receive: Any, send: Any) -> None:
        seen.append(scope["scheme"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    config = serve_config(ServeSettings(trusted_proxies=["10.9.0.2"]))
    config.app = echo
    config.load()
    for address in ("10.9.0.2", "203.0.113.9"):
        transport = ASGITransport(app=config.loaded_app, client=(address, 4000))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/", headers={"x-forwarded-proto": "https"})
    assert seen == ["https", "http"]
