from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import TaskConfig
from .io import iter_jsonl, read_jsonl, write_json, write_jsonl


def _write_gold_files(
    task: TaskConfig,
    version: str,
    rows: list[dict],
    manifest_extra: dict,
) -> Path:
    out = task.runs_dir / "gold"
    out.mkdir(parents=True, exist_ok=True)
    gold_path = out / f"gold_{version}.jsonl"
    write_jsonl(rows, gold_path)
    primary = task.primary_label["name"]
    counts = Counter(str(row.get(primary)) for row in rows)
    manifest = {
        "task_id": task.task_id,
        "version": version,
        "path": str(gold_path),
        "rows": len(rows),
        "unique_ids": len({str(row[task.id_field]) for row in rows if task.id_field in row}),
        "primary_label": primary,
        "label_counts": dict(counts),
        "created_at": datetime.now(timezone.utc).isoformat(),
        **manifest_extra,
    }
    write_json(manifest, out / f"gold_{version}.manifest.json")
    (out / f"gold_{version}.data_card.md").write_text(
        "# 训练集说明\n\n"
        f"- 任务：`{task.task_id}`\n"
        f"- 版本：`{version}`\n"
        f"- 行数：`{len(rows)}`\n"
        f"- 主标签：`{primary}`\n"
        f"- 标签分布：`{dict(counts)}`\n",
        encoding="utf-8",
    )
    return gold_path


def build_gold(task: TaskConfig, run_dir: str | Path, version: str, decisions: str | Path | None = None) -> Path:
    run = Path(run_dir)

    # Start from source records so gold rows keep text and metadata fields for local training.
    source_rows: dict[str, dict] = {}
    for batch_path in sorted((run / "input" / "batches").glob("batch_*.jsonl")):
        for row in iter_jsonl(batch_path):
            source_rows[str(row[task.id_field])] = dict(row)

    rows: dict[str, dict] = {}
    for label_row in iter_jsonl(run / "merged" / "merged_clean.jsonl"):
        rid = str(label_row[task.id_field])
        merged = dict(source_rows.get(rid, {}))
        merged.update(label_row)
        merged[task.id_field] = rid
        rows[rid] = merged

    if decisions:
        for decision in iter_jsonl(decisions):
            rid = str(decision[task.id_field])
            patch = decision.get("human_label", {})
            if rid in rows:
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
        },
    )


def build_gold_from_decisions(
    task: TaskConfig,
    sample_path: str | Path,
    decisions_path: str | Path,
    version: str,
) -> Path:
    source_rows = {str(row[task.id_field]): dict(row) for row in read_jsonl(sample_path)}
    rows: dict[str, dict] = {}
    for decision in iter_jsonl(decisions_path):
        rid = str(decision[task.id_field])
        if rid not in source_rows:
            continue
        row = dict(source_rows[rid])
        row.update(decision.get("human_label", {}))
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
        },
    )
