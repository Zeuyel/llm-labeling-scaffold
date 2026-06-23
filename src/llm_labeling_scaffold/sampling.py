from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

from .config import TaskConfig
from .io import read_jsonl, write_json, write_jsonl


def sample_records(task: TaskConfig, rows: int, sample_id: str, strategy: str = "random", seed: int = 20260617) -> Path:
    source = read_jsonl(task.input_path)
    if strategy == "head":
        picked = source[:rows]
    elif strategy == "random":
        rng = random.Random(seed)
        picked = source[:]
        rng.shuffle(picked)
        picked = picked[:rows]
    else:
        raise ValueError(f"unsupported sampling strategy in MVP: {strategy}")
    sample_dir = task.runs_dir / "samples" / sample_id
    sample_path = sample_dir / "sample.jsonl"
    write_jsonl(picked, sample_path)
    write_json(
        {
            "task_id": task.task_id,
            "sample_id": sample_id,
            "input_path": str(task.input_path),
            "id_field": task.id_field,
            "strategy": strategy,
            "seed": seed,
            "rows": len(picked),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        sample_dir / "manifest.json",
    )
    return sample_path
