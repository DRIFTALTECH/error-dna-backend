"""list_families — business logic."""

from db import read


async def handle() -> list[dict]:
    return await read(
        """SELECT f.family_name AS name, f.description, f.color,
                  COUNT(s.id) AS fix_count
           FROM error_families f
           LEFT JOIN summaries s ON s.family = f.family_name AND s.is_latest = 1
           GROUP BY f.family_name, f.description, f.color ORDER BY fix_count DESC"""
    )
