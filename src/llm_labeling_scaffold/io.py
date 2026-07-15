from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator
import uuid


_JSONL_PAIR_SCHEMA_VERSION = 1


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_pair_commit_path(primary_path: Path) -> Path:
    return primary_path.with_name(f".{primary_path.name}.pair-commit.json")


def _jsonl_pair_generation_root(primary_path: Path) -> Path:
    return primary_path.with_name(f".{primary_path.name}.pair-generations")


def _pair_artifact_paths(payload: Any, parent: Path, *, marker_path: Path | None = None) -> dict[str, Path]:
    if not isinstance(payload, dict) or payload.get("schema_version") != _JSONL_PAIR_SCHEMA_VERSION:
        raise RuntimeError("JSONL pair commit schema 无效")
    generation = str(payload.get("generation") or "")
    primary_name = str(payload.get("primary") or "")
    artifacts = payload.get("artifacts")
    if (
        len(generation) != 32
        or any(char not in "0123456789abcdef" for char in generation)
        or not primary_name
        or primary_name in {".", ".."}
        or "/" in primary_name
        or "\\" in primary_name
    ):
        raise RuntimeError("JSONL pair commit identity 无效")
    if not isinstance(artifacts, dict) or set(artifacts) != {"accepted", "quarantine"}:
        raise RuntimeError("JSONL pair commit artifacts 不完整")
    primary_path = parent / primary_name
    if marker_path is not None and marker_path.resolve() != _jsonl_pair_commit_path(primary_path).resolve():
        raise RuntimeError("JSONL pair commit marker 路径无效")
    generation_root = _jsonl_pair_generation_root(primary_path)
    raw_generation_dir = generation_root / generation
    if generation_root.is_symlink() or raw_generation_dir.is_symlink():
        raise RuntimeError("JSONL pair generation 路径不能使用 symlink")
    generation_dir = raw_generation_dir.resolve()
    resolved: dict[str, Path] = {}
    logical_names: set[str] = set()
    for label in ("accepted", "quarantine"):
        item = artifacts[label]
        if not isinstance(item, dict):
            raise RuntimeError("JSONL pair commit artifact 结构无效")
        logical_name = str(item.get("logical_name") or "")
        relative_path = Path(str(item.get("generation_path") or ""))
        expected_sha256 = str(item.get("sha256") or "")
        if (
            not logical_name
            or logical_name in {".", ".."}
            or "/" in logical_name
            or "\\" in logical_name
            or logical_name in logical_names
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or len(expected_sha256) != 64
        ):
            raise RuntimeError("JSONL pair commit artifact identity 无效")
        raw_artifact_path = parent / relative_path
        artifact_path = raw_artifact_path.resolve()
        if (
            raw_artifact_path.is_symlink()
            or not artifact_path.is_relative_to(generation_dir)
            or not artifact_path.is_file()
        ):
            raise RuntimeError("JSONL pair committed generation 不完整")
        if _file_sha256(artifact_path) != expected_sha256:
            raise RuntimeError("JSONL pair committed generation 哈希不一致")
        logical_names.add(logical_name)
        resolved[logical_name] = artifact_path
    if primary_name not in resolved:
        raise RuntimeError("JSONL pair primary artifact 缺失")
    return resolved


def resolve_committed_jsonl_path(path: str | Path) -> Path:
    logical_path = Path(path)
    parent = logical_path.parent
    own_marker = _jsonl_pair_commit_path(logical_path)
    markers = [own_marker, *sorted(parent.glob(".*.pair-commit.json"))]
    matches: list[Path] = []
    seen: set[Path] = set()
    for marker in markers:
        resolved_marker = marker.resolve()
        if resolved_marker in seen or not marker.is_file():
            continue
        seen.add(resolved_marker)
        try:
            if marker.is_symlink():
                raise RuntimeError("JSONL pair commit marker 不能使用 symlink")
            payload = json.loads(marker.read_text(encoding="utf-8"))
            artifacts = _pair_artifact_paths(payload, parent, marker_path=marker)
        except Exception as exc:
            if marker.resolve() == own_marker.resolve():
                raise RuntimeError("JSONL pair commit marker 无法验证") from exc
            continue
        committed = artifacts.get(logical_path.name)
        if committed is not None:
            matches.append(committed)
    if len(matches) > 1:
        raise RuntimeError("JSONL artifact 被多个 pair commit 声明")
    if matches:
        return matches[0]

    for descriptor in parent.glob(".*.pair-generations/*/generation.json"):
        try:
            payload = json.loads(descriptor.read_text(encoding="utf-8"))
            artifacts = payload.get("artifacts") if isinstance(payload, dict) else None
            logical_names = set()
            if isinstance(artifacts, dict):
                logical_names = {
                    str(item.get("logical_name") or "")
                    for item in artifacts.values()
                    if isinstance(item, dict)
                }
        except Exception:
            continue
        if logical_path.name in logical_names:
            raise RuntimeError("JSONL pair 尚无完整 committed generation")
    return logical_path


def read_jsonl(path: str | Path) -> list[dict]:
    rows: list[dict] = []
    with resolve_committed_jsonl_path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def iter_jsonl(path: str | Path) -> Iterator[dict]:
    with resolve_committed_jsonl_path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def write_jsonl(rows: Iterable[dict], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def append_jsonl(row: dict, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def write_text_atomic(text: str, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_jsonl_non_atomic(rows: Iterable[dict], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(obj: dict | list, path: str | Path, *, indent: int = 2) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, indent=indent))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def publish_jsonl_pair(
    accepted_rows: Iterable[dict],
    accepted_path: str | Path,
    quarantine_rows: Iterable[dict],
    quarantine_path: str | Path,
) -> dict[str, Any]:
    accepted = Path(accepted_path)
    quarantine = Path(quarantine_path)
    if accepted.resolve() == quarantine.resolve():
        raise ValueError("JSONL pair artifacts 不能使用同一路径")
    if accepted.parent.resolve() != quarantine.parent.resolve():
        raise ValueError("JSONL pair artifacts 必须位于同一目录")

    accepted_values = list(accepted_rows)
    quarantine_values = list(quarantine_rows)
    generation = uuid.uuid4().hex
    generation_root = _jsonl_pair_generation_root(accepted)
    if generation_root.is_symlink():
        raise RuntimeError("JSONL pair generation root 不能使用 symlink")
    generation_root.mkdir(parents=True, exist_ok=True)
    if generation_root.is_symlink() or not generation_root.is_dir():
        raise RuntimeError("JSONL pair generation root 无效")
    generation_dir = generation_root / generation
    generation_dir.mkdir(exist_ok=False)
    generation_accepted = generation_dir / "accepted.jsonl"
    generation_quarantine = generation_dir / "quarantine.jsonl"
    write_jsonl(accepted_values, generation_accepted)
    write_jsonl(quarantine_values, generation_quarantine)

    parent = accepted.parent
    descriptor = {
        "schema_version": _JSONL_PAIR_SCHEMA_VERSION,
        "generation": generation,
        "primary": accepted.name,
        "artifacts": {
            "accepted": {
                "logical_name": accepted.name,
                "generation_path": str(generation_accepted.relative_to(parent)),
                "sha256": _file_sha256(generation_accepted),
                "rows": len(accepted_values),
            },
            "quarantine": {
                "logical_name": quarantine.name,
                "generation_path": str(generation_quarantine.relative_to(parent)),
                "sha256": _file_sha256(generation_quarantine),
                "rows": len(quarantine_values),
            },
        },
    }
    write_json(descriptor, generation_dir / "generation.json")
    write_jsonl(accepted_values, accepted)
    write_jsonl(quarantine_values, quarantine)
    commit_marker = _jsonl_pair_commit_path(accepted)
    write_json(descriptor, commit_marker)
    return {
        "generation": generation,
        "commit_marker": str(commit_marker),
        "accepted_generation_path": str(generation_accepted),
        "quarantine_generation_path": str(generation_quarantine),
    }


def read_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
