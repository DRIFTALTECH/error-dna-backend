#!/usr/bin/env python3
"""Batch diagnose all error_signatures — writes JSON report."""

import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.error_diagnose import diagnose  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


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


async def main() -> None:
    src = ROOT / "error_signatures"
    out_path = ROOT / "data" / "error_signatures_diagnose_report.json"
    entries = parse_signatures(src)
    results = []
    total = len(entries)

    for idx, (err, prod_count) in enumerate(entries, 1):
        short = err[:90].replace("\n", " ")
        print(f"[{idx}/{total}] {short}...", flush=True)
        try:
            r = await diagnose(err, caller="batch-signatures", source="error_signatures")
            top = r["solutions"][0] if r["solutions"] else None
            results.append({
                "index": idx,
                "production_count": prod_count,
                "family_code": r["family_code"],
                "family_name": r["family_name"],
                "distinct_error_id": r["distinct_error_id"],
                "title": r["title"],
                "is_new_distinct": r["is_new_distinct"],
                "informational": r.get("informational", False),
                "cluster_confidence": r.get("cluster_confidence", r.get("error_match_percent")),
                "solutions_count": len(r["solutions"]),
                "top_solution_note": top.get("note_number") if top else None,
                "top_solution_title": top.get("title") if top else None,
                "top_solution_match": top.get("match_percent") if top else None,
                "error_preview": short,
            })
        except Exception as exc:
            results.append({
                "index": idx,
                "production_count": prod_count,
                "error": str(exc),
                "error_preview": short,
            })
        if idx % 25 == 0:
            out_path.write_text(json.dumps({"partial": True, "results": results}, indent=2))

    families = Counter(r.get("family_code") for r in results if r.get("family_code"))
    clusters = Counter(r.get("distinct_error_id") for r in results if r.get("distinct_error_id"))
    new_count = sum(1 for r in results if r.get("is_new_distinct"))
    info_count = sum(1 for r in results if r.get("informational"))
    matched = sum(1 for r in results if (r.get("cluster_confidence") or 0) >= 70)
    with_solutions = sum(1 for r in results if (r.get("solutions_count") or 0) > 0)
    errors = sum(1 for r in results if r.get("error"))

    report = {
        "generated_at": datetime.now(IST).isoformat(),
        "total_tested": total,
        "summary": {
            "new_clusters_created": new_count,
            "matched_existing_cluster_ge_70pct": matched,
            "informational": info_count,
            "with_solution_notes": with_solutions,
            "failed": errors,
            "unique_families": len(families),
            "unique_clusters": len(clusters),
        },
        "family_distribution": dict(families.most_common()),
        "top_clusters": dict(clusters.most_common(20)),
        "results": results,
    }
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nDone → {out_path}")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
