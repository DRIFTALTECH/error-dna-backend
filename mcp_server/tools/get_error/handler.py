"""get_error — business logic + markdown helper for the error:// resource."""

from db import read
from routes.summaries import _summary_to_ui, _resolve_attachments


async def handle(id: int) -> dict:
    rows = await read("SELECT * FROM summaries WHERE id = ?", (id,))
    if not rows:
        return {"error": f"No summary with id {id}"}
    ui = _summary_to_ui(rows[0])
    ui["attachments"] = _resolve_attachments(rows[0].get("attachments"))
    return ui


def format_fix(d: dict) -> str:
    """Render a get_error dict as readable markdown (for the resource)."""
    def section(head, body):
        return f"## {head}\n{body}\n\n" if body else ""

    fixes = "\n".join(f"{i}. {s}" for i, s in enumerate(d.get("how_to_fix") or [], 1))
    gotchas = "\n".join(
        f"- **{g['name']}:** {g['description']}" for g in (d.get("gotchas") or [])
    )
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
