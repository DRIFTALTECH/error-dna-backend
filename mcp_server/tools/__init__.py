"""Register every MCP tool with the FastMCP instance."""

from mcp.server.fastmcp import FastMCP

from mcp_server.tools.hybrid_search.tool import register as register_hybrid_search
from mcp_server.tools.search_errors.tool import register as register_search_errors
from mcp_server.tools.get_error.tool import register as register_get_error
from mcp_server.tools.list_families.tool import register as register_list_families


def register_all(mcp: FastMCP) -> None:
    register_hybrid_search(mcp)
    register_search_errors(mcp)
    register_get_error(mcp)
    register_list_families(mcp)
