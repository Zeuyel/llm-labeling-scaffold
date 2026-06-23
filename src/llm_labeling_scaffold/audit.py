from __future__ import annotations

from pathlib import Path

from .config import TaskConfig
from .io import read_json, read_jsonl, write_json, write_jsonl
from .schema import build_output_schema, validate_row_light


def _constraint_errors(row: dict, constraints: list[dict[str, str]]) -> list[str]:
    # MVP supports the common implication form: if field == literal then field == literal.
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


def audit_run(task: TaskConfig, run_dir: str | Path) -> dict:
    run = Path(run_dir)
    audit_dir = run / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    schema = build_output_schema(task)
    item_schema = schema["properties"]["results"]["items"]
    summaries = []
    for batch_path in sorted((run / "input" / "batches").glob("batch_*.jsonl")):
        batch_name = batch_path.stem
        input_rows = read_jsonl(batch_path)
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
            "output_rows": len(output_rows),
            "missing_count": len(missing_ids),
            "duplicate_row_count": len(duplicate_rows),
            "duplicate_conflict_id_count": duplicate_conflicts,
            "schema_error_count": len(schema_errors),
            "constraint_error_count": len(constraint_errors),
            "is_clean": not missing_ids and not duplicate_rows and not schema_errors and not constraint_errors,
            "is_usable": duplicate_conflicts == 0 and not schema_errors and not constraint_errors,
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
    }
    write_json(run_summary, audit_dir / "run_summary.json")
    return run_summary
