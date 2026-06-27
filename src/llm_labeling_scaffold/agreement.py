from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import TaskConfig
from .io import iter_jsonl, read_json, write_json


_MISSING_SUBMITTER = "__missing_submitter__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _canonical_row(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_jsonl_with_hash(path: str | Path) -> tuple[list[dict], str]:
    rows: list[dict] = []
    digest = hashlib.sha256()
    for row in iter_jsonl(path):
        rows.append(row)
        digest.update(_canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
    return rows, digest.hexdigest()


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _submitter_id(row: dict) -> str | None:
    for key in ("user_id", "submitted_by", "annotator_id", "annotator", "worker_id", "reviewer_id", "user"):
        value = row.get(key)
        if _present(value):
            return str(value)
    return None


def _primary_value(row: dict, primary_label: str) -> Any:
    human_label = row.get("human_label")
    if isinstance(human_label, dict) and primary_label in human_label:
        return human_label.get(primary_label)
    if primary_label in row:
        return row.get(primary_label)
    return None


def _sorted_ids(values: set[str]) -> list[str]:
    return sorted(values, key=lambda item: (str(item)))


def build_agreement_summary(
    task: TaskConfig,
    sample_path: str | Path,
    decisions_path: str | Path,
    *,
    audit_id: str,
    min_submitted: int = 1,
) -> dict[str, Any]:
    sample_file = Path(sample_path)
    decisions_file = Path(decisions_path)
    if not sample_file.is_file():
        raise ValueError(f"样本文件不存在: {sample_file}")
    if not decisions_file.is_file():
        raise ValueError(f"标注决策文件不存在: {decisions_file}")
    min_submitted = int(min_submitted)
    if min_submitted < 1:
        raise ValueError("min_submitted 必须大于 0")

    sample_rows, sample_sha256 = _load_jsonl_with_hash(sample_file)
    decision_rows, decisions_sha256 = _load_jsonl_with_hash(decisions_file)
    id_field = task.id_field
    primary_label = task.primary_label["name"]

    sample_ids: set[str] = set()
    sample_duplicate_ids: set[str] = set()
    sample_missing_id_rows: list[int] = []
    for row_no, row in enumerate(sample_rows, start=1):
        value = row.get(id_field)
        if not _present(value):
            sample_missing_id_rows.append(row_no)
            continue
        record_id = str(value)
        if record_id in sample_ids:
            sample_duplicate_ids.add(record_id)
        sample_ids.add(record_id)

    decision_entries: list[dict[str, Any]] = []
    decision_missing_id_rows: list[int] = []
    unknown_ids: set[str] = set()
    covered_ids: set[str] = set()
    submission_rows: dict[tuple[str, str], list[int]] = defaultdict(list)
    submitters_by_id: dict[str, set[str]] = defaultdict(set)

    for row_no, row in enumerate(decision_rows, start=1):
        raw_id = row.get(id_field)
        submitter = _submitter_id(row) or _MISSING_SUBMITTER
        primary_value = _primary_value(row, primary_label)
        entry = {
            "row": row_no,
            "id": str(raw_id) if _present(raw_id) else None,
            "submitter": None if submitter == _MISSING_SUBMITTER else submitter,
            "submitter_key": submitter,
            "primary_value": primary_value,
            "primary_present": _present(primary_value),
        }
        decision_entries.append(entry)
        if not _present(raw_id):
            decision_missing_id_rows.append(row_no)
            continue
        record_id = str(raw_id)
        if record_id not in sample_ids:
            unknown_ids.add(record_id)
            continue
        covered_ids.add(record_id)
        submission_rows[(record_id, submitter)].append(row_no)
        submitters_by_id[record_id].add(submitter)

    duplicate_submissions: list[dict[str, Any]] = []
    duplicate_followup_rows: set[int] = set()
    for (record_id, submitter), rows in sorted(submission_rows.items(), key=lambda item: (item[0][0], item[0][1])):
        if len(rows) <= 1:
            continue
        duplicate_submissions.append(
            {
                "id": record_id,
                "submitter": None if submitter == _MISSING_SUBMITTER else submitter,
                "rows": rows,
            }
        )
        duplicate_followup_rows.update(rows[1:])

    label_counts: Counter[str] = Counter()
    primary_label_missing: list[dict[str, Any]] = []
    primary_label_missing_ids: set[str] = set()
    for entry in decision_entries:
        record_id = entry["id"]
        if record_id is None or record_id not in sample_ids:
            continue
        if not entry["primary_present"]:
            primary_label_missing.append(
                {
                    "id": record_id,
                    "row": entry["row"],
                    "submitter": entry["submitter"],
                }
            )
            primary_label_missing_ids.add(record_id)
            continue
        if entry["row"] not in duplicate_followup_rows:
            label_counts[str(entry["primary_value"])] += 1

    missing_sample_ids = sample_ids - covered_ids
    below_min_submitted_ids = {
        record_id
        for record_id in sample_ids
        if len(submitters_by_id.get(record_id, set())) < min_submitted
    }
    issue_counts = {
        "sample_missing_id_rows": len(sample_missing_id_rows),
        "sample_duplicate_ids": len(sample_duplicate_ids),
        "decision_missing_id_rows": len(decision_missing_id_rows),
        "unknown_ids": len(unknown_ids),
        "duplicate_submissions": len(duplicate_submissions),
        "primary_label_missing": len(primary_label_missing),
        "below_min_submitted_ids": len(below_min_submitted_ids),
    }
    passed = not any(issue_counts.values())
    coverage_rate = (len(covered_ids) / len(sample_ids)) if sample_ids else 0.0
    summary = {
        "schema_version": 1,
        "task_id": task.task_id,
        "audit_id": audit_id,
        "created_at": _now(),
        "sample_path": str(sample_file),
        "decisions_path": str(decisions_file),
        "id_field": id_field,
        "primary_label": primary_label,
        "min_submitted": min_submitted,
        "input_fingerprint": {
            "sample_path": str(sample_file),
            "sample_sha256": sample_sha256,
            "decisions_path": str(decisions_file),
            "decisions_sha256": decisions_sha256,
            "min_submitted": min_submitted,
        },
        "sample_rows": len(sample_rows),
        "sample_unique_ids": len(sample_ids),
        "decision_rows": len(decision_rows),
        "sample_coverage": {
            "sample_ids": len(sample_ids),
            "covered_ids": len(covered_ids),
            "missing_ids": _sorted_ids(missing_sample_ids),
            "coverage_rate": coverage_rate,
        },
        "submitted_counts": {
            record_id: len(submitters_by_id.get(record_id, set()))
            for record_id in _sorted_ids(sample_ids)
        },
        "sample_missing_id_rows": sample_missing_id_rows,
        "sample_duplicate_ids": _sorted_ids(sample_duplicate_ids),
        "decision_missing_id_rows": decision_missing_id_rows,
        "unknown_ids": _sorted_ids(unknown_ids),
        "duplicate_submissions": duplicate_submissions,
        "primary_label_missing": primary_label_missing,
        "primary_label_missing_ids": _sorted_ids(primary_label_missing_ids),
        "below_min_submitted_ids": _sorted_ids(below_min_submitted_ids),
        "label_distribution": dict(sorted(label_counts.items())),
        "issue_counts": issue_counts,
        "passed": passed,
    }
    return summary


def write_agreement_audit(
    task: TaskConfig,
    sample_path: str | Path,
    decisions_path: str | Path,
    audit_id: str,
    *,
    min_submitted: int = 1,
) -> dict[str, Any]:
    audit_id = str(audit_id or "").strip()
    if not _safe_segment(audit_id):
        raise ValueError("一致性检查编号只能使用单段名称，不能包含路径分隔符")

    summary = build_agreement_summary(
        task,
        sample_path,
        decisions_path,
        audit_id=audit_id,
        min_submitted=min_submitted,
    )
    audit_dir = task.runs_dir / "agreement_audits" / audit_id
    summary_path = audit_dir / "summary.json"
    if audit_dir.exists():
        if not summary_path.exists():
            raise ValueError(f"一致性检查编号已存在但缺少 summary.json: {audit_id}")
        existing = read_json(summary_path)
        if existing.get("input_fingerprint") == summary.get("input_fingerprint"):
            return {
                "audit_id": audit_id,
                "summary_path": str(summary_path),
                "summary": existing,
                "action": "reused",
                "idempotent": True,
            }
        raise ValueError(f"一致性检查编号已存在且输入或参数不同: {audit_id}。请使用新的 audit_id，系统不会覆盖已有数据。")

    write_json(summary, summary_path)
    return {
        "audit_id": audit_id,
        "summary_path": str(summary_path),
        "summary": summary,
        "action": "created",
        "idempotent": False,
    }


def audit_agreement(
    task: TaskConfig,
    sample_path: str | Path,
    decisions_path: str | Path,
    audit_id: str,
    *,
    min_submitted: int = 1,
) -> dict[str, Any]:
    return write_agreement_audit(
        task,
        sample_path,
        decisions_path,
        audit_id,
        min_submitted=min_submitted,
    )
