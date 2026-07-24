# hybrid_search

## Purpose
Semantic + keyword search over the Error DNA knowledge base. Preferred tool for diagnosing an error.

## Input
| Arg | Type | Required | Notes |
|---|---|---|---|
| `query` | string | yes | Error text / symptom / code phrase |
| `limit` | int | no | 1–20, default **5** |

## Output
List of hits (up to `limit`). Each hit:
- Full summary fields (`title`, `the_problem`, `whats_going_on`, `how_to_fix`, `gotchas`, `tags`, `note_number`, …)
- `match_percent` (0–100)
- `images` (URL map; empty `{}` for SAP notes)

No `source` field.

## Behavior
1. Embed `query` with Amazon Titan Text Embeddings V2.
2. Vector search on `summary_embeddings` + keyword ILIKE on summaries.
3. Blend scores (70% vector / 30% keyword), return top N full rows.

## See also
- `search_errors` — keyword-only compact browse
- `get_error` — fetch one summary by id
