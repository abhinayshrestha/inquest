"""MCP front door for Environment Investigators.

Translates MCP tools/list and tools/call into list_tools() and execute_tool(); it never
reaches SigNoz itself. Every call runs for the assignment named by the verified bearer
token, never by anything the model wrote. Arguments reach the executor unvalidated so that
invalid ones are rejected, audited and counted there like any other request.
"""

import json
import logging
from typing import Any

import mcp_types as types
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request

from src.mcp_server.auth import (
    INVESTIGATE_SCOPE,
    AssignmentTokenVerifier,
    McpSettings,
)
from src.models import ToolCallStatus
from src.tools import (
    TOOLS,
    ExecutionContext,
    ToolError,
    ToolExecutionResult,
    execute_tool,
    list_tools,
)

logger = logging.getLogger(__name__)

# OpenAI-compatible function calling (Nemotron on Nebius) allows only [A-Za-z0-9_-] in
# tool names, so the dotted registry names are exposed with underscores. ToolCall rows
# keep the canonical name.
MCP_TO_CANONICAL = {name.replace(".", "_"): name for name in TOOLS}
CANONICAL_TO_MCP = {v: k for k, v in MCP_TO_CANONICAL.items()}

# Set by the investigator runtime from the framework's tool_call_id (LangGraph), never
# by the model, which only writes `arguments`. _meta is the protocol-native carrier; the
# header exists because langchain-mcp-adapters interceptors can set headers but not _meta.
IDEMPOTENCY_META_KEY = "inquest/tool_call_id"
IDEMPOTENCY_HEADER = "inquest-tool-call-id"

BINDING_KEYS = ("investigation_id", "assignment_id", "environment_id", "organization_id")
MAX_TOOL_NAME_CHARS = 150
READ_ONLY = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False
)
RESULT_SCHEMA = ToolExecutionResult.model_json_schema()


class _Refused(Exception):
    """A request refused at the MCP layer, before the executor can bind it."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


async def _list_tools(
    ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=CANONICAL_TO_MCP[d.name],
                description=d.description,
                input_schema=d.input_schema,
                output_schema=RESULT_SCHEMA,
                annotations=READ_ONLY,
            )
            for d in list_tools()
        ]
    )


def _binding(ctx: ServerRequestContext[Any, Any]) -> dict[str, Any]:
    # The bearer middleware already refused unauthenticated HTTP requests; this also
    # covers transports without one (in-process, stdio), which must not run anything.
    request = ctx.request
    user = request.scope.get("user") if isinstance(request, Request) else None
    if not isinstance(user, AuthenticatedUser) or INVESTIGATE_SCOPE not in user.scopes:
        raise _Refused("mcp.unauthenticated", "no verified investigator credentials")
    claims = user.access_token.claims or {}
    if not all(k in claims for k in BINDING_KEYS):
        raise _Refused("mcp.unauthenticated", "credentials carry no assignment binding")
    return claims


def _idempotency_key(ctx: ServerRequestContext[Any, Any]) -> str | None:
    from_meta = (ctx.meta or {}).get(IDEMPOTENCY_META_KEY)
    request = ctx.request
    from_header = request.headers.get(IDEMPOTENCY_HEADER) if isinstance(request, Request) else None
    if from_meta is not None and not isinstance(from_meta, str):
        raise _Refused(
            "mcp.invalid_idempotency_key", f"_meta {IDEMPOTENCY_META_KEY} must be a string"
        )
    if from_meta is not None and from_header is not None and from_meta != from_header:
        raise _Refused(
            "mcp.invalid_idempotency_key", "_meta and header tool call identifiers disagree"
        )
    return from_meta if from_meta is not None else from_header


def _context(ctx: ServerRequestContext[Any, Any]) -> ExecutionContext:
    binding = _binding(ctx)
    key = _idempotency_key(ctx)
    try:
        return ExecutionContext(**{k: binding[k] for k in BINDING_KEYS}, idempotency_key=key)
    except ValidationError:
        # Only the key can be malformed here: the binding comes from a verified token.
        # Running without it would silently drop replay protection, so nothing runs.
        raise _Refused(
            "mcp.invalid_idempotency_key",
            "tool call identifier must be 1-100 characters of [A-Za-z0-9_.:-]",
        ) from None


def _to_mcp(result: ToolExecutionResult) -> types.CallToolResult:
    payload = result.model_dump(mode="json")
    return types.CallToolResult(
        # Text mirrors the structured result for clients that only read content.
        content=[types.TextContent(text=json.dumps(payload, separators=(",", ":")))],
        structured_content=payload,
        # Empty and not_found outcomes succeeded; the model reads `outcome` for those.
        is_error=result.status is not ToolCallStatus.SUCCEEDED,
    )


def _refusal(
    tool_name: str, code: str, message: str, status: ToolCallStatus = ToolCallStatus.REJECTED
) -> types.CallToolResult:
    return _to_mcp(
        ToolExecutionResult(
            tool_call_id=None,
            tool_name=tool_name[:MAX_TOOL_NAME_CHARS],
            status=status,
            error=ToolError(code=code, message=message),
        )
    )


async def _call_tool(
    ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
) -> types.CallToolResult:
    try:
        context = _context(ctx)
    except _Refused as refused:
        logger.warning("MCP tool call refused before execution: %s", refused.code)
        return _refusal(params.name, refused.code, refused.message)
    # Canonical dotted names resolve too: same tool, same checks. Anything else reaches
    # the executor as-is so the unknown-tool rejection is audited and counted.
    tool_name = MCP_TO_CANONICAL.get(params.name, params.name)
    try:
        result = await execute_tool(tool_name, params.arguments or {}, context)
    except Exception:
        # execute_tool reports failures as results; this guards the contract without
        # letting exception text (hosts, payloads) reach the model.
        logger.exception("MCP tool call for assignment %s raised", context.assignment_id)
        return _refusal(params.name, "mcp.internal", "internal error", ToolCallStatus.FAILED)
    return _to_mcp(result)


def build_mcp_app(settings: McpSettings | None = None) -> tuple[Server[Any], Starlette]:
    """Returns the server and its ASGI app. The app's own lifespan runs the session
    manager, so it must be served directly (src.mcp_server.listener), not mounted."""
    settings = settings or McpSettings()
    server: Server[Any] = Server(
        "inquest",
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
        # Our schemas declare no Mcp-Param headers; skip the SDK's per-call schema lookup.
        get_tool_input_schema=lambda name: None,
    )
    app = server.streamable_http_app(
        streamable_http_path="/",
        # No per-session server state: every request carries its own token, so any worker
        # can serve it, including handshake-era (2025) clients.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            # Investigators are server-side clients; a browser Origin is never legitimate.
            allowed_origins=[],
        ),
        auth=AuthSettings(
            # Resource-server only: no OAuth routes are served and the issuer is never
            # contacted. resource_server_url stays None so no discovery metadata is published.
            issuer_url="http://inquest.internal",
            resource_server_url=None,
            required_scopes=[INVESTIGATE_SCOPE],
        ),
        token_verifier=AssignmentTokenVerifier(settings),
    )
    return server, app
