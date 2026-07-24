"""search_errors — MCP tool registration."""

from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

from mcp_server.tools.search_errors.handler import handle


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def search_errors(
        query: Annotated[str, Field(
            description="Key phrase from the error message, code, or symptom. "
                        "Case-insensitive substring match over title, issue, summary, and tags. "
                        "Use the distinctive part (e.g. 'SSL handshake', 'MPL not found', 'XSUAA 401'), "
                        "not the whole stack trace. Leave empty to browse by family/type only.")] = "",
        family: Annotated[str, Field(
            description="Optional exact error family to filter by, e.g. 'Authentication', "
                        "'Connection', 'Certificate & TLS'. Get valid names from list_families().")] = "",
        type: Annotated[str, Field(
            description="Optional exact error type to filter by, as stored on the summary. "
                        "Omit unless you already know a specific type value.")] = "",
        limit: Annotated[int, Field(
            ge=1, le=50,
            description="Max number of hits to return (1-50). Default 20.")] = 20,
    ) -> list[dict]:
        """Keyword-only browse of the SAP-error knowledge base (compact hits).

        Prefer hybrid_search for diagnosis. This matches `query` against
        title/issue/summary/tags and optionally filters by `family`/`type`. Returns:
        id, note_number, title, family, type, snippet. Expand with get_error(id).
        """
        return await handle(query=query, family=family, type=type, limit=limit)
