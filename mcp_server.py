"""Error DNA — MCP server (read-only knowledge base tools).

Exposes the curated SAP-error fixes over MCP so any client (Claude Desktop,
Cursor, your own agent) can search them as tools. Reuses the FastAPI backend's
DB layer and UI-shaper — no re-implementation, no LLM inside (this is a server,
not an agent).

Run:  python3 mcp_server.py           # serves streamable-http on :3333/mcp
Test: python3 mcp_server.py selftest  # hits the real DB, asserts tools work

# ponytail: read-only + open (no auth). Add bearer-token ASGI middleware +
# MCP_TOKEN in .env when this leaves localhost — see SECURITY note at bottom.
"""

import os
import sys
from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

from db import read
from routes.summaries import _summary_to_ui  # reuse the exact FE-shaped mapper

# Server-level system prompt. MCP returns this as `instructions` on initialize;
# clients (Claude Desktop, Cursor, agents) surface it to the LLM on connect —
# so any model that attaches this server knows what it is and how to drive it.
INSTRUCTIONS = """\
Error DNA is a curated knowledge base of SAP integration error fixes — SAP CPI /
Integration Suite notes distilled by an LLM into structured, verified, step-by-step
fixes and grouped into error families (Authentication, Connection, Certificate & TLS,
Mapping, Messaging, and more).

Use this server whenever a user hits an SAP or integration error — a stack trace, an
error code, a failing adapter, or a symptom — and wants a known, cited fix.

Typical flow:
  1. (optional) list_families() to see the categories and their fix counts.
  2. search_errors(query="<key phrase from the error>") — narrow with family/type.
  3. get_error(id) on the best hit for the full fix: the problem, what's going on,
     numbered how_to_fix steps, gotchas, tags, and the source note_number.
  4. Or attach a fix as context via the `error://{id}` resource, or invoke the
     `diagnose` prompt to run this flow automatically.

Rules: always cite the note_number when you give a fix. If search_errors returns no
hits, say so plainly — do NOT invent SAP notes or fixes. This server is read-only;
you cannot create, edit, or delete knowledge-base entries.
"""

mcp = FastMCP(
    "Error DNA Knowledge Base",
    instructions=INSTRUCTIONS,
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "3333")),
)


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
    """Search the SAP-error knowledge base and return compact matching hits.

    First step for any error lookup. Matches `query` against title/issue/summary/tags
    and optionally filters by `family`/`type`. Returns a list of hits, each with:
    id, note_number, title, family, type, and a snippet. Pick the best `id` and call
    get_error(id) for the full fix. Empty query + a family = browse that family.
    """
    where = ["is_latest = 1"]
    params: list = []
    if query:
        where.append("(title LIKE ? OR issue LIKE ? OR summary LIKE ? OR tags LIKE ?)")
        params += [f"%{query}%"] * 4
    if family:
        where.append("family = ?")
        params.append(family)
    if type:
        where.append("type = ?")
        params.append(type)

    limit = max(1, min(limit, 50))
    sql = (f"SELECT id, source_id, title, family, type, issue FROM summaries "
           f"WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?")

    rows = await read(sql, params + [limit])
    return [{
        "id": r["id"],
        "note_number": r["source_id"],
        "title": r["title"],
        "family": r["family"],
        "type": r["type"],
        "snippet": (r["issue"] or "")[:200],
    } for r in rows]


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
    rows = await read("SELECT * FROM summaries WHERE id = ?", (id,))
    if not rows:
        return {"error": f"No summary with id {id}"}
    return _summary_to_ui(rows[0])


@mcp.tool()
async def list_families() -> list[dict]:
    """List all error families (categories), each with name, description, color, and
    fix_count. Takes no arguments. Use a returned `name` as the `family` filter in
    search_errors, or to orient the user on what the knowledge base covers.
    """
    return await read(
        """SELECT f.family_name AS name, f.description, f.color,
                  COUNT(s.id) AS fix_count
           FROM error_families f
           LEFT JOIN summaries s ON s.family = f.family_name AND s.is_latest = 1
           GROUP BY f.family_name, f.description, f.color ORDER BY fix_count DESC"""
    )


def _format_fix(d: dict) -> str:
    """Render a get_error() dict as readable markdown (for the resource)."""
    def section(head, body):
        return f"## {head}\n{body}\n\n" if body else ""

    fixes = "\n".join(f"{i}. {s}" for i, s in enumerate(d.get("how_to_fix") or [], 1))
    gotchas = "\n".join(f"- **{g['name']}:** {g['description']}" for g in (d.get("gotchas") or []))
    tags = ", ".join(d.get("tags") or [])

    header = f"# {d.get('title') or 'Untitled'}"
    meta = f"*{d.get('note_number') or '?'} · v{d.get('version') or '1'}"
    if d.get("area"):
        meta += f" · {d['area']}"
    if d.get("type"):
        meta += f" · {d['type']}"
    meta += "*"

    return (
        f"{header}\n{meta}\n\n"
        + section("The problem", d.get("the_problem"))
        + section("What's going on", d.get("whats_going_on"))
        + section("How to fix", fixes)
        + section("Gotchas", gotchas)
        + (f"**Tags:** {tags}\n" if tags else "")
    ).strip()


@mcp.resource("error://{id}")
async def error_resource(id: str) -> str:
    """One fix as attachable markdown context (pin into chat instead of tool-calling).
    URI: error://<id>, ids from search_errors."""
    detail = await get_error(int(id))
    if "error" in detail and "how_to_fix" not in detail:
        return f"error://{id} — {detail['error']}"
    return _format_fix(detail)


@mcp.prompt()
def diagnose(error_text: str) -> str:
    """Seed a diagnosis: search the KB for this error, then reason over the top fix."""
    return (
        "You have MCP tools into the Error DNA SAP knowledge base: "
        "`search_errors(query, family?, type?)`, `get_error(id)`, `list_families()`, "
        "and `error://{id}` resources.\n\n"
        f"Diagnose this error:\n\n```\n{error_text.strip()}\n```\n\n"
        "Steps: (1) call `search_errors` with the key phrase from the error. "
        "(2) If hits, `get_error(id)` on the best match. (3) Answer with the problem, "
        "the fix steps, and any gotchas — cite the note_number. "
        "(4) If no hits, say so plainly and give best-effort general guidance."
    )


async def _selftest():
    hits = await search_errors(query="", limit=3)
    assert isinstance(hits, list), "search_errors must return a list"
    fams = await list_families()
    assert isinstance(fams, list) and fams, "list_families must return families"
    if hits:
        detail = await get_error(hits[0]["id"])
        assert "how_to_fix" in detail, "get_error must return the FE-shaped fix"
        md = _format_fix(detail)
        assert md.startswith("#"), "resource markdown must render"
        assert "search_errors" in diagnose("boom"), "prompt must reference the tools"
    print(f"✅ selftest ok — {len(hits)} sample hit(s), {len(fams)} families")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        import asyncio
        asyncio.run(_selftest())
    else:
        print(f"🧩 Error DNA MCP on http://{mcp.settings.host}:{mcp.settings.port}/mcp")
        mcp.run(transport="streamable-http")
