"""MCP server for Environment Investigators: the six registered tools over Streamable HTTP,
each call bound to one assignment by a server-minted token and run through execute_tool()."""

from src.mcp_server.auth import McpSettings, mint_assignment_token
from src.mcp_server.server import build_mcp_app

__all__ = ["McpSettings", "build_mcp_app", "mint_assignment_token"]
