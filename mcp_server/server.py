"""Error DNA — MCP server entrypoint.

Exposes curated SAP-error fixes over MCP. Tools live under mcp_server/tools/*/
(each with tool.py, handler.py, reference.md). This file wires FastMCP, resources,
and prompts, then registers all tools.

Security: streamable-http is gated by McpBearerMiddleware. No bearer configured
in Developer Settings → 401 on every call. Valid Authorization: Bearer required.

Run:  python3 -m mcp_server           # streamable-http on :3333/mcp
Test: python3 -m mcp_server selftest  # handlers only (no HTTP auth)
"""

import os
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP

from mcp_server.bearer import McpBearerMiddleware
from mcp_server.tools import register_all
from mcp_server.tools.get_error.handler import format_fix, handle as get_error_handle

INSTRUCTIONS = """\
Error DNA is a curated knowledge base of SAP integration error fixes — SAP CPI /
Integration Suite notes distilled by an LLM into structured, verified, step-by-step
fixes and grouped into error families (Authentication, Connection, Certificate & TLS,
Mapping, Messaging, and more).

Use this server whenever a user hits an SAP or integration error — a stack trace, an
error code, a failing adapter, or a symptom — and wants a known, cited fix.

Typical flow:
  1. Prefer hybrid_search(query) — semantic + keyword top matches with match_percent,
     full summary, and images in one call.
  2. (optional) list_families() / search_errors() for browse/filter by family.
  3. get_error(id) if you only have an id and need the full fix.
  4. Or attach a fix via the `error://{id}` resource, or the `diagnose` prompt.

Rules: always cite the note_number when you give a fix. If hybrid_search returns no
hits, say so plainly — do NOT invent SAP notes or fixes. This server is read-only;
you cannot create, edit, or delete knowledge-base entries.

Auth: every HTTP request must include Authorization: Bearer <token> from Error DNA
Developer Settings. Calls without a valid bearer are rejected.
"""

mcp = FastMCP(
    "Error DNA Knowledge Base",
    instructions=INSTRUCTIONS,
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "3333")),
)

register_all(mcp)


@mcp.resource("error://{id}")
async def error_resource(id: str) -> str:
    """One fix as attachable markdown context (pin into chat instead of tool-calling).
    URI: error://<id>, ids from search_errors."""
    detail = await get_error_handle(int(id))
    if "error" in detail and "how_to_fix" not in detail:
        return f"error://{id} — {detail['error']}"
    return format_fix(detail)


@mcp.prompt()
def diagnose(error_text: str) -> str:
    """Seed a diagnosis: hybrid-search the KB, then reason over the top fix."""
    return (
        "You have MCP tools into the Error DNA SAP knowledge base: "
        "`hybrid_search(query)`, `search_errors(query, family?, type?)`, "
        "`get_error(id)`, `list_families()`, and `error://{id}` resources.\n\n"
        f"Diagnose this error:\n\n```\n{error_text.strip()}\n```\n\n"
        "Steps: (1) call `hybrid_search` with the key phrase from the error. "
        "(2) Use the top hit (highest match_percent) — it already has the full fix "
        "and images. (3) Answer with the problem, fix steps, and gotchas — cite "
        "the note_number. (4) If no hits, say so plainly and give best-effort guidance."
    )


def build_http_app():
    """Streamable-http Starlette app wrapped with Bearer gate (pure ASGI)."""
    return McpBearerMiddleware(mcp.streamable_http_app())


async def _selftest():
    from mcp_server.tools.search_errors.handler import handle as search_errors
    from mcp_server.tools.list_families.handler import handle as list_families
    from mcp_server.tools.hybrid_search.handler import handle as hybrid_search
    from mcp_server.tools.get_error.handler import handle as get_error

    hits = await search_errors(query="", limit=3)
    assert isinstance(hits, list), "search_errors must return a list"
    fams = await list_families()
    assert isinstance(fams, list) and fams, "list_families must return families"
    hybrid = await hybrid_search(query="SSL handshake", limit=3)
    assert isinstance(hybrid, list), "hybrid_search must return a list"
    if hybrid:
        assert "match_percent" in hybrid[0], "hybrid hit needs match_percent"
        assert "how_to_fix" in hybrid[0], "hybrid hit needs full summary"
        assert "images" in hybrid[0], "hybrid hit needs images"
        assert "source" not in hybrid[0], "hybrid hit must not expose source"
    if hits:
        detail = await get_error(hits[0]["id"])
        assert "how_to_fix" in detail, "get_error must return the FE-shaped fix"
        md = format_fix(detail)
        assert md.startswith("#"), "resource markdown must render"
        assert "hybrid_search" in diagnose("boom"), "prompt must reference hybrid_search"
    print(f"✅ selftest ok — {len(hits)} keyword hit(s), {len(hybrid)} hybrid, {len(fams)} families")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        import asyncio
        asyncio.run(_selftest())
        return

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "3333"))
    print(f"🧩 Error DNA MCP on http://{host}:{port}/mcp (Bearer required)")
    uvicorn.run(build_http_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
