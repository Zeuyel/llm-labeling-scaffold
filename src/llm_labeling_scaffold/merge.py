from __future__ import annotations

from collections import Counter
from pathlib import Path

from .audit import validate_run_inputs
from .config import TaskConfig
from .io import iter_jsonl, read_json, sha256_file, write_json, write_jsonl


def merge_run(task: TaskConfig, run_dir: str | Path) -> dict:
    run = Path(run_dir)
    out = run / "merged"
    out.mkdir(parents=True, exist_ok=True)
    input_evidence = validate_run_inputs(run)
    audit_path = run / "audit" / "run_summary.json"
    audit_summary = read_json(audit_path) if audit_path.is_file() else {}
    lineage_errors: list[str] = []
    if not audit_path.is_file():
        lineage_errors.append("缺少 audit run_summary.json")
    elif audit_summary.get("input_evidence") != input_evidence:
        lineage_errors.append("审计输入证据与当前批次 manifest 不一致")
    if audit_summary.get("is_usable") is not True:
        lineage_errors.append("审计未通过，不能合并为 gold 候选")

    merged = []
    missing_pool = []
    duplicate_pool = []
    conflict_pool = []
    seen: dict[str, tuple[str, dict]] = {}
    summaries = []
    primary = task.primary_label["name"]
    for input_item in input_evidence["batches"]:
        batch_file = str(input_item["batch"])
        batch = Path(batch_file).stem
        summary_path = run / "audit" / f"{batch}.summary.json"
        if not summary_path.is_file():
            lineage_errors.append(f"缺少批次审计摘要: {batch}")
            continue
        summary = read_json(summary_path)
        summaries.append(summary)
        missing_pool.extend(iter_jsonl(run / "audit" / f"{batch}.missing.jsonl"))
        duplicate_pool.extend(iter_jsonl(run / "audit" / f"{batch}.duplicates.jsonl"))
        output_path = run / "llm" / f"{batch}.json"
        if not output_path.is_file() or summary.get("output_sha256") != sha256_file(output_path):
            lineage_errors.append(f"标注输出指纹与审计摘要不一致: {batch}")
            continue
        if summary.get("input_sha256") != input_item.get("sha256"):
            lineage_errors.append(f"批次输入指纹与审计摘要不一致: {batch}")
            continue
        if not summary.get("is_usable"):
            continue
        payload = read_json(output_path)
        by_id: dict[str, dict] = {}
        for row in payload.get("results", []):
            rid = str(row[task.id_field])
            if rid not in by_id:
                by_id[rid] = row
            elif by_id[rid].get(primary) != row.get(primary):
                conflict_pool.append(
                    {
                        "kind": "within_batch_label_conflict",
                        "record_id": rid,
                        "batch": batch,
                        "existing": by_id[rid],
                        "incoming": row,
                    }
                )
        for rid, row in by_id.items():
            prior = seen.get(rid)
            if prior is None:
                seen[rid] = (batch, row)
                merged.append(row)
                continue
            prior_batch, prior_row = prior
            if prior_row != row:
                conflict_pool.append(
                    {
                        "kind": "cross_batch_content_conflict",
                        "record_id": rid,
                        "first_batch": prior_batch,
                        "second_batch": batch,
                        "first": prior_row,
                        "second": row,
                    }
                )
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
        "merged_sha256": sha256_file(out / "merged_clean.jsonl"),
        "missing_pool_rows": len(missing_pool),
        "duplicate_pool_rows": len(duplicate_pool),
        "conflict_pool_rows": len(conflict_pool),
        "cross_batch_conflict_count": sum(1 for row in conflict_pool if row.get("kind") == "cross_batch_content_conflict"),
        "primary_label_field": primary,
        "primary_label_counts": dict(counts),
        "input_evidence": input_evidence,
        "audit_run_summary_sha256": sha256_file(audit_path) if audit_path.is_file() else None,
        "lineage_errors": lineage_errors,
        "is_usable": not lineage_errors and not conflict_pool,
    }
    write_json(summary, out / "merge_summary.json")
    return summary
