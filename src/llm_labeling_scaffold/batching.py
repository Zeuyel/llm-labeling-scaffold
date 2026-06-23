from __future__ import annotations

import hashlib
from pathlib import Path

from .io import read_jsonl, write_json, write_jsonl


def _hash_rows(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(repr(sorted(row.items())).encode("utf-8"))
    return h.hexdigest()


def batch_records(sample: str | Path, output_dir: str | Path, batch_size: int) -> list[Path]:
    rows = read_jsonl(sample)
    out = Path(output_dir)
    batch_dir = out / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    manifest_batches = []
    for idx in range(0, len(rows), batch_size):
        chunk = rows[idx : idx + batch_size]
        batch_no = len(paths) + 1
        path = batch_dir / f"batch_{batch_no:05d}.jsonl"
        write_jsonl(chunk, path)
        paths.append(path)
        manifest_batches.append({"batch": path.name, "rows": len(chunk), "sha256": _hash_rows(chunk)})
    write_json({"sample": str(sample), "batch_size": batch_size, "batch_count": len(paths), "batches": manifest_batches}, out / "manifest.json")
    return paths
