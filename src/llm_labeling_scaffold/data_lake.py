from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse

import yaml

from .config import TaskConfig
from .io import read_jsonl


DEFAULT_REGISTRY_URI = "r2:ai-innovation-data-lake/governance/data_lake/v1/current/data_lake.yaml"
JSONL_SUFFIXES = (".jsonl", ".ndjson")
DEFAULT_R2_PREFIX = "r2:ai-innovation-data-lake/"
PUBLISH_ARTIFACT_KINDS = ("decisions", "gold", "predictions", "model_metadata")
_PUBLISH_ASSET_TYPES = {
    "decisions": "scaffold_decisions",
    "gold": "scaffold_gold",
    "predictions": "scaffold_predictions",
    "model_metadata": "scaffold_model_metadata",
}
_DEFAULT_REGISTRY_URI_OVERRIDE: str | None = None
_ALLOWED_R2_PREFIX_OVERRIDE: str | None = None


class DataLakeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MaterializedSource:
    registry_uri: str
    source_dataset_id: str
    source_manifest_uri: str
    source_object_uri: str
    source_object_path: str | None
    source_object_bytes: int | None
    source_object_sha256: str
    source_object_rows: int
    source_asset_type: str
    source_id_field: str
    source_unique_ids: int
    source_created_by: str
    source_upstream_uri: list[str]
    source_sampling_strategy: str
    local_path: Path
    manifest: dict[str, Any]
    dataset: dict[str, Any]

    def lineage(self, content_sha256: str | None = None) -> dict[str, Any]:
        data = {
            "lake_registry_uri": self.registry_uri,
            "source_dataset_id": self.source_dataset_id,
            "source_manifest_uri": self.source_manifest_uri,
            "source_object_uri": self.source_object_uri,
            "source_object_path": self.source_object_path,
            "source_object_bytes": self.source_object_bytes,
            "source_object_sha256": self.source_object_sha256,
            "source_content_sha256": content_sha256,
            "source_rows": self.source_object_rows,
            "source_asset_type": self.source_asset_type,
            "source_id_field": self.source_id_field,
            "source_unique_ids": self.source_unique_ids,
            "source_created_by": self.source_created_by,
            "source_upstream_uri": self.source_upstream_uri,
            "source_sampling_strategy": self.source_sampling_strategy,
        }
        return {key: value for key, value in data.items() if value not in (None, "")}


def _rclone_bin() -> str:
    return os.environ.get("LLS_RCLONE_BIN") or "rclone"


def _rclone_timeout() -> int:
    return int(os.environ.get("LLS_RCLONE_TIMEOUT_SECONDS", "120"))


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _allow_local_data_lake_uris() -> bool:
    return _truthy_env("LLS_ALLOW_LOCAL_DATA_LAKE_URIS")


def _normalize_r2_prefix(value: str) -> str:
    text = str(value or "").strip()
    remote, sep, path = text.partition(":")
    if not text or text.startswith("file://") or text.startswith("/") or text.startswith("./") or text.startswith("../"):
        raise DataLakeError(f"非法 R2 前缀: {value}")
    if not sep or not remote or not path:
        raise DataLakeError(f"R2 前缀必须是 rclone remote:path: {value}")
    if any(ch in remote for ch in "/\\"):
        raise DataLakeError(f"非法 rclone remote: {remote}")
    if path.startswith("/") or "\\" in path or any(part == ".." for part in path.split("/")):
        raise DataLakeError(f"R2 前缀不允许路径穿越: {value}")
    return text.rstrip("/") + "/"


def set_default_registry_uri_override(value: str | None) -> None:
    global _DEFAULT_REGISTRY_URI_OVERRIDE
    _DEFAULT_REGISTRY_URI_OVERRIDE = _normalize_registry_uri(value) if value else None


def default_registry_uri() -> str:
    return _normalize_registry_uri(
        _DEFAULT_REGISTRY_URI_OVERRIDE
        or os.environ.get("LLS_TASK_REGISTRY_URI")
        or DEFAULT_REGISTRY_URI
    )


def set_allowed_r2_prefix_override(value: str | None) -> None:
    global _ALLOWED_R2_PREFIX_OVERRIDE
    _ALLOWED_R2_PREFIX_OVERRIDE = _normalize_r2_prefix(value) if value else None


def _allowed_r2_prefix() -> str:
    return _normalize_r2_prefix(
        _ALLOWED_R2_PREFIX_OVERRIDE
        or os.environ.get("LLS_DATA_LAKE_R2_PREFIX")
        or DEFAULT_R2_PREFIX
    )


def _normalize_registry_uri(uri: str) -> str:
    text = str(uri or "").strip() or DEFAULT_REGISTRY_URI
    if text.endswith("/"):
        return text + "data_lake.yaml"
    if Path(text).name == "":
        return text.rstrip("/") + "/data_lake.yaml"
    return text


def _is_file_uri(uri: str) -> bool:
    return uri.startswith("file://")


def _is_local_path(uri: str) -> bool:
    return _is_file_uri(uri) or uri.startswith("/") or uri.startswith("./") or uri.startswith("../")


def _local_path(uri: str) -> Path:
    if _is_file_uri(uri):
        parsed = urlparse(uri)
        return Path(parsed.path)
    return Path(uri)


def _is_rclone_uri(uri: str) -> bool:
    if _is_local_path(uri):
        return False
    head, sep, tail = uri.partition(":")
    return bool(head and sep and tail)


def _validate_rclone_uri(uri: str) -> None:
    remote, sep, path = uri.partition(":")
    if not sep or not remote or not path:
        raise DataLakeError(f"非法数据湖 URI: {uri}")
    if any(ch in remote for ch in "/\\"):
        raise DataLakeError(f"非法 rclone remote: {remote}")
    if path.startswith("/") or "\\" in path or any(part == ".." for part in path.split("/")):
        raise DataLakeError(f"数据湖对象路径不允许路径穿越: {uri}")
    allowed_prefix = _allowed_r2_prefix()
    if uri != allowed_prefix.rstrip("/") and not uri.startswith(allowed_prefix):
        raise DataLakeError(f"数据湖 URI 必须位于 {allowed_prefix}: {uri}")


def _validate_manifest_path(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise DataLakeError("任务 data_lake 缺少 source_object_path")
    if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise DataLakeError(f"source_object_path 必须是 manifest 内的安全相对路径: {path}")
    return value


def _validate_output_path(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise DataLakeError("回写相对路径不能为空")
    if value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise DataLakeError(f"回写路径必须是安全相对路径: {path}")
    return value


def _validate_publish_segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or "/" in text or "\\" in text or ".." in text or text in {".", ".."}:
        raise DataLakeError(f"{label} 必须是安全单段名称: {value}")
    return text


def _validate_storage_uri(uri: str) -> str:
    value = str(uri or "").strip()
    if not value:
        raise DataLakeError("manifest 对象缺少 storage_uri")
    if _is_rclone_uri(value):
        _validate_rclone_uri(value)
    elif _is_local_path(value):
        if not _allow_local_data_lake_uris():
            raise DataLakeError("生产模式不允许本地数据湖 URI；如需单测请设置 LLS_ALLOW_LOCAL_DATA_LAKE_URIS=1")
    else:
        raise DataLakeError(f"不支持的数据湖对象 URI: {uri}")
    return value


def _rclone_path_parts(uri: str) -> list[str]:
    if not _is_rclone_uri(uri):
        return []
    path = uri.partition(":")[2]
    return [part for part in path.strip("/").split("/") if part]


def _reject_registry_or_current_target(uri: str) -> None:
    parts = _rclone_path_parts(uri)
    if not parts:
        return
    lowered = {part.lower() for part in parts}
    if lowered.intersection({"governance", "registry", "current"}) or parts[-1].lower() == "data_lake.yaml":
        raise DataLakeError(f"发布目标不能写 registry/governance/current 路径: {uri}")


def _publish_output_base_uri(task: TaskConfig) -> str:
    base = str(task.data_lake.get("output_base_uri") or "").strip()
    if not base:
        raise DataLakeError("任务 data_lake 缺少 output_base_uri，无法发布产物")
    normalized = base.rstrip("/") + "/"
    _validate_storage_uri(normalized)
    _reject_registry_or_current_target(normalized)
    return normalized


def _join_uri(base_uri: str, relative_path: str) -> str:
    return base_uri.rstrip("/") + "/" + _validate_output_path(relative_path)


def _validate_publish_uri(uri: str, base_uri: str) -> str:
    value = _validate_storage_uri(uri)
    if not value.startswith(base_uri):
        raise DataLakeError(f"发布目标必须位于任务 output_base_uri 内: base={base_uri}, target={uri}")
    _reject_registry_or_current_target(value)
    return value


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_uri_to_path(uri: str, target: str | Path) -> Path:
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_local_path(uri):
        if not _allow_local_data_lake_uris():
            raise DataLakeError("生产模式不允许本地数据湖 URI；如需单测请设置 LLS_ALLOW_LOCAL_DATA_LAKE_URIS=1")
        source = _local_path(uri)
        if not source.is_file():
            raise DataLakeError(f"本地数据湖文件不存在: {source}")
        shutil.copyfile(source, target_path)
        return target_path
    if not _is_rclone_uri(uri):
        raise DataLakeError(f"不支持的数据湖 URI: {uri}")
    _validate_rclone_uri(uri)
    try:
        subprocess.run(
            [_rclone_bin(), "copyto", uri, str(target_path)],
            check=True,
            text=True,
            capture_output=True,
            timeout=_rclone_timeout(),
        )
    except FileNotFoundError as exc:
        raise DataLakeError("未找到 rclone。请在宿主机或面板容器中安装 rclone，并配置 R2 remote。") from exc
    except subprocess.TimeoutExpired as exc:
        raise DataLakeError(f"rclone 读取超时: {uri}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise DataLakeError(f"rclone 读取失败: {uri} {detail}") from exc
    return target_path


def copy_path_to_uri(source: str | Path, uri: str) -> str:
    source_path = Path(source)
    if not source_path.is_file():
        raise DataLakeError(f"本地文件不存在: {source_path}")
    if _is_local_path(uri):
        if not _allow_local_data_lake_uris():
            raise DataLakeError("生产模式不允许本地数据湖 URI；如需单测请设置 LLS_ALLOW_LOCAL_DATA_LAKE_URIS=1")
        target = _local_path(uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
        return str(target)
    if not _is_rclone_uri(uri):
        raise DataLakeError(f"不支持的数据湖 URI: {uri}")
    _validate_rclone_uri(uri)
    try:
        subprocess.run(
            [_rclone_bin(), "copyto", str(source_path), uri],
            check=True,
            text=True,
            capture_output=True,
            timeout=_rclone_timeout(),
        )
    except FileNotFoundError as exc:
        raise DataLakeError("未找到 rclone。请在宿主机或面板容器中安装 rclone，并配置 R2 remote。") from exc
    except subprocess.TimeoutExpired as exc:
        raise DataLakeError(f"rclone 写入超时: {uri}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise DataLakeError(f"rclone 写入失败: {uri} {detail}") from exc
    return uri


def _read_uri(uri: str, suffix: str) -> Any:
    with tempfile.TemporaryDirectory(prefix="lls-lake-") as td:
        path = Path(td) / f"object{suffix}"
        copy_uri_to_path(uri, path)
        if suffix in {".yaml", ".yml"}:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return json.loads(path.read_text(encoding="utf-8"))


def read_yaml_uri(uri: str) -> dict[str, Any]:
    data = _read_uri(uri, ".yaml")
    if not isinstance(data, dict):
        raise DataLakeError(f"数据湖 YAML 必须是对象: {uri}")
    return data


def read_json_uri(uri: str) -> dict[str, Any]:
    data = _read_uri(uri, ".json")
    if not isinstance(data, dict):
        raise DataLakeError(f"数据湖 manifest 必须是对象: {uri}")
    return data


def _data_lake_config(task: TaskConfig, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(task.data_lake)
    for key, value in (overrides or {}).items():
        if value not in (None, ""):
            cfg[key] = value
    return cfg


def _registry_uri(cfg: dict[str, Any]) -> str:
    return _normalize_registry_uri(str(cfg.get("lake_registry_uri") or default_registry_uri()))


def _resolve_dataset(registry: dict[str, Any], dataset_id: str) -> dict[str, Any]:
    datasets = registry.get("datasets")
    if not isinstance(datasets, dict):
        raise DataLakeError("数据湖登记表缺少 datasets")
    dataset = datasets.get(dataset_id)
    if not isinstance(dataset, dict):
        raise DataLakeError(f"数据湖登记表中没有数据集: {dataset_id}")
    return dict(dataset)


def _manifest_objects(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    objects = manifest.get("objects")
    if not isinstance(objects, list):
        raise DataLakeError("数据集 manifest 缺少 objects 清单")
    return [dict(item) for item in objects if isinstance(item, dict)]


def candidate_objects(manifest: dict[str, Any], suffixes: tuple[str, ...] = JSONL_SUFFIXES) -> list[dict[str, Any]]:
    out = []
    for item in _manifest_objects(manifest):
        path = str(item.get("path") or "")
        uri = str(item.get("storage_uri") or "")
        if path.endswith(suffixes) or uri.endswith(suffixes):
            out.append(item)
    return out


def _manifest_sha256(item: dict[str, Any]) -> str:
    value = item.get("sha256") or item.get("content_sha256")
    text = str(value or "").strip()
    if not text:
        raise DataLakeError("任务级输入对象缺少 sha256")
    return text


def _manifest_int(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    if not isinstance(value, int):
        raise DataLakeError(f"任务级输入对象缺少整数 {key}")
    return value


def _manifest_text(item: dict[str, Any], key: str) -> str:
    text = str(item.get(key) or "").strip()
    if not text:
        raise DataLakeError(f"任务级输入对象缺少 {key}")
    return text


def _manifest_uri_list(item: dict[str, Any], key: str) -> list[str]:
    value = item.get(key)
    if not isinstance(value, list):
        raise DataLakeError(f"任务级输入对象缺少列表 {key}")
    out = [str(part).strip() for part in value if str(part).strip()]
    if not out:
        raise DataLakeError(f"任务级输入对象 {key} 不能为空")
    return out


def _validate_label_import_object(item: dict[str, Any], task: TaskConfig) -> dict[str, Any]:
    path = _validate_manifest_path(str(item.get("path") or ""))
    uri = _validate_storage_uri(str(item.get("storage_uri") or "").strip())
    if not (path.endswith(JSONL_SUFFIXES) or uri.endswith(JSONL_SUFFIXES)):
        raise DataLakeError(f"任务级输入对象必须是 JSONL/NDJSON: {path}")
    asset_type = str(item.get("asset_type") or "").strip()
    if asset_type != "label_import_jsonl":
        raise DataLakeError(f"任务级输入对象 asset_type 必须是 label_import_jsonl: {path}")
    rows = _manifest_int(item, "rows")
    unique_ids = _manifest_int(item, "unique_ids")
    bytes_value = _manifest_int(item, "bytes")
    sha256 = _manifest_sha256(item)
    created_by = _manifest_text(item, "created_by")
    upstream_uri = _manifest_uri_list(item, "upstream_uri")
    sampling_strategy = _manifest_text(item, "sampling_strategy")
    id_field = str(item.get("id_field") or "").strip()
    if id_field != task.id_field:
        raise DataLakeError(f"任务 ID 字段与 manifest 不一致: task={task.id_field}, manifest={id_field or '-'}")
    return {
        **item,
        "path": path,
        "storage_uri": uri,
        "asset_type": asset_type,
        "rows": rows,
        "unique_ids": unique_ids,
        "bytes": bytes_value,
        "sha256": sha256,
        "id_field": id_field,
        "created_by": created_by,
        "upstream_uri": upstream_uri,
        "sampling_strategy": sampling_strategy,
    }


def _canonical_relative_storage_uri(dataset: dict[str, Any], source_object_path: str) -> str | None:
    canonical_uri = str(dataset.get("canonical_uri") or "").strip()
    if not canonical_uri:
        return None
    return canonical_uri.rstrip("/") + "/" + source_object_path


def _select_object(manifest: dict[str, Any], cfg: dict[str, Any], dataset: dict[str, Any] | None = None) -> dict[str, Any]:
    objects = _manifest_objects(manifest)
    source_object_path = str(cfg.get("source_object_path") or "").strip()

    if source_object_path:
        safe_path = _validate_manifest_path(source_object_path)
        matches = [item for item in objects if item.get("path") == safe_path]
        if not matches and dataset:
            expected_uri = _canonical_relative_storage_uri(dataset, safe_path)
            matches = [item for item in objects if item.get("storage_uri") == expected_uri] if expected_uri else []
    else:
        matches = candidate_objects(manifest)

    if not matches:
        raise DataLakeError("数据集 manifest 中没有匹配的可导入对象")
    if len(matches) > 1:
        preview = ", ".join(str(item.get("path") or item.get("storage_uri")) for item in matches[:8])
        raise DataLakeError(f"匹配到多个 JSONL 对象，必须在 task.yaml 指定 source_object_path: {preview}")
    return matches[0]


def resolve_source(task: TaskConfig, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = _data_lake_config(task, overrides)
    if not cfg:
        raise DataLakeError("任务未配置 data_lake")
    registry_uri = _registry_uri(cfg)
    registry = read_yaml_uri(registry_uri)
    dataset_id = str(cfg.get("source_dataset_id") or "").strip()
    if not dataset_id:
        raise DataLakeError("任务 data_lake 缺少 source_dataset_id")
    dataset = _resolve_dataset(registry, dataset_id)
    registry_manifest_uri = str(dataset.get("manifest") or "").strip()
    if not registry_manifest_uri:
        raise DataLakeError(f"数据集缺少 manifest URI: {dataset_id}")
    explicit_manifest_uri = str(cfg.get("source_manifest_uri") or "").strip()
    if explicit_manifest_uri and explicit_manifest_uri != registry_manifest_uri:
        raise DataLakeError(
            f"source_manifest_uri 与 registry 不一致: task={explicit_manifest_uri}, registry={registry_manifest_uri}"
        )
    manifest_uri = registry_manifest_uri
    manifest = read_json_uri(manifest_uri)
    manifest_dataset_id = str(manifest.get("dataset_id") or "").strip()
    if not manifest_dataset_id:
        raise DataLakeError("manifest 缺少 dataset_id")
    if manifest_dataset_id != dataset_id:
        raise DataLakeError(f"manifest.dataset_id 与 registry 不一致: dataset={dataset_id}, manifest={manifest_dataset_id}")
    for key in ("layer", "domain"):
        registry_value = str(dataset.get(key) or "").strip()
        manifest_value = str(manifest.get(key) or "").strip()
        if registry_value:
            if not manifest_value:
                raise DataLakeError(f"manifest 缺少 {key}")
            if registry_value != manifest_value:
                raise DataLakeError(f"manifest.{key} 与 registry 不一致: registry={registry_value}, manifest={manifest_value}")
    selected = _validate_label_import_object(_select_object(manifest, cfg, dataset), task)
    object_uri = str(selected.get("storage_uri") or "").strip()
    if not object_uri:
        raise DataLakeError("匹配对象缺少 storage_uri")
    return {
        "registry_uri": registry_uri,
        "registry": registry,
        "source_dataset_id": dataset_id,
        "dataset": dataset,
        "source_manifest_uri": manifest_uri,
        "manifest": manifest,
        "selected_object": selected,
        "candidate_objects": candidate_objects(manifest),
    }


def preview_source(task: TaskConfig, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    resolved = resolve_source(task, overrides)
    selected = resolved["selected_object"]
    candidates = resolved["candidate_objects"]
    return {
        "lake_registry_uri": resolved["registry_uri"],
        "source_dataset_id": resolved["source_dataset_id"],
        "source_manifest_uri": resolved["source_manifest_uri"],
        "dataset": {
            "layer": resolved["dataset"].get("layer"),
            "domain": resolved["dataset"].get("domain"),
            "canonical_uri": resolved["dataset"].get("canonical_uri"),
        },
        "manifest": {
            "dataset_id": resolved["manifest"].get("dataset_id"),
            "layer": resolved["manifest"].get("layer"),
            "domain": resolved["manifest"].get("domain"),
            "object_count": resolved["manifest"].get("object_count"),
            "total_bytes": resolved["manifest"].get("total_bytes"),
        },
        "selected_object": {
            "path": selected.get("path"),
            "storage_uri": selected.get("storage_uri"),
            "bytes": selected.get("bytes"),
            "sha256": selected.get("sha256"),
            "rows": selected.get("rows"),
            "asset_type": selected.get("asset_type"),
            "id_field": selected.get("id_field"),
            "unique_ids": selected.get("unique_ids"),
            "created_by": selected.get("created_by"),
            "upstream_uri": selected.get("upstream_uri"),
            "sampling_strategy": selected.get("sampling_strategy"),
            "mod_time": selected.get("mod_time"),
        },
        "candidate_objects": [
            {
                "path": item.get("path"),
                "storage_uri": item.get("storage_uri"),
                "bytes": item.get("bytes"),
            }
            for item in candidates[:50]
        ],
    }


def materialize_source(task: TaskConfig, overrides: dict[str, Any] | None = None,
                       max_bytes: int | None = None) -> MaterializedSource:
    resolved = resolve_source(task, overrides)
    selected = resolved["selected_object"]
    object_uri = str(selected.get("storage_uri") or "").strip()
    size = selected.get("bytes")
    if max_bytes is not None and isinstance(size, int) and size > max_bytes:
        raise DataLakeError(f"数据湖对象过大，当前上限为 {max_bytes} bytes")
    tmp = tempfile.NamedTemporaryFile(prefix="lls-lake-source-", suffix=Path(object_uri).suffix or ".jsonl", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        copy_uri_to_path(object_uri, tmp_path)
        actual_size = tmp_path.stat().st_size
        if isinstance(size, int) and actual_size != size:
            raise DataLakeError(f"数据湖对象字节数校验失败: manifest={size}, actual={actual_size}")
        actual_sha256 = _file_sha256(tmp_path)
        if actual_sha256 != selected["sha256"]:
            raise DataLakeError("数据湖对象 sha256 校验失败")
        if max_bytes is not None and tmp_path.stat().st_size > max_bytes:
            raise DataLakeError(f"数据湖对象过大，当前上限为 {max_bytes} bytes")
        return MaterializedSource(
            registry_uri=resolved["registry_uri"],
            source_dataset_id=resolved["source_dataset_id"],
            source_manifest_uri=resolved["source_manifest_uri"],
            source_object_uri=object_uri,
            source_object_path=selected.get("path"),
            source_object_bytes=size if isinstance(size, int) else None,
            source_object_sha256=selected["sha256"],
            source_object_rows=selected["rows"],
            source_asset_type=selected["asset_type"],
            source_id_field=selected["id_field"],
            source_unique_ids=selected["unique_ids"],
            source_created_by=selected["created_by"],
            source_upstream_uri=selected["upstream_uri"],
            source_sampling_strategy=selected["sampling_strategy"],
            local_path=tmp_path,
            manifest=resolved["manifest"],
            dataset=resolved["dataset"],
        )
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def read_source_rows(source: MaterializedSource) -> list[dict]:
    suffix = source.local_path.suffix.lower()
    if suffix in JSONL_SUFFIXES or suffix == "":
        return read_jsonl(source.local_path)
    raise DataLakeError(f"不支持直接导入的数据湖对象格式: {source.source_object_uri}")


def default_import_id(task: TaskConfig, source: MaterializedSource) -> str:
    cfg = task.data_lake
    if cfg.get("default_import_id"):
        return str(cfg["default_import_id"]).strip()
    stem = Path(str(source.source_object_path or source.source_object_uri)).stem
    return f"{task.task_id}_{stem}_lake"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonl_rows_if_supported(path: Path) -> int | None:
    if path.suffix.lower() not in JSONL_SUFFIXES:
        return None
    return len(read_jsonl(path))


def _artifact_manifest_uri(target_uri: str) -> str:
    for suffix in JSONL_SUFFIXES + (".json", ".csv"):
        if target_uri.endswith(suffix):
            return target_uri[: -len(suffix)] + ".manifest.json"
    return target_uri.rstrip("/") + ".manifest.json"


def _first_existing(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def _publish_artifact_location(task: TaskConfig, runs_root: str | Path, kind: str, artifact_id: str) -> dict[str, Any]:
    if kind not in PUBLISH_ARTIFACT_KINDS:
        raise DataLakeError(f"不支持的发布产物类型: {kind}")
    task_dir = Path(runs_root) / task.task_id
    artifact_id = str(artifact_id or "").strip()
    if kind == "decisions":
        decision_id = _validate_publish_segment(artifact_id, "decision_id")
        local = task_dir / "decisions" / decision_id / "decisions.jsonl"
        source_manifest = task_dir / "decisions" / decision_id / "manifest.json"
        relative = f"decisions/{decision_id}/decisions.jsonl"
        identity = {"decision_id": decision_id}
        required_manifest = source_manifest
    elif kind == "gold":
        version = artifact_id[5:] if artifact_id.startswith("gold_") else artifact_id
        version = _validate_publish_segment(version, "gold version")
        local = task_dir / "gold" / f"gold_{version}.jsonl"
        source_manifest = task_dir / "gold" / f"gold_{version}.manifest.json"
        relative = f"gold/{version}/gold_{version}.jsonl"
        identity = {"version": version}
        required_manifest = source_manifest
    elif kind == "predictions":
        run_id = _validate_publish_segment(artifact_id, "prediction run_id")
        local = task_dir / "inference" / run_id / "predictions.jsonl"
        source_manifest = _first_existing([
            task_dir / "inference" / run_id / "manifest.json",
            task_dir / "inference" / run_id / "inference_summary.json",
        ])
        relative = f"predictions/{run_id}/predictions.jsonl"
        identity = {"run_id": run_id}
        required_manifest = None
    else:
        model_id = _validate_publish_segment(artifact_id, "model_id")
        local = task_dir / "models" / model_id / "manifest.json"
        source_manifest = local
        relative = f"model_metadata/{model_id}/manifest.json"
        identity = {"model_id": model_id}
        required_manifest = local

    if not local.is_file():
        raise DataLakeError(f"本地产物不存在: {local}")
    if required_manifest is not None and not required_manifest.is_file():
        raise DataLakeError(f"本地产物缺少 manifest: {required_manifest}")

    base_uri = _publish_output_base_uri(task)
    target_uri = _validate_publish_uri(_join_uri(base_uri, relative), base_uri)
    manifest_uri = _validate_publish_uri(_artifact_manifest_uri(target_uri), base_uri)
    bytes_value = local.stat().st_size
    sha256 = _file_sha256(local)
    return {
        "artifact_type": kind,
        "asset_type": _PUBLISH_ASSET_TYPES[kind],
        "artifact_id": artifact_id,
        **identity,
        "local_path": str(local),
        "source_manifest_path": str(source_manifest) if source_manifest is not None else None,
        "target_path": relative,
        "target_uri": target_uri,
        "manifest_uri": manifest_uri,
        "bytes": bytes_value,
        "sha256": sha256,
        "rows": _jsonl_rows_if_supported(local),
    }


def _publish_manifest(task: TaskConfig, artifact: dict[str, Any], *, dry_run: bool,
                      idempotency_key: str | None = None) -> dict[str, Any]:
    manifest = {
        "manifest_version": "1.0",
        "task_id": task.task_id,
        "artifact_type": artifact["artifact_type"],
        "asset_type": artifact["asset_type"],
        "artifact_id": artifact["artifact_id"],
        "path": artifact["target_path"],
        "storage_uri": artifact["target_uri"],
        "manifest_uri": artifact["manifest_uri"],
        "bytes": artifact["bytes"],
        "sha256": artifact["sha256"],
        "rows": artifact["rows"],
        "source_manifest_path": artifact.get("source_manifest_path"),
        "created_at": _now(),
        "created_by": "llm_labeling_scaffold",
        "dry_run": dry_run,
    }
    for key in ("decision_id", "version", "run_id", "model_id"):
        if artifact.get(key):
            manifest[key] = artifact[key]
    if idempotency_key:
        manifest["idempotency_key"] = idempotency_key
    return {key: value for key, value in manifest.items() if value is not None}


def plan_artifact_publish(task: TaskConfig, runs_root: str | Path, kind: str, artifact_id: str) -> dict[str, Any]:
    artifact = _publish_artifact_location(task, runs_root, kind, artifact_id)
    artifact["manifest"] = _publish_manifest(task, artifact, dry_run=True)
    return {
        "task_id": task.task_id,
        "action": "publish_plan",
        "dry_run": True,
        "output_base_uri": _publish_output_base_uri(task),
        "artifacts": [artifact],
    }


def _validate_idempotency_key(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        raise DataLakeError("submit 必须提供 idempotency key")
    if len(text) > 128 or any(ch.isspace() for ch in text) or "/" in text or "\\" in text or ".." in text:
        raise DataLakeError("idempotency key 必须是安全短字符串，不能包含空白、路径分隔符或路径穿越")
    return text


def _verify_published_object(uri: str, *, expected_bytes: int, expected_sha256: str) -> dict[str, Any]:
    suffix = Path(uri).suffix or ".artifact"
    with tempfile.TemporaryDirectory(prefix="lls-lake-publish-verify-") as td:
        target = Path(td) / f"object{suffix}"
        copy_uri_to_path(uri, target)
        actual_bytes = target.stat().st_size
        actual_sha256 = _file_sha256(target)
    if actual_bytes != expected_bytes:
        raise DataLakeError(f"发布对象字节数校验失败: uri={uri} expected={expected_bytes} actual={actual_bytes}")
    if actual_sha256 != expected_sha256:
        raise DataLakeError(f"发布对象 sha256 校验失败: uri={uri}")
    return {"bytes": actual_bytes, "sha256": actual_sha256}


def submit_artifact_publish(
    task: TaskConfig,
    runs_root: str | Path,
    kind: str,
    artifact_id: str,
    *,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise DataLakeError("submit 必须显式 confirm")
    key = _validate_idempotency_key(idempotency_key)
    plan = plan_artifact_publish(task, runs_root, kind, artifact_id)
    submitted: list[dict[str, Any]] = []
    for artifact in plan["artifacts"]:
        manifest = _publish_manifest(task, artifact, dry_run=False, idempotency_key=key)
        with tempfile.TemporaryDirectory(prefix="lls-lake-publish-") as td:
            manifest_path = Path(td) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest_bytes = manifest_path.stat().st_size
            manifest_sha256 = _file_sha256(manifest_path)

            written_artifact = copy_path_to_uri(artifact["local_path"], artifact["target_uri"])
            written_manifest = copy_path_to_uri(manifest_path, artifact["manifest_uri"])
            artifact_verification = _verify_published_object(
                artifact["target_uri"],
                expected_bytes=artifact["bytes"],
                expected_sha256=artifact["sha256"],
            )
            manifest_verification = _verify_published_object(
                artifact["manifest_uri"],
                expected_bytes=manifest_bytes,
                expected_sha256=manifest_sha256,
            )
        submitted.append({
            **artifact,
            "manifest": manifest,
            "target_uri": written_artifact,
            "manifest_uri": written_manifest,
            "verified": True,
            "verification": {
                "artifact": artifact_verification,
                "manifest": manifest_verification,
            },
        })
    return {
        "task_id": task.task_id,
        "action": "publish_submit",
        "dry_run": False,
        "confirmed": True,
        "idempotency_key": key,
        "output_base_uri": plan["output_base_uri"],
        "artifacts": submitted,
    }


def export_artifact(task: TaskConfig, local_path: str | Path,
                    target_uri: str | None = None, target_path: str | None = None) -> dict[str, Any]:
    cfg = task.data_lake
    local = Path(local_path)
    if not local.is_file():
        raise DataLakeError(f"本地文件不存在: {local}")
    if not target_uri:
        base = str(cfg.get("output_base_uri") or "").strip()
        if not base:
            raise DataLakeError("任务 data_lake 缺少 output_base_uri，无法推导回写位置")
        rel = _validate_output_path(target_path or local.name)
        target_uri = base.rstrip("/") + "/" + rel
    else:
        rel = _validate_output_path(target_path or local.name)
    manifest_uri = _artifact_manifest_uri(target_uri)
    rows = _jsonl_rows_if_supported(local)
    manifest = {
        "manifest_version": "1.0",
        "task_id": task.task_id,
        "asset_type": "scaffold_output",
        "path": rel,
        "storage_uri": target_uri,
        "bytes": local.stat().st_size,
        "sha256": _file_sha256(local),
        "rows": rows,
        "created_at": _now(),
        "created_by": "llm_labeling_scaffold",
    }
    with tempfile.TemporaryDirectory(prefix="lls-lake-export-") as td:
        manifest_path = Path(td) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        written = copy_path_to_uri(local, target_uri)
        written_manifest = copy_path_to_uri(manifest_path, manifest_uri)
    return {
        "task_id": task.task_id,
        "local_path": str(local),
        "target_uri": written,
        "manifest_uri": written_manifest,
        "manifest": manifest,
    }
