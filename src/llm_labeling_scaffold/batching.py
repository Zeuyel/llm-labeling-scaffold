from __future__ import annotations

import math
import hashlib
from pathlib import Path
from typing import Any

from .io import read_jsonl, write_json, write_jsonl


def _hash_rows(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(repr(sorted(row.items())).encode("utf-8"))
    return h.hexdigest()


def _rate(value: float | int | str | None, *, name: str) -> float:
    if value in (None, ""):
        return 0.0
    parsed = float(value)
    if parsed < 0 or parsed > 1:
        raise ValueError(f"{name} 必须在 0 到 1 之间")
    return parsed


def _item_id(row: dict, row_no: int, id_field: str | None) -> str:
    if id_field:
        value = row.get(id_field)
        if value not in (None, ""):
            return str(value)
    for key in ("record_id", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return f"row_{row_no:06d}"


def _select_overlap_positions(total: int, count: int) -> list[int]:
    if total <= 0 or count <= 0:
        return []
    if count >= total:
        return list(range(total))
    return [int(idx * total / count) for idx in range(count)]


def _batch_name(batch_no: int) -> str:
    return f"batch_{batch_no:05d}.jsonl"


def _default_plan_id(output_dir: Path, batch_size: int, overlap_rate: float) -> str:
    if output_dir.name:
        return output_dir.name
    if overlap_rate:
        return f"qc_size_{batch_size}"
    return f"size_{batch_size}"


def batch_records(
    sample: str | Path,
    output_dir: str | Path,
    batch_size: int,
    *,
    overlap_rate: float | int | str = 0.0,
    min_annotators_per_overlap_item: int | str = 2,
    gold_rate: float | int | str = 0.0,
    strategy_id: str | None = None,
    plan_id: str | None = None,
    id_field: str | None = None,
) -> list[Path]:
    batch_size = int(batch_size)
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    overlap_rate_value = _rate(overlap_rate, name="overlap_rate")
    gold_rate_value = _rate(gold_rate, name="gold_rate")
    min_annotators = int(min_annotators_per_overlap_item)
    if min_annotators < 1:
        raise ValueError("min_annotators_per_overlap_item 必须大于 0")
    if overlap_rate_value > 0 and min_annotators < 2:
        raise ValueError("overlap_rate 大于 0 时，min_annotators_per_overlap_item 至少为 2")

    rows = read_jsonl(sample)
    out = Path(output_dir)
    batch_dir = out / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)

    entries = [
        {
            "row": dict(row),
            "row_number": row_no,
            "id": _item_id(row, row_no, id_field),
        }
        for row_no, row in enumerate(rows, start=1)
    ]
    planned_batches: list[dict[str, list[dict[str, Any]]]] = []
    for idx in range(0, len(rows), batch_size):
        planned_batches.append({"regular": entries[idx : idx + batch_size], "overlap": []})

    overlap_count = 0
    if overlap_rate_value > 0 and entries:
        overlap_count = min(len(entries), math.ceil(len(entries) * overlap_rate_value))
    if overlap_count and not planned_batches:
        planned_batches.append({"regular": [], "overlap": []})
    while overlap_count and len(planned_batches) < min_annotators:
        planned_batches.append({"regular": [], "overlap": []})

    regular_batch_by_row: dict[int, int] = {}
    for batch_idx, batch in enumerate(planned_batches):
        for entry in batch["regular"]:
            regular_batch_by_row[int(entry["row_number"])] = batch_idx

    overlap_items: list[dict[str, Any]] = []
    for position in _select_overlap_positions(len(entries), overlap_count):
        entry = entries[position]
        source_batch_idx = regular_batch_by_row.get(int(entry["row_number"]), 0)
        assigned_batch_idxs = {source_batch_idx}
        next_offset = 1
        while len(assigned_batch_idxs) < min_annotators:
            target_idx = (source_batch_idx + next_offset) % len(planned_batches)
            next_offset += 1
            if target_idx in assigned_batch_idxs:
                continue
            planned_batches[target_idx]["overlap"].append(entry)
            assigned_batch_idxs.add(target_idx)
        overlap_items.append(
            {
                "id": entry["id"],
                "row_number": entry["row_number"],
                "regular_batch": _batch_name(source_batch_idx + 1),
                "batches": [_batch_name(idx + 1) for idx in sorted(assigned_batch_idxs)],
                "annotator_slots": len(assigned_batch_idxs),
            }
        )

    paths: list[Path] = []
    manifest_batches: list[dict[str, Any]] = []
    for batch_idx, batch in enumerate(planned_batches):
        regular_entries = batch["regular"]
        overlap_entries = batch["overlap"]
        chunk = [entry["row"] for entry in regular_entries] + [entry["row"] for entry in overlap_entries]
        path = batch_dir / _batch_name(batch_idx + 1)
        write_jsonl(chunk, path)
        paths.append(path)
        manifest_batches.append(
            {
                "batch": path.name,
                "rows": len(chunk),
                "regular_rows": len(regular_entries),
                "overlap_rows": len(overlap_entries),
                "regular_item_ids": [entry["id"] for entry in regular_entries],
                "overlap_item_ids": [entry["id"] for entry in overlap_entries],
                "sha256": _hash_rows(chunk),
            }
        )

    resolved_strategy_id = str(strategy_id or "").strip()
    if not resolved_strategy_id:
        resolved_strategy_id = "quality_control_overlap_v1" if overlap_rate_value > 0 else "regular_sequential_v1"
    resolved_plan_id = str(plan_id or "").strip() or _default_plan_id(out, batch_size, overlap_rate_value)
    manifest = {
        "schema_version": 2,
        "sample": str(sample),
        "batch_size": batch_size,
        "batch_count": len(paths),
        "plan_id": resolved_plan_id,
        "strategy_id": resolved_strategy_id,
        "total_sample_rows": len(rows),
        "regular_assignment_count": len(rows),
        "overlap_item_count": len(overlap_items),
        "overlap_assignment_count": sum(len(item["batches"]) - 1 for item in overlap_items),
        "overlap_rate": overlap_rate_value,
        "min_annotators_per_overlap_item": min_annotators,
        "gold_rate": gold_rate_value,
        "gold_item_count": 0,
        "gold_item_ids": [],
        "gold_source": None,
        "id_field": id_field,
        "overlap_item_ids": [item["id"] for item in overlap_items],
        "overlap_items": overlap_items,
        "batches": manifest_batches,
    }
    write_json(manifest, out / "manifest.json")
    return paths
