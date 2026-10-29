import ast
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from mcp import Client

from src.mcp_server import McpSettings, build_mcp_app, mint_assignment_token
from src.mcp_server.auth import AUDIENCE, INVESTIGATE_SCOPE, ISSUER, TokenConfigError
from src.models import InvestigationAssignment, ToolCall
from tests.mcp_server.conftest import SECRET, connect, http_client, token_for
from tests.tools.conftest import World, trace_stats_args

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
JSON_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def claims_for(world: World, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(world.assignment_id),
        "inv": str(world.investigation_id),
        "env": str(world.environment_id),
        "org": "legacy",
        "scope": INVESTIGATE_SCOPE,
        "iat": now,
        "exp": now + 300,
        "jti": uuid.uuid4().hex,
    }
    return {k: v for k, v in (claims | overrides).items() if v is not None}


def forged(world: World, *, key: str = SECRET, algorithm: str = "HS256", **overrides: Any) -> str:
    return jwt.encode(claims_for(world, **overrides), key, algorithm=algorithm)


async def post(app: Any, token: str | None, **headers: str) -> Any:
    async with http_client(app, token, **JSON_HEADERS, **headers) as client:
        return await client.post("/", json=LIST)


# --- authentication ----------------------------------------------------------------------


async def test_request_without_token_is_unauthorized(mcp_app):
    response = await post(mcp_app, None)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "make_token",
    [
        lambda w: "not-a-jwt",
        lambda w: forged(w, key="another-secret-" + "y" * 40),
        lambda w: forged(w, exp=int(time.time()) - 5),
        lambda w: forged(w, aud="some-other-service"),
        lambda w: forged(w, iss="someone-else"),
        lambda w: forged(w, jti=None),
        lambda w: forged(w, inv=None),
        lambda w: forged(w, sub="not-a-uuid"),
        lambda w: forged(w, algorithm="HS512"),
        lambda w: jwt.encode(claims_for(w), None, algorithm="none"),
        lambda w: token_for(w)[:-4] + "AAAA",
    ],
    ids=[
        "garbage",
        "wrong-secret",
        "expired",
        "wrong-audience",
        "wrong-issuer",
        "missing-jti",
        "missing-investigation",
        "non-uuid-assignment",
        "other-algorithm",
        "alg-none",
        "tampered-signature",
    ],
)
@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
async def test_invalid_tokens_are_unauthorized(mcp_app, world: World, make_token):
    response = await post(mcp_app, make_token(world))
    assert response.status_code == 401


async def test_token_without_investigator_scope_is_forbidden(mcp_app, world: World):
    response = await post(mcp_app, forged(world, scope="inquest:main"))
    assert response.status_code == 403


async def test_without_a_configured_secret_nothing_is_accepted(world: World):
    with pytest.raises(TokenConfigError):
        mint_assignment_token(
            InvestigationAssignment(id=world.assignment_id),
            McpSettings(token_secret="short"),
            organization_id=None,
        )
    _, app = build_mcp_app(McpSettings(token_secret=None, allowed_hosts=["testserver"]))
    response = await post(app, token_for(world))
    assert response.status_code == 401


async def test_unexpected_host_is_rejected(mcp_app, world: World):
    async with http_client(mcp_app, token_for(world), **JSON_HEADERS) as client:
        client.base_url = "http://attacker.example"
        response = await client.post("/", json=LIST)
    assert response.status_code == 421


async def test_browser_origin_is_rejected(mcp_app, world: World):
    response = await post(mcp_app, token_for(world), origin="http://localhost:3000")
    assert response.status_code == 403


async def test_the_public_app_does_not_serve_mcp(world: World):
    """MCP lives on the loopback listener only (D041): the public API answers 404 for
    every spelling, even with a valid token."""
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        headers = {**JSON_HEADERS, "authorization": f"Bearer {token_for(world)}"}
        for path in ("/mcp", "/mcp/", "/MCP/", "/%6dcp/", "//mcp/", "/./mcp/", "/api/mcp/"):
            response = await client.post(path, json=LIST, headers=headers)
            assert response.status_code == 404, path


# --- binding and isolation ---------------------------------------------------------------


async def test_token_binds_its_own_assignment_only(mcp_app, make_world, signoz):
    signoz("trace_stats_scalar.json")
    a, b = await make_world(), await make_world()

    async with connect(mcp_app, token_for(a)) as client:
        # Model-supplied IDs are just invalid arguments: rejected, audited under A.
        smuggled = await client.call_tool(
            "signoz_trace_stats",
            trace_stats_args(
                assignment_id=str(b.assignment_id), environment_id=str(b.environment_id)
            ),
        )
        # A service from B's scope is outside A's environment.
        foreign = await client.call_tool("signoz_trace_stats", trace_stats_args(service=b.service))

    assert smuggled.structured_content["error"]["code"] == "policy.invalid_arguments"
    assert foreign.is_error is True
    assert foreign.structured_content["error"]["code"].startswith("policy.")
    assert len(await a.tool_calls()) == 2
    assert await b.tool_calls() == []


async def test_keys_are_scoped_per_assignment(mcp_app, make_world, signoz):
    seen = signoz("trace_stats_scalar.json")
    a, b = await make_world(), await make_world()
    for world in (a, b):
        async with connect(mcp_app, token_for(world)) as client:
            result = await client.call_tool(
                "signoz_trace_stats", trace_stats_args(), meta={"inquest/tool_call_id": "same"}
            )
        assert result.structured_content["replayed"] is False
    assert len(seen) == 2


async def test_in_process_transport_without_http_auth_runs_nothing(world: World, signoz):
    seen = signoz("trace_stats_scalar.json")
    server, _ = build_mcp_app(McpSettings(token_secret=SECRET))
    async with Client(server) as client:
        result = await client.call_tool("signoz_trace_stats", trace_stats_args())

    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "mcp.unauthenticated"
    assert seen == [] and await world.tool_calls() == []


async def test_token_never_reaches_logs_rows_or_responses(mcp_app, world: World, signoz, caplog):
    signoz("trace_stats_scalar.json")
    token = token_for(world)
    caplog.set_level(logging.DEBUG)
    async with connect(mcp_app, token) as client:
        ok = await client.call_tool("signoz_trace_stats", trace_stats_args())
        bad = await client.call_tool("signoz_trace_stats", {"group_by": "nonsense"})
    await post(mcp_app, token + "x")

    signature = token.rsplit(".", 1)[1]
    assert signature not in caplog.text
    for result in (ok, bad):
        assert signature not in json.dumps(result.structured_content)
    rows: list[ToolCall] = await world.tool_calls()
    assert len(rows) == 2
    for row in rows:
        stored = json.dumps(
            [row.input_json, row.output_metadata_json, row.error_message, row.result_summary]
        )
        assert signature not in stored


def test_mcp_package_cannot_reach_signoz_directly():
    root = Path(__file__).resolve().parents[2] / "src" / "mcp_server"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert not module.startswith("src.integrations"), (path.name, module)
                assert not module.startswith("src.tools.") or module == "src.tools", (
                    path.name,
                    module,
                )
