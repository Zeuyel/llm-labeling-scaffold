from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .config import TaskConfig
from .io import iter_jsonl, read_jsonl, write_json, write_jsonl


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
    out = task.runs_dir / "gold"
    out.mkdir(parents=True, exist_ok=True)
    gold_path = out / f"gold_{version}.jsonl"
    write_jsonl(gold_rows, gold_path)
    primary = task.primary_label["name"]
    counts = Counter(str(row.get(primary)) for row in gold_rows)
    manifest = {
        "task_id": task.task_id,
        "version": version,
        "run_dir": str(run),
        "decisions": str(decisions) if decisions else None,
        "rows": len(gold_rows),
        "unique_ids": len(rows),
        "primary_label": primary,
        "label_counts": dict(counts),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(manifest, out / f"gold_{version}.manifest.json")
    (out / f"gold_{version}.data_card.md").write_text(
        "# Gold Set Data Card\n\n"
        f"- task_id: `{task.task_id}`\n"
        f"- version: `{version}`\n"
        f"- rows: `{len(gold_rows)}`\n"
        f"- primary_label: `{primary}`\n"
        f"- label_counts: `{dict(counts)}`\n",
        encoding="utf-8",
    )
    return gold_path
