"""hybrid_search — MCP tool registration."""

from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

from config import HYBRID_SEARCH_DEFAULT_LIMIT, HYBRID_SEARCH_MAX_LIMIT
from mcp_server.tools.hybrid_search.handler import handle


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    async def hybrid_search(
        query: Annotated[str, Field(
            description="Error text, symptom, code, or key phrase to search for. "
                        "Titan vector similarity over the whole knowledge base. "
                        "Use a distinctive phrase (e.g. 'SSL handshake failure', 'HTTP 415 OAuth2'), "
                        "not a huge raw dump if you can trim it.")],
        limit: Annotated[int, Field(
            ge=1, le=HYBRID_SEARCH_MAX_LIMIT,
            description=f"Max hits to return (1-{HYBRID_SEARCH_MAX_LIMIT}). "
                        f"Default {HYBRID_SEARCH_DEFAULT_LIMIT}.")] = HYBRID_SEARCH_DEFAULT_LIMIT,
    ) -> list[dict]:
        """Semantic search over the knowledge base — top matches with full summaries.

        Embeds the query and returns the `limit` nearest notes by cosine similarity.
        Each hit is a full fix (the_problem, whats_going_on, how_to_fix, gotchas, tags,
        note_number, …) plus match_percent (0–100) and images (URL map; empty for notes).
        Prefer this over search_errors when diagnosing an error.
        """
        return await handle(query, limit=limit)
