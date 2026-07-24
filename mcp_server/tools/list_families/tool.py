"""list_families — MCP tool registration."""

from mcp.server.fastmcp import FastMCP

from mcp_server.tools.list_families.handler import handle


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def list_families() -> list[dict]:
        """List all error families (categories), each with name, description, color, and
        fix_count. Takes no arguments. Use a returned `name` as the `family` filter in
        search_errors, or to orient the user on what the knowledge base covers.
        """
        return await handle()
