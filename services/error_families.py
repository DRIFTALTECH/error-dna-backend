"""Error family catalog — CSV seed, pattern classify, LLM catalog."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from db import read, write

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "error_families.csv"

_COLORS = {
    "critical": "#f85149",
    "high": "#d29922",
    "medium": "#58a6ff",
    "low": "#8b949e",
}

# ponytail: cache catalog strings in-process; CSV changes need restart
_catalog_cache: str | None = None
_codes_cache: set[str] | None = None


def _parse_patterns(raw: str) -> str:
    """Normalize match_patterns cell to JSON array text."""
    raw = (raw or "").strip()
    if not raw:
        return "[]"
    if raw.startswith("["):
        return raw
    return json.dumps([p.strip() for p in raw.split(",") if p.strip()])


async def seed_families() -> int:
    """Upsert all rows from data/error_families.csv. Returns count upserted."""
    if not CSV_PATH.is_file():
        return 0
    n = 0
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            if not code:
                continue
            name = (row.get("name") or code).strip()
            severity = (row.get("severity") or "medium").strip().lower()
            desc = (row.get("description") or "").strip()
            patterns = _parse_patterns(row.get("match_patterns") or "[]")
            try:
                priority = int(row.get("match_priority") or 999000)
            except ValueError:
                priority = 999000
            color = _COLORS.get(severity, "#58a6ff")
            await write(
                """INSERT INTO error_families
                   (code, family_name, severity, description, match_patterns, match_priority, color)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (code) DO UPDATE SET
                     family_name = EXCLUDED.family_name,
                     severity = EXCLUDED.severity,
                     description = EXCLUDED.description,
                     match_patterns = EXCLUDED.match_patterns,
                     match_priority = EXCLUDED.match_priority,
                     color = EXCLUDED.color""",
                (code, name, severity, desc, patterns, priority, color),
            )
            n += 1
    global _catalog_cache, _codes_cache
    _catalog_cache = None
    _codes_cache = None
    return n


async def list_families() -> list[dict]:
    return await read(
        """SELECT code, family_name, severity, description, match_priority, color, icon
           FROM error_families ORDER BY match_priority ASC, family_name ASC""",
    )


async def get_by_code(code: str) -> dict | None:
    rows = await read("SELECT * FROM error_families WHERE code = ? LIMIT 1", (code,))
    return rows[0] if rows else None


async def valid_codes() -> set[str]:
    global _codes_cache
    if _codes_cache is None:
        rows = await read("SELECT code FROM error_families WHERE code IS NOT NULL")
        _codes_cache = {r["code"] for r in rows}
    return _codes_cache


async def catalog_for_llm() -> str:
    """One line per family for LLM prompts."""
    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    rows = await list_families()
    lines = [f"- {r['code']}: {r['family_name']} — {r.get('description') or ''}" for r in rows]
    _catalog_cache = "\n".join(lines)
    return _catalog_cache


async def classify_text(text: str) -> str:
    """First regex match by match_priority (lowest first). Catch-all .* skipped until end."""
    blob = (text or "").strip()
    if not blob:
        return "UNCLASSIFIED_ERROR"
    rows = await read(
        """SELECT code, match_patterns FROM error_families
           WHERE match_patterns IS NOT NULL ORDER BY match_priority ASC""",
    )
    fallback = "UNCLASSIFIED_ERROR"
    for r in rows:
        code = r["code"]
        try:
            patterns = json.loads(r["match_patterns"] or "[]")
        except json.JSONDecodeError:
            continue
        for pat in patterns:
            if pat in (".*", "^.*$"):
                fallback = code
                continue
            try:
                if re.search(pat, blob, re.I | re.MULTILINE):
                    return code
            except re.error:
                continue
    return fallback


async def code_from_family_field(value: str) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    rows = await read(
        "SELECT code FROM error_families WHERE code = ? OR family_name = ? LIMIT 1",
        (v, v),
    )
    return rows[0]["code"] if rows else None


async def family_display_name(code: str | None) -> str | None:
    row = await get_by_code(code) if code else None
    return row["family_name"] if row else code


FAMILY_JOIN = "(s.family = f.code OR s.family = f.family_name)"
