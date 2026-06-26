from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TaskConfig:
    path: Path
    raw: dict[str, Any]

    @property
    def task_id(self) -> str:
        return str(self.raw["task_id"])

    @property
    def id_field(self) -> str:
        return str(self.raw["id_field"])

    @property
    def input_path(self) -> Path:
        value = self.raw["input"]["path"]
        p = Path(value)
        if p.is_absolute():
            return p
        # Resolve paths relative to the task file first.
        candidate = (self.path.parent / p).resolve()
        if candidate.exists():
            return candidate
        return Path(value)

    @property
    def text_fields(self) -> list[str]:
        return list(self.raw["input"].get("text_fields", []))

    @property
    def metadata_fields(self) -> list[str]:
        return list(self.raw["input"].get("metadata_fields", []))

    @property
    def primary_label(self) -> dict[str, Any]:
        return dict(self.raw["labels"]["primary"])

    @property
    def auxiliary_labels(self) -> list[dict[str, Any]]:
        return list(self.raw["labels"].get("auxiliary", []))

    @property
    def annotation(self) -> dict[str, Any]:
        value = self.raw.get("annotation", {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def annotation_guidelines(self) -> str | None:
        value = self.annotation.get("guidelines")
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @property
    def constraints(self) -> list[dict[str, str]]:
        return list(self.raw.get("constraints", []))

    @property
    def runs_dir(self) -> Path:
        return Path(self.raw.get("runs_dir", "runs")) / self.task_id


def load_task(path: str | Path) -> TaskConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"task config must be a mapping: {path}")
    for key in ("task_id", "id_field", "input", "labels"):
        if key not in raw:
            raise ValueError(f"missing required task key: {key}")
    return TaskConfig(path=p, raw=raw)


def with_runs_root(task: TaskConfig, runs_root: str | Path) -> TaskConfig:
    raw = dict(task.raw)
    raw["runs_dir"] = str(Path(runs_root))
    return TaskConfig(path=task.path, raw=raw)


def build_text(row: dict, task: TaskConfig) -> str:
    parts = []
    for field in task.text_fields:
        value = row.get(field, "")
        if value is None:
            value = ""
        parts.append(str(value).strip())
    return " [SEP] ".join(part for part in parts if part)
