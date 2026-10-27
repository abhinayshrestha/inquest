"""Thin wrappers over the official MCP SDK client for Local Investigators (D018).

The bearer token is minted here from the assignment row and lives only in the HTTP
client's headers: never in graph state, checkpoints, prompts or logs. Tool calls carry
the framework tool_call_id as the idempotency key, so a replayed graph step returns the
stored result instead of querying SigNoz again.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from src.investigators import policy
from src.mcp_server import McpSettings, mint_assignment_token
from src.mcp_server.server import IDEMPOTENCY_META_KEY
from src.models import InvestigationAssignment
from src.tools.schemas import ToolExecutionResult

# Lifetime of one investigator run plus slack; the executor's running-check is the real
# revocation, expiry is the backstop.
TOKEN_SLACK_SECONDS = 60


class McpCallError(Exception):
    """Transport or protocol failure: the call may or may not have reached the executor."""


@asynccontextmanager
async def connect(
    assignment: InvestigationAssignment,
    *,
    organization_id: uuid.UUID | None,
    url: str,
    settings: McpSettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncIterator[Client]:
    """`organization_id` is the assignment's tenant, read by the caller from the
    assignment's own investigation and environment rows. `transport` lets tests drive
    the in-process app; production uses real HTTP."""
    token = mint_assignment_token(
        assignment,
        settings,
        organization_id=organization_id,
        ttl_seconds=int(policy.WALL_CLOCK_SECONDS) + TOKEN_SLACK_SECONDS,
    )
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(policy.MCP_CALL_TIMEOUT_SECONDS),
        transport=transport,
    )
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        yield client


# Schema keywords the model does not need; the executor still enforces the full schema.
DROPPED_KEYWORDS = frozenset({"title", "maxLength", "minLength", "additionalProperties"})


def compact_schema(node: Any, *, property_names: bool = False) -> Any:
    """The schema as the model sees it, about a third smaller. Tool schemas are resent on
    every agent call and were most of each prompt. Enums, patterns, numeric bounds, refs
    and required fields are kept; titles, null defaults and `X | null` wrappers go (an
    optional field is simply omitted), as do the internal docstrings of $defs."""
    if isinstance(node, list):
        return [compact_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    if property_names:
        return {name: compact_schema(sub) for name, sub in node.items()}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in DROPPED_KEYWORDS or (key == "default" and value is None):
            continue
        if key == "properties":
            out[key] = compact_schema(value, property_names=True)
        elif key == "$defs":
            out[key] = {
                name: {k: v for k, v in compact_schema(d).items() if k != "description"}
                for name, d in value.items()
            }
        else:
            out[key] = compact_schema(value)
    if "anyOf" in out:
        branches = [b for b in out["anyOf"] if b != {"type": "null"}]
        if len(branches) == 1:
            rest = {k: v for k, v in out.items() if k != "anyOf"}
            out = {**rest, **branches[0]}
        else:
            out["anyOf"] = branches
    return out


async def tool_specs(client: Client) -> list[dict[str, Any]]:
    """MCP tools as OpenAI-style function definitions for bind_tools."""
    listed = (await client.list_tools()).tools
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": compact_schema(t.input_schema),
            },
        }
        for t in listed
    ]


async def call_tool(
    client: Client, name: str, arguments: dict[str, Any], tool_call_id: str
) -> ToolExecutionResult:
    try:
        result = await client.call_tool(name, arguments, meta={IDEMPOTENCY_META_KEY: tool_call_id})
    except Exception as exc:
        # The SDK raises for transport and protocol errors; tool failures come back as
        # results. The message may carry URLs, never the token.
        raise McpCallError(type(exc).__name__) from exc
    try:
        return ToolExecutionResult.model_validate(result.structured_content or {})
    except ValidationError:
        raise McpCallError("malformed tool result") from None
