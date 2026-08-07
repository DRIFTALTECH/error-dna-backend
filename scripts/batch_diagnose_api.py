#!/usr/bin/env python3
"""Batch test POST /api/errors/diagnose for every line in error_signatures."""

import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))

BASE_URL = os.getenv("EDNA_API_BASE", "https://16.113.9.182.sslip.io").rstrip("/")
CLIENT_ID = os.getenv("EDNA_CLIENT_ID", "edna_CURhPRDnPMgI-tKH")
CLIENT_SECRET = os.getenv("EDNA_CLIENT_SECRET", "YSqCl1p06lSv4ex16u7dTh3TmWyfo5u-rMsJ_G_E1tI")
OUT_PATH = ROOT / "data" / "error_signatures_api_report.json"


def parse_signatures(path: Path) -> list[tuple[str, int]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[tuple[str, int]] = []
    i = 0
    while i < len(lines):
        err = lines[i].strip()
        i += 1
        count = int(lines[i].strip()) if i < len(lines) and lines[i].strip().isdigit() else 0
        i += 1
        if err:
            out.append((err, count))
    return out


async def get_token(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"{BASE_URL}/api/oauth/token",
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def diagnose_api(client: httpx.AsyncClient, token: str, error_text: str, source: str) -> dict:
    r = await client.post(
        f"{BASE_URL}/api/errors/diagnose",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"error_text": error_text, "source": source},
        timeout=120.0,
    )
    if r.status_code != 200:
        return {"http_status": r.status_code, "error": r.text[:500]}
    return r.json()


def summarize_row(idx: int, prod_count: int, err: str, data: dict) -> dict:
    top = (data.get("solutions") or [None])[0] if isinstance(data.get("solutions"), list) else None
    return {
        "index": idx,
        "production_count": prod_count,
        "http_status": data.get("http_status", 200),
        "family_code": data.get("family_code"),
        "family_name": data.get("family_name"),
        "distinct_error_id": data.get("distinct_error_id"),
        "title": data.get("title"),
        "is_new_distinct": data.get("is_new_distinct"),
        "informational": data.get("informational"),
        "cluster_confidence": data.get("cluster_confidence", data.get("error_match_percent")),
        "occurrence_count": data.get("occurrence_count"),
        "solutions_count": len(data.get("solutions") or []),
        "top_note": top.get("note_number") if top else None,
        "top_match_pct": top.get("match_percent") if top else None,
        "error_preview": err[:100],
        "api_error": data.get("error"),
    }


async def main() -> None:
    entries = parse_signatures(ROOT / "error_signatures")
    results: list[dict] = []
    cluster_hits: dict[int, list[int]] = defaultdict(list)
    total = len(entries)

    async with httpx.AsyncClient() as client:
        token = await get_token(client)
        print(f"Token OK — testing {total} errors via {BASE_URL}/api/errors/diagnose\n")

        for idx, (err, prod_count) in enumerate(entries, 1):
            print(f"[{idx}/{total}] diagnosing...", flush=True)
            data = await diagnose_api(client, token, err, f"signatures-{idx}")
            row = summarize_row(idx, prod_count, err, data)
            results.append(row)
            if row.get("distinct_error_id"):
                cluster_hits[row["distinct_error_id"]].append(idx)

            if idx % 10 == 0:
                OUT_PATH.write_text(json.dumps({"partial": True, "done": idx, "results": results}, indent=2))

        # Re-send: exact duplicate + first error again
        retest: list[dict] = []
        retest_cases = [
            ("exact_repeat_idx1", entries[0][0]),
            ("exact_repeat_idx1_again", entries[0][0]),
            ("similar_http_statuscode", entries[1][0] if len(entries) > 1 else entries[0][0]),
        ]
        # Pick two errors that landed in same cluster if any
        multi = [cid for cid, ids in cluster_hits.items() if len(ids) >= 2]
        if multi:
            cid = multi[0]
            j = cluster_hits[cid][1]
            retest_cases.append((f"similar_same_cluster_{cid}", entries[j - 1][0]))

        print("\n--- RETEST (same / similar) ---")
        for label, text in retest_cases:
            print(f"  {label}...", flush=True)
            data = await diagnose_api(client, token, text, f"retest-{label}")
            row = summarize_row(-1, 0, text, data)
            row["retest_label"] = label
            retest.append(row)
            print(
                f"    cluster={row.get('distinct_error_id')} "
                f"new={row.get('is_new_distinct')} "
                f"confidence={row.get('cluster_confidence')}% "
                f"occ={row.get('occurrence_count')}",
            )

    families = Counter(r.get("family_code") for r in results if r.get("family_code"))
    new_count = sum(1 for r in results if r.get("is_new_distinct"))
    matched = sum(1 for r in results if (r.get("cluster_confidence") or 0) >= 70)
    with_solutions = sum(1 for r in results if (r.get("solutions_count") or 0) > 0)
    merged_clusters = {cid: ids for cid, ids in cluster_hits.items() if len(ids) > 1}

    report = {
        "generated_at": datetime.now(IST).isoformat(),
        "api_base": BASE_URL,
        "total_tested": total,
        "summary": {
            "new_clusters_on_first_pass": new_count,
            "rematched_existing_ge_70pct": matched,
            "with_solution_notes": with_solutions,
            "unique_families": len(families),
            "unique_clusters_used": len(cluster_hits),
            "clusters_with_multiple_signatures": len(merged_clusters),
        },
        "family_distribution": dict(families.most_common()),
        "merged_clusters": {str(k): v for k, v in sorted(merged_clusters.items(), key=lambda x: -len(x[1]))[:30]},
        "retest": retest,
        "results": results,
    }
    OUT_PATH.write_text(json.dumps(report, indent=2))
    print(f"\nReport → {OUT_PATH}")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
