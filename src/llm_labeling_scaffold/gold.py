from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile

from .config import TaskConfig
from .audit import validate_run_inputs
from .io import iter_jsonl, read_json, read_jsonl, sha256_file, write_json, write_jsonl


def _row_id(task: TaskConfig, row: dict, source: str) -> str:
    if not isinstance(row, dict):
        raise ValueError(f"{source} 必须由 JSON object 组成")
    value = row.get(task.id_field)
    if value in (None, ""):
        raise ValueError(f"{source} 缺少 ID 字段 {task.id_field}")
    return str(value)


def _unique_rows(task: TaskConfig, rows, source: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        record_id = _row_id(task, row, source)
        if record_id in result:
            raise ValueError(f"{source} 存在重复 ID: {record_id}")
        result[record_id] = dict(row)
    return result


def _validate_gold_rows(task: TaskConfig, rows: list[dict]) -> None:
    primary = task.primary_label["name"]
    seen: set[str] = set()
    for row in rows:
        record_id = _row_id(task, row, "gold")
        if record_id in seen:
            raise ValueError(f"gold 存在重复 ID: {record_id}")
        seen.add(record_id)
        if row.get(primary) in (None, ""):
            raise ValueError(f"gold 记录 {record_id} 缺少主标签: {primary}")


def validate_gold_artifact(task: TaskConfig, version: str) -> dict:
    out = task.runs_dir / "gold"
    gold_path = out / f"gold_{version}.jsonl"
    manifest_path = out / f"gold_{version}.manifest.json"
    if not gold_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f"gold 版本产物不完整: {version}")
    manifest = read_json(manifest_path)
    if manifest.get("task_id") != task.task_id or manifest.get("version") != version:
        raise ValueError(f"gold manifest 标识不匹配: {version}")
    if manifest.get("data_sha256") != sha256_file(gold_path):
        raise ValueError(f"gold 内容哈希与质量证明不一致: {version}")
    rows = read_jsonl(gold_path)
    if manifest.get("rows") != len(rows):
        raise ValueError(f"gold 行数与质量证明不一致: {version}")
    _validate_gold_rows(task, rows)
    return manifest


def _source_rows_from_batches(task: TaskConfig, batch_paths) -> dict[str, dict]:
    source_rows: dict[str, dict] = {}
    for batch_path in batch_paths:
        batch_rows = _unique_rows(task, iter_jsonl(batch_path), f"批次 {batch_path.name}")
        for record_id, row in batch_rows.items():
            if record_id in source_rows:
                if source_rows[record_id] != row:
                    raise ValueError(f"输入批次跨批重复 ID 内容不一致: {record_id}")
                continue
            source_rows[record_id] = row
    return source_rows


def _write_gold_files(
    task: TaskConfig,
    version: str,
    rows: list[dict],
    manifest_extra: dict,
) -> Path:
    _validate_gold_rows(task, rows)
    out = task.runs_dir / "gold"
    out.mkdir(parents=True, exist_ok=True)
    gold_path = out / f"gold_{version}.jsonl"
    manifest_path = out / f"gold_{version}.manifest.json"
    data_card_path = out / f"gold_{version}.data_card.md"
    existing = [path for path in (gold_path, manifest_path, data_card_path) if path.exists()]
    if existing:
        if gold_path.is_file() and manifest_path.is_file():
            validate_gold_artifact(task, version)
        raise FileExistsError(
            "gold 版本产物已存在，拒绝覆盖: "
            + ", ".join(str(path) for path in existing)
        )
    primary = task.primary_label["name"]
    counts = Counter(str(row.get(primary)) for row in rows)
    data_card = (
        "# 训练集说明\n\n"
        f"- 任务：`{task.task_id}`\n"
        f"- 版本：`{version}`\n"
        f"- 行数：`{len(rows)}`\n"
        f"- 主标签：`{primary}`\n"
        f"- 标签分布：`{dict(counts)}`\n"
    )
    staging = Path(tempfile.mkdtemp(prefix=f".gold_{version}_", dir=out))
    staged_paths = {
        gold_path: staging / gold_path.name,
        manifest_path: staging / manifest_path.name,
        data_card_path: staging / data_card_path.name,
    }
    published: list[Path] = []
    try:
        write_jsonl(rows, staged_paths[gold_path])
        manifest = {
            "task_id": task.task_id,
            "version": version,
            "path": str(gold_path),
            "rows": len(rows),
            "unique_ids": len({str(row[task.id_field]) for row in rows if task.id_field in row}),
            "primary_label": primary,
            "label_counts": dict(counts),
            "data_sha256": sha256_file(staged_paths[gold_path]),
            "created_at": datetime.now(timezone.utc).isoformat(),
            **manifest_extra,
        }
        write_json(manifest, staged_paths[manifest_path])
        staged_paths[data_card_path].write_text(data_card, encoding="utf-8")
        for final_path, staged_path in staged_paths.items():
            if final_path.exists():
                raise FileExistsError(f"gold 版本产物已存在，拒绝覆盖: {final_path}")
            os.link(staged_path, final_path)
            published.append(final_path)
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return gold_path


def build_gold(task: TaskConfig, run_dir: str | Path, version: str, decisions: str | Path | None = None) -> Path:
    run = Path(run_dir)
    input_evidence = validate_run_inputs(run)
    if not input_evidence["valid"]:
        raise ValueError("批次输入证据未通过，不能构建 gold")
    merge_path = run / "merged" / "merge_summary.json"
    if not merge_path.is_file():
        raise ValueError("缺少 merge_summary.json，不能构建 gold")
    merge_summary = read_json(merge_path)
    merged_path = run / "merged" / "merged_clean.jsonl"
    if merge_summary.get("is_usable") is not True:
        raise ValueError("合并结果未通过质量与血缘检查，不能构建 gold")
    if merge_summary.get("input_evidence") != input_evidence:
        raise ValueError("合并输入证据与当前批次 manifest 不一致")
    if not merged_path.is_file() or merge_summary.get("merged_sha256") != sha256_file(merged_path):
        raise ValueError("合并结果内容与血缘证明不一致")

    source_rows = _source_rows_from_batches(
        task,
        sorted((run / "input" / "batches").glob("batch_*.jsonl")),
    )

    rows: dict[str, dict] = {}
    merged_rows = _unique_rows(task, iter_jsonl(merged_path), "merged_clean")
    for rid, label_row in merged_rows.items():
        merged = dict(source_rows.get(rid, {}))
        merged.update(label_row)
        merged[task.id_field] = rid
        rows[rid] = merged

    if decisions:
        decision_rows = _unique_rows(task, iter_jsonl(decisions), "decision artifact")
        for rid, decision in decision_rows.items():
            if rid not in rows:
                raise ValueError(f"decision artifact 存在未知 ID: {rid}")
            patch = decision.get("human_label", {})
            if not isinstance(patch, dict):
                raise ValueError(f"decision artifact {rid} 的 human_label 必须是 object")
            rows[rid].update(patch)
            rows[rid]["gold_source"] = "human_override"
    gold_rows = list(rows.values())
    return _write_gold_files(
        task,
        version,
        gold_rows,
        {
            "source": "run",
            "run_dir": str(run),
            "decisions": str(decisions) if decisions else None,
            "decisions_sha256": sha256_file(decisions) if decisions else None,
            "input_manifest_sha256": input_evidence["manifest_sha256"],
            "merged_sha256": merge_summary["merged_sha256"],
        },
    )


def build_gold_from_decisions(
    task: TaskConfig,
    sample_path: str | Path,
    decisions_path: str | Path,
    version: str,
) -> Path:
    source_rows = _unique_rows(task, read_jsonl(sample_path), "样本")
    rows: dict[str, dict] = {}
    decision_rows = _unique_rows(task, iter_jsonl(decisions_path), "decision artifact")
    for rid, decision in decision_rows.items():
        if rid not in source_rows:
            raise ValueError(f"decision artifact 存在未知 ID: {rid}")
        row = dict(source_rows[rid])
        patch = decision.get("human_label", {})
        if not isinstance(patch, dict):
            raise ValueError(f"decision artifact {rid} 的 human_label 必须是 object")
        row.update(patch)
        row[task.id_field] = rid
        row["gold_source"] = decision.get("source", "argilla")
        rows[rid] = row
    return _write_gold_files(
        task,
        version,
        list(rows.values()),
        {
            "source": "decision_artifact",
            "sample_path": str(sample_path),
            "decisions": str(decisions_path),
            "sample_sha256": sha256_file(sample_path),
            "decisions_sha256": sha256_file(decisions_path),
        },
    )
