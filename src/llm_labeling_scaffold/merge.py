from __future__ import annotations

from collections import Counter
from pathlib import Path

from .config import TaskConfig
from .io import read_json, iter_jsonl, write_json, write_jsonl


def merge_run(task: TaskConfig, run_dir: str | Path) -> dict:
    run = Path(run_dir)
    out = run / "merged"
    out.mkdir(parents=True, exist_ok=True)
    merged = []
    missing_pool = []
    duplicate_pool = []
    conflict_pool = []
    seen = set()
    summaries = []
    primary = task.primary_label["name"]
    for summary_path in sorted((run / "audit").glob("batch_*.summary.json")):
        summary = read_json(summary_path)
        summaries.append(summary)
        batch = summary["batch_name"]
        missing_pool.extend(iter_jsonl(run / "audit" / f"{batch}.missing.jsonl"))
        duplicate_pool.extend(iter_jsonl(run / "audit" / f"{batch}.duplicates.jsonl"))
        if not summary.get("is_usable"):
            conflict_pool.extend(iter_jsonl(run / "audit" / f"{batch}.duplicates.jsonl"))
            continue
        payload = read_json(run / "llm" / f"{batch}.json")
        by_id: dict[str, dict] = {}
        for row in payload.get("results", []):
            rid = str(row[task.id_field])
            if rid not in by_id:
                by_id[rid] = row
            elif by_id[rid].get(primary) != row.get(primary):
                conflict_pool.append(row)
        for rid, row in by_id.items():
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(row)
    write_jsonl(merged, out / "merged_clean.jsonl")
    write_jsonl(missing_pool, out / "missing_pool.jsonl")
    write_jsonl(duplicate_pool, out / "duplicate_pool.jsonl")
    write_jsonl(conflict_pool, out / "conflict_pool.jsonl")
    counts = Counter(str(row.get(primary)) for row in merged)
    summary = {
        "audited_batches": len(summaries),
        "clean_batches": sum(1 for s in summaries if s.get("is_clean")),
        "usable_batches": sum(1 for s in summaries if s.get("is_usable")),
        "merged_rows": len(merged),
        "missing_pool_rows": len(missing_pool),
        "duplicate_pool_rows": len(duplicate_pool),
        "conflict_pool_rows": len(conflict_pool),
        "primary_label_field": primary,
        "primary_label_counts": dict(counts),
    }
    write_json(summary, out / "merge_summary.json")
    return summary
