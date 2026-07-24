# search_errors

## Purpose
Keyword-only browse of SAP-note summaries. Prefer `hybrid_search` for diagnosis.

## Input
| Arg | Type | Required | Notes |
|---|---|---|---|
| `query` | string | no | Substring match on title/issue/summary/tags |
| `family` | string | no | Exact family name from `list_families` |
| `type` | string | no | Exact type filter |
| `limit` | int | no | 1–50, default **20** |

## Output
Compact hits: `id`, `note_number`, `title`, `family`, `type`, `snippet`.  
Expand a hit with `get_error(id)`.

## See also
- `hybrid_search` — full summaries + match_percent
- `list_families` — valid family names
