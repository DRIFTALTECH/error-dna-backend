# get_error

## Purpose
Fetch one full SAP-note summary by internal numeric `id`.

## Input
| Arg | Type | Required | Notes |
|---|---|---|---|
| `id` | int | yes | Internal summary id from `search_errors` — **not** the SAP note number |

## Output
Full UI-shaped fix: `the_problem`, `whats_going_on`, `how_to_fix`, `gotchas`, `tags`, `environment`, `note_number`, `version`, …  
Or `{"error": "..."}` if missing.

## Notes
Also used by the `error://{id}` MCP resource (markdown rendering lives in `handler.format_fix`).

## See also
- `hybrid_search` — usually already returns the full fix
- `search_errors` — source of compact `id`s
