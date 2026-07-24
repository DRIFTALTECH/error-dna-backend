"""hybrid_search — MCP tool registration."""

from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

from mcp_server.tools.hybrid_search.handler import handle


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def hybrid_search(
        query: Annotated[str, Field(
            description="Error text, symptom, code, or key phrase to search for. "
                        "Hybrid: Titan vector similarity + keyword match. "
                        "Use a distinctive phrase (e.g. 'SSL handshake failure', 'HTTP 415 OAuth2'), "
                        "not a huge raw dump if you can trim it.")],
        limit: Annotated[int, Field(
            ge=1, le=20,
            description="Max hits to return (1-20). Default 5.")] = 5,
    ) -> list[dict]:
        """Hybrid search the knowledge base — top matches with full summaries.

        Embeds the query, blends vector + keyword scores, returns up to `limit` hits.
        Each hit is a full fix (the_problem, whats_going_on, how_to_fix, gotchas, tags,
        note_number, …) plus match_percent (0–100) and images (URL map; empty for notes).
        Prefer this over search_errors when diagnosing an error.
        """
        return await handle(query, limit=limit)
