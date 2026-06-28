from __future__ import annotations

from pathlib import Path

from .batching import batch_records
from .config import TaskConfig
from .io import read_jsonl, write_json
from .providers.local_stub import LocalStubProvider


def get_provider(name: str):
    if name == "local_stub":
        return LocalStubProvider()
    if name == "codex_exec":
        from .providers.codex_exec import CodexExecProvider

        return CodexExecProvider()
    raise ValueError(f"provider not implemented in MVP: {name}")


def annotate(task: TaskConfig, sample: str | Path, run_id: str, provider_name: str, batch_size: int, skip_existing: bool = False) -> Path:
    run_dir = task.runs_dir / run_id
    input_dir = run_dir / "input"
    llm_dir = run_dir / "llm"
    input_dir.mkdir(parents=True, exist_ok=True)
    llm_dir.mkdir(parents=True, exist_ok=True)
    batches = batch_records(sample, input_dir, batch_size)
    provider = get_provider(provider_name)
    for batch in batches:
        output = llm_dir / f"{batch.stem}.json"
        if skip_existing and output.exists() and output.stat().st_size > 0:
            continue
        rows = read_jsonl(batch)
        payload = provider.annotate_batch(rows, task)
        write_json(payload, output)
        write_json({"provider": provider_name, "batch": batch.name, "rows": len(rows)}, llm_dir / f"{batch.stem}.meta.json")
    return run_dir
