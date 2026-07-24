"""hybrid_search — business logic."""

from services.hybrid_search import hybrid_search as _hybrid


async def handle(query: str, limit: int = 5) -> list[dict]:
    return await _hybrid(query, limit=limit)
