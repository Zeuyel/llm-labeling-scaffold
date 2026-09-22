from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import TaskConfig
from .io import read_json, read_jsonl, sha256_file, write_json, write_jsonl
from .schema import build_output_schema, validate_row_light


def _constraint_errors(row: dict, constraints: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    for c in constraints:
        left = c.get("if", "")
        right = c.get("then", "")
        if "==" not in left or "==" not in right:
            continue
        lf, lv = [part.strip().strip("'\"") for part in left.split("==", 1)]
        rf, rv = [part.strip().strip("'\"") for part in right.split("==", 1)]
        actual_l = row.get(lf)
        try:
            lv_cast = int(lv)
        except ValueError:
            lv_cast = lv
        if actual_l == lv_cast and str(row.get(rf)) != rv:
            errors.append(f"constraint failed: {left} -> {right}")
    return errors


def validate_run_inputs(run_dir: str | Path) -> dict[str, Any]:
    run = Path(run_dir)
    input_dir = run / "input"
    manifest_path = input_dir / "manifest.json"
    errors: list[str] = []
    evidence: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {}
    if not manifest_path.is_file():
        return {
            "valid": False,
            "manifest_path": str(manifest_path),
            "manifest_sha256": None,
            "batches": evidence,
            "errors": ["缺少批次 manifest.json"],
        }
    try:
        manifest = read_json(manifest_path)
    except Exception as exc:
        return {
            "valid": False,
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "batches": evidence,
            "errors": [f"无法读取批次 manifest.json: {exc}"],
        }
    if not isinstance(manifest, dict):
        errors.append("批次 manifest 必须是 object")
        manifest = {}

    entries = manifest.get("batches")
    if not isinstance(entries, list):
        errors.append("批次 manifest 缺少 batches 列表")
        entries = []
    batch_count = manifest.get("batch_count")
    if isinstance(batch_count, bool) or not isinstance(batch_count, int) or batch_count < 0:
        errors.append("批次 manifest 缺少有效 batch_count")
    elif batch_count != len(entries):
        errors.append("批次 manifest 的 batch_count 与 batches 条目数不一致")
    expected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("批次 manifest 包含非法批次条目")
            continue
        name = entry.get("batch")
        if not isinstance(name, str) or Path(name).name != name or not name.startswith("batch_"):
            errors.append(f"批次 manifest 包含非法文件名: {name!r}")
            continue
        if name in expected:
            errors.append(f"批次 manifest 包含重复批次: {name}")
            continue
        if not isinstance(entry.get("rows"), int) or entry["rows"] < 0:
            errors.append(f"批次 manifest 缺少有效行数: {name}")
        if not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != 64:
            errors.append(f"批次 manifest 缺少有效哈希: {name}")
        expected[name] = entry

    sample = manifest.get("sample")
    sample_sha256 = manifest.get("sample_sha256")
    sample_rows = manifest.get("sample_rows")
    if isinstance(sample_rows, bool) or not isinstance(sample_rows, int) or sample_rows < 0:
        errors.append("批次 manifest 缺少有效 sample_rows")
    if not isinstance(sample, str) or not sample:
        errors.append("批次 manifest 缺少样本路径")
    elif not isinstance(sample_sha256, str) or len(sample_sha256) != 64:
        errors.append("批次 manifest 缺少样本哈希")
    else:
        sample_path = Path(sample)
        if not sample_path.is_file():
            errors.append(f"样本文件不存在: {sample}")
        else:
            if sha256_file(sample_path) != sample_sha256:
                errors.append("样本文件哈希与批次 manifest 不一致")
            try:
                actual_sample_rows = len(read_jsonl(sample_path))
                if sample_rows != actual_sample_rows:
                    errors.append("样本行数与批次 manifest 不一致")
            except Exception as exc:
                errors.append(f"无法读取样本文件 {sample}: {exc}")

    batch_dir = input_dir / "batches"
    actual = {path.name for path in batch_dir.glob("batch_*.jsonl")} if batch_dir.is_dir() else set()
    for name in sorted(set(expected) - actual):
        errors.append(f"缺少已登记批次文件: {name}")
    for name in sorted(actual - set(expected)):
        errors.append(f"存在未登记批次文件: {name}")
    for name, entry in expected.items():
        path = batch_dir / name
        item: dict[str, Any] = {
            "batch": name,
            "path": str(path),
            "expected_rows": entry.get("rows"),
            "expected_sha256": entry.get("sha256"),
        }
        if not path.is_file():
            item["valid"] = False
            evidence.append(item)
            continue
        try:
            rows = read_jsonl(path)
            item["rows"] = len(rows)
            item["sha256"] = sha256_file(path)
            item["valid"] = item["rows"] == entry.get("rows") and item["sha256"] == entry.get("sha256")
            if item["rows"] != entry.get("rows"):
                errors.append(f"批次行数与 manifest 不一致: {name}")
            if item["sha256"] != entry.get("sha256"):
                errors.append(f"批次哈希与 manifest 不一致: {name}")
        except Exception as exc:
            item["valid"] = False
            item["error"] = str(exc)
            errors.append(f"无法读取批次文件 {name}: {exc}")
        evidence.append(item)
    return {
        "valid": not errors,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "sample": sample,
        "sample_sha256": sample_sha256,
        "sample_rows": sample_rows,
        "batches": evidence,
        "errors": errors,
    }


def audit_run(task: TaskConfig, run_dir: str | Path) -> dict:
    run = Path(run_dir)
    audit_dir = run / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    input_evidence = validate_run_inputs(run)
    schema = build_output_schema(task)
    item_schema = schema["properties"]["results"]["items"]
    summaries = []
    for input_item in input_evidence["batches"]:
        batch_file = str(input_item["batch"])
        batch_name = Path(batch_file).stem
        batch_path = run / "input" / "batches" / batch_file
        input_rows = read_jsonl(batch_path) if input_item.get("valid") else []
        input_ids = [str(row[task.id_field]) for row in input_rows]
        output_path = run / "llm" / f"{batch_name}.json"
        payload = read_json(output_path) if output_path.exists() else {"results": []}
        output_rows = payload.get("results", [])
        seen: dict[str, list[dict]] = {}
        schema_errors = []
        constraint_errors = []
        for row in output_rows:
            rid = str(row.get(task.id_field, ""))
            seen.setdefault(rid, []).append(row)
            for err in validate_row_light(row, item_schema):
                schema_errors.append({task.id_field: rid, "error": err, "row": row})
            for err in _constraint_errors(row, task.constraints):
                constraint_errors.append({task.id_field: rid, "error": err, "row": row})
        missing_ids = [rid for rid in input_ids if rid not in seen]
        duplicate_rows = [row for rows in seen.values() if len(rows) > 1 for row in rows]
        duplicate_conflicts = 0
        primary = task.primary_label["name"]
        for rows in seen.values():
            if len({r.get(primary) for r in rows}) > 1:
                duplicate_conflicts += 1
        missing = [row for row in input_rows if str(row[task.id_field]) in set(missing_ids)]
        summary = {
            "batch_name": batch_name,
            "input_rows": len(input_rows),
            "input_sha256": input_item.get("sha256"),
            "output_rows": len(output_rows),
            "output_sha256": sha256_file(output_path) if output_path.exists() else None,
            "missing_count": len(missing_ids),
            "duplicate_row_count": len(duplicate_rows),
            "duplicate_conflict_id_count": duplicate_conflicts,
            "schema_error_count": len(schema_errors),
            "constraint_error_count": len(constraint_errors),
            "input_integrity_valid": bool(input_item.get("valid")),
            "is_clean": bool(input_item.get("valid")) and not missing_ids and not duplicate_rows and not schema_errors and not constraint_errors,
            "is_usable": bool(input_item.get("valid")) and duplicate_conflicts == 0 and not schema_errors and not constraint_errors,
        }
        summaries.append(summary)
        write_json(summary, audit_dir / f"{batch_name}.summary.json")
        write_jsonl(missing, audit_dir / f"{batch_name}.missing.jsonl")
        write_jsonl(duplicate_rows, audit_dir / f"{batch_name}.duplicates.jsonl")
        write_jsonl(schema_errors, audit_dir / f"{batch_name}.schema_errors.jsonl")
        write_jsonl(constraint_errors, audit_dir / f"{batch_name}.constraint_errors.jsonl")
    run_summary = {
        "audited_batches": len(summaries),
        "clean_batches": sum(1 for s in summaries if s["is_clean"]),
        "usable_batches": sum(1 for s in summaries if s["is_usable"]),
        "blocked_batches": sum(1 for s in summaries if not s["is_usable"]),
        "input_evidence": input_evidence,
        "output_fingerprints": [
            {"batch_name": summary["batch_name"], "rows": summary["output_rows"], "sha256": summary["output_sha256"]}
            for summary in summaries
        ],
        "is_usable": bool(input_evidence["valid"]) and all(summary["is_usable"] for summary in summaries),
    }
    write_json(run_summary, audit_dir / "run_summary.json")
    return run_summary
