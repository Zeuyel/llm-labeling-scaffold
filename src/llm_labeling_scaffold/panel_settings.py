from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .data_lake import _normalize_registry_uri
from .io import write_json


SETTINGS_FILENAME = "panel_settings.json"
UPDATEABLE_FIELDS = {"task_registry_uri", "data_lake_r2_prefix"}


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def task_source_mode() -> str:
    value = str(os.environ.get("LLS_TASK_SOURCE") or "local").strip().lower()
    if value in {"r2", "data_lake", "registry"}:
        return "r2"
    return "local"


def allow_data_lake_overrides() -> bool:
    return _truthy_env("LLS_ALLOW_DATA_LAKE_OVERRIDES")


def allow_manual_imports() -> bool:
    configured = _env_bool("LLS_ALLOW_MANUAL_IMPORTS")
    if configured is not None:
        return configured
    return task_source_mode() == "local"


def rclone_config_path() -> str | None:
    value = str(os.environ.get("RCLONE_CONFIG") or os.environ.get("LLS_RCLONE_CONFIG") or "").strip()
    return value or None


def settings_path(runs_root: str | Path) -> Path:
    override = str(os.environ.get("LLS_PANEL_SETTINGS_PATH") or "").strip()
    if override:
        return Path(override)
    return Path(runs_root) / "_system" / SETTINGS_FILENAME


def _is_local_uri(value: str) -> bool:
    return value.startswith("file://") or value.startswith("/") or value.startswith("./") or value.startswith("../")


def _validate_rclone_uri(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    remote, sep, path = text.partition(":")
    if _is_local_uri(text) or not sep or not remote or not path:
        raise ValueError(f"{field_name} 必须是 rclone/R2 URI: {value}")
    if any(ch in remote for ch in "/\\"):
        raise ValueError(f"非法 rclone remote: {remote}")
    if path.startswith("/") or "\\" in path or any(part == ".." for part in path.split("/")):
        raise ValueError(f"{field_name} 不允许路径穿越: {value}")
    return text


def normalize_task_registry_uri(value: str) -> str:
    return _validate_rclone_uri(_normalize_registry_uri(value), "task_registry_uri")


def normalize_data_lake_r2_prefix(value: str) -> str:
    text = _validate_rclone_uri(value, "data_lake_r2_prefix")
    return text.rstrip("/") + "/"


def _env_task_registry_uri() -> str:
    return str(os.environ.get("LLS_TASK_REGISTRY_URI") or "").strip()


def _env_data_lake_r2_prefix() -> str:
    return str(os.environ.get("LLS_DATA_LAKE_R2_PREFIX") or "").strip()


def read_stored_settings(runs_root: str | Path) -> dict[str, str]:
    path = settings_path(runs_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"panel settings 不是合法 JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"panel settings 必须是 JSON 对象: {path}")
    stored: dict[str, str] = {}
    if data.get("task_registry_uri") not in (None, ""):
        stored["task_registry_uri"] = normalize_task_registry_uri(str(data["task_registry_uri"]))
    if data.get("data_lake_r2_prefix") not in (None, ""):
        stored["data_lake_r2_prefix"] = normalize_data_lake_r2_prefix(str(data["data_lake_r2_prefix"]))
    return stored


def effective_settings(runs_root: str | Path) -> dict[str, Any]:
    stored = read_stored_settings(runs_root)
    task_registry = stored.get("task_registry_uri") or _env_task_registry_uri()
    data_lake_prefix = stored.get("data_lake_r2_prefix") or _env_data_lake_r2_prefix()
    return {
        "task_source": task_source_mode(),
        "task_registry_uri": normalize_task_registry_uri(task_registry) if task_registry else "",
        "data_lake_r2_prefix": normalize_data_lake_r2_prefix(data_lake_prefix) if data_lake_prefix else "",
        "allow_data_lake_overrides": allow_data_lake_overrides(),
        "allow_manual_imports": allow_manual_imports(),
        "rclone_config_path": rclone_config_path(),
    }


def update_settings(runs_root: str | Path, updates: dict[str, Any]) -> dict[str, Any]:
    unsupported = sorted(str(key) for key in updates if key not in UPDATEABLE_FIELDS)
    if unsupported:
        raise ValueError(f"不支持更新这些 settings 字段: {', '.join(unsupported)}")

    stored = read_stored_settings(runs_root)
    if "task_registry_uri" in updates:
        value = str(updates.get("task_registry_uri") or "").strip()
        if value:
            stored["task_registry_uri"] = normalize_task_registry_uri(value)
        else:
            stored.pop("task_registry_uri", None)
    if "data_lake_r2_prefix" in updates:
        value = str(updates.get("data_lake_r2_prefix") or "").strip()
        if value:
            stored["data_lake_r2_prefix"] = normalize_data_lake_r2_prefix(value)
        else:
            stored.pop("data_lake_r2_prefix", None)

    write_json(stored, settings_path(runs_root), indent=2)
    return effective_settings(runs_root)
