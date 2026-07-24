"""search_errors — business logic (keyword-only compact hits)."""

from db import read


async def handle(
    query: str = "",
    family: str = "",
    type: str = "",
    limit: int = 20,
) -> list[dict]:
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
    sql = (
        f"SELECT id, source_id, title, family, type, issue FROM summaries "
        f"WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?"
    )
    rows = await read(sql, params + [limit])
    return [{
        "id": r["id"],
        "note_number": r["source_id"],
        "title": r["title"],
        "family": r["family"],
        "type": r["type"],
        "snippet": (r["issue"] or "")[:200],
    } for r in rows]
