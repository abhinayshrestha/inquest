import json

import pytest

from src.mcp_server.server import MCP_TO_CANONICAL, RESULT_SCHEMA
from src.tools import TOOLS, list_tools
from tests.mcp_server.conftest import connect, token_for
from tests.tools.conftest import World

EXPECTED = {
    "signoz_query_metrics": "signoz.query_metrics",
    "signoz_search_logs": "signoz.search_logs",
    "signoz_log_volume": "signoz.log_volume",
    "signoz_search_spans": "signoz.search_spans",
    "signoz_trace_stats": "signoz.trace_stats",
    "signoz_get_trace": "signoz.get_trace",
}


def test_every_registered_tool_has_one_function_calling_safe_name():
    assert MCP_TO_CANONICAL == EXPECTED
    assert set(EXPECTED.values()) == set(TOOLS)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_discovery_lists_exactly_the_registered_tools(mcp_app, world: World, mode: str):
    async with connect(mcp_app, token_for(world), mode=mode) as client:
        tools = (await client.list_tools()).tools

    descriptors = {d.name: d for d in list_tools()}
    assert sorted(t.name for t in tools) == sorted(EXPECTED)
    for tool in tools:
        descriptor = descriptors[EXPECTED[tool.name]]
        assert tool.description == descriptor.description
        assert tool.input_schema == descriptor.input_schema
        assert tool.output_schema == RESULT_SCHEMA
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


async def test_no_tool_schema_accepts_binding_ids(mcp_app, world: World):
    async with connect(mcp_app, token_for(world)) as client:
        tools = (await client.list_tools()).tools

    for tool in tools:
        schema = json.dumps(tool.input_schema)
        for field in ("investigation_id", "assignment_id", "environment_id", "idempotency"):
            assert field not in schema, (tool.name, field)
        assert tool.input_schema.get("additionalProperties") is False
