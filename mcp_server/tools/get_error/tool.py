"""get_error — MCP tool registration."""

from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

from mcp_server.tools.get_error.handler import handle


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def get_error(
        id: Annotated[int, Field(
            description="Numeric summary id taken from a search_errors hit (its `id` field). "
                        "This is the internal id, NOT the SAP note_number.")],
    ) -> dict:
        """Get the full fix for one error by its id.

        Returns the complete, frontend-shaped fix: the_problem, whats_going_on,
        numbered how_to_fix steps, gotchas (name+description), tags, environment,
        note_number, and version. Call after search_errors to expand the chosen hit.
        Returns {"error": ...} if no summary has that id.
        """
        return await handle(id)
