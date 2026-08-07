"""list_families — business logic."""

from db import read
from services.error_families import FAMILY_JOIN


async def handle() -> list[dict]:
    return await read(
        f"""SELECT f.code, f.family_name AS name, f.description, f.color, f.severity,
                  COUNT(s.id) AS fix_count
           FROM error_families f
           LEFT JOIN summaries s ON s.is_latest = 1 AND {FAMILY_JOIN}
           GROUP BY f.code, f.family_name, f.description, f.color, f.severity
           ORDER BY fix_count DESC""",
    )
