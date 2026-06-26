from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

import yaml

from .config import load_task, with_runs_root
from .io import append_jsonl, iter_jsonl, read_json, write_json, write_jsonl, write_text_atomic
from .jobs import Job, create_job, run_job

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None


# --- core object: task -------------------------------------------------------

def _task_roots(tasks_root: str | Path) -> list[Path]:
    parts: list[str] = []
    for chunk in str(tasks_root).split(os.pathsep):
        parts.extend(item.strip() for item in chunk.split(","))
    return [Path(item) for item in parts if item]


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _archive_stamp(value: str) -> str:
    return value.replace(":", "").replace("-", "").replace("+00:00", "Z")


def _fsync_dir(path: str | Path) -> None:
    try:
        fd = os.open(Path(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _staging_dir(runs_root: str | Path, task_id: str, kind: str, asset_id: str) -> Path:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    return (
        Path(runs_root)
        / task_id
        / "_staging"
        / kind
        / f"{_slug(asset_id)}.{os.getpid()}.{uuid.uuid4().hex}"
    )


def _publish_directory(staging: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"目标目录已存在: {target}")
    _fsync_dir(staging)
    os.replace(staging, target)
    _fsync_dir(target.parent)


def _move_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"归档目录已存在: {target}")
    old_parent = source.parent
    os.replace(source, target)
    _fsync_dir(old_parent)
    _fsync_dir(target.parent)


@contextmanager
def _asset_lock(runs_root: str | Path, task_id: str, name: str):
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    lock_dir = Path(runs_root) / task_id / "_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{_slug(name)}.lock"
    with lock_path.open("w", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def record_audit_event(runs_root: str | Path, task_id: str, event: str, *,
                       asset_type: str, asset_id: str, status: str = "succeeded",
                       actor: str = "system", details: dict[str, Any] | None = None) -> dict:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    entry = {
        "event": event,
        "task_id": task_id,
        "asset_type": asset_type,
        "asset_id": asset_id,
        "status": status,
        "actor": actor,
        "details": details or {},
        "created_at": _now(),
    }
    append_jsonl(entry, Path(runs_root) / task_id / "_audit" / "events.jsonl")
    return entry


def _task_file_deletable(path: Path, root: Path) -> bool:
    if root.name == "examples":
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def load_task_by_id(tasks_root: str | Path, task_id: str):
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    for root in _task_roots(tasks_root):
        if not root.exists():
            continue
        for yml in sorted(root.rglob("task.yaml")):
            if "_archive" in yml.parts:
                continue
            try:
                task = load_task(yml)
            except Exception:
                continue
            if task.task_id == task_id:
                return task
    raise ValueError(f"任务不存在: {task_id}")


def _active_task_exists(roots: list[Path], task_id: str) -> bool:
    for root in roots:
        if not root.exists():
            continue
        for yml in sorted(root.rglob("task.yaml")):
            if "_archive" in yml.parts:
                continue
            try:
                if load_task(yml).task_id == task_id:
                    return True
            except Exception:
                continue
    return False


def _archived_task_exists(roots: list[Path], task_id: str) -> bool:
    for root in roots:
        archive = root / "_archive"
        if archive.is_dir() and any(path.name.startswith(f"{task_id}__") for path in archive.iterdir()):
            return True
    return False


def _profile_jsonl(path: Path, id_field: str | None = None) -> dict[str, Any]:
    fields: list[str] = []
    seen_fields: set[str] = set()
    ids: set[str] = set()
    duplicate_ids = 0
    missing_ids = 0
    rows = 0
    for row in iter_jsonl(path):
        rows += 1
        for key in row:
            if key not in seen_fields:
                seen_fields.add(key)
                fields.append(key)
        if id_field:
            value = row.get(id_field)
            if value in (None, ""):
                missing_ids += 1
            else:
                text = str(value)
                if text in ids:
                    duplicate_ids += 1
                ids.add(text)
    return {
        "rows": rows,
        "fields": fields,
        "id_field": id_field,
        "unique_ids": len(ids) if id_field else None,
        "duplicate_ids": duplicate_ids if id_field else None,
        "missing_ids": missing_ids if id_field else None,
    }


def _canonical_row(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _jsonl_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for row in iter_jsonl(path):
        digest.update(_canonical_row(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def list_tasks(tasks_root: str | Path) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for root in _task_roots(tasks_root):
        if not root.exists():
            continue
        for yml in sorted(root.rglob("task.yaml")):
            if "_archive" in yml.parts:
                continue
            try:
                task = load_task(yml)
                key = task.task_id
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "task_id": task.task_id,
                    "path": str(yml),
                    "root": str(root),
                    "source": root.name,
                    "deletable": _task_file_deletable(yml, root),
                    "id_field": task.id_field,
                    "primary_label": task.primary_label,
                    "auxiliary_labels": task.auxiliary_labels,
                })
            except Exception as exc:  # noqa: BLE001
                out.append({"task_id": None, "path": str(yml), "error": str(exc)})
    return out


def _task_run_assets(runs_root: str | Path, task_id: str) -> list[str]:
    run_dir = Path(runs_root) / task_id
    if not run_dir.is_dir():
        return []
    ignored = {"_locks"}
    return sorted(item.name for item in run_dir.iterdir() if item.name not in ignored)


def archive_task(tasks_root: str | Path, task_id: str, *, runs_root: str | Path | None = None,
                 reason: str = "") -> dict:
    task_id = str(task_id or "").strip()
    if not task_id or ".." in task_id or "/" in task_id or "\\" in task_id:
        raise ValueError("非法任务编号")
    for root in _task_roots(tasks_root):
        if not root.exists():
            continue
        for yml in sorted(root.rglob("task.yaml")):
            if "_archive" in yml.parts:
                continue
            try:
                task = load_task(yml)
            except Exception:
                continue
            if task.task_id != task_id:
                continue
            if not _task_file_deletable(yml, root):
                raise ValueError("示例任务不可归档；如需隐藏示例任务，请只配置 tasks 作为任务目录")
            if runs_root is not None:
                assets = _task_run_assets(runs_root, task_id)
                meaningful_assets = [name for name in assets if name not in {"_audit"}]
                if meaningful_assets:
                    raise ValueError(f"任务已有数据资产，不能归档任务配置。请先处理 runs/{task_id} 下资产: {', '.join(meaningful_assets)}")
            task_dir = yml.parent
            archived_at = _now()
            stamp = _archive_stamp(archived_at)
            target = root / "_archive" / f"{task_id}__{stamp}"
            write_json(
                {
                    "task_id": task_id,
                    "archived_at": archived_at,
                    "archive_reason": reason,
                    "archived_from": str(task_dir),
                },
                task_dir / "archive.json",
            )
            _move_directory(task_dir, target)
            if runs_root is not None:
                record_audit_event(
                    runs_root,
                    task_id,
                    "task.archive",
                    asset_type="task",
                    asset_id=task_id,
                    details={"archive_path": str(target), "reason": reason},
                )
            return {
                "task_id": task_id,
                "archive_path": str(target),
                "archived": True,
                "archived_at": archived_at,
            }
    raise ValueError(f"任务不存在: {task_id}")


def delete_task(tasks_root: str | Path, task_id: str, *, runs_root: str | Path | None = None,
                delete_runs: bool = False) -> dict:
    if delete_runs:
        raise ValueError("不支持从面板删除 runs 数据；任务只能归档，数据资产需按各自规则归档")
    return archive_task(tasks_root, task_id, runs_root=runs_root, reason="panel archive")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = []
        for line in value.splitlines():
            raw_items.extend(line.split(","))
        return [item.strip() for item in raw_items if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _coerce_label_value(label_type: str, value: str):
    if label_type == "integer":
        return int(value)
    if label_type == "number":
        return float(value)
    if label_type == "boolean":
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n"}:
            return False
        raise ValueError(f"非法布尔取值: {value}")
    return str(value)


def _normalize_label(raw: dict[str, Any], *, primary: bool = False) -> dict[str, Any]:
    name = str(raw.get("name") or raw.get("primary_label_name") or "").strip()
    if not name:
        raise ValueError("标签字段名不能为空")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"非法标签字段名: {name}")
    label_type = str(raw.get("type", "categorical" if primary else "string")).strip() or "string"
    if label_type not in {"categorical", "integer", "number", "boolean", "string"}:
        raise ValueError(f"不支持的标签类型: {label_type}")

    label: dict[str, Any] = {"name": name, "type": label_type}
    title = str(raw.get("title") or "").strip()
    if title:
        label["title"] = title
    if "required" in raw:
        label["required"] = bool(raw.get("required"))

    values = _string_list(raw.get("values"))
    if values:
        label["values"] = [_coerce_label_value(label_type, value) for value in values]
    if label_type == "categorical" and len(label.get("values", [])) < 2:
        raise ValueError(f"分类标签至少需要两个取值: {name}")
    if label_type in {"integer", "number"}:
        if raw.get("min") not in (None, ""):
            label["min"] = _coerce_label_value(label_type, str(raw["min"]))
        if raw.get("max") not in (None, ""):
            label["max"] = _coerce_label_value(label_type, str(raw["max"]))
    return label


def create_task(tasks_root: str | Path, spec: dict[str, Any]) -> dict:
    task_id = str(spec.get("task_id", "")).strip()
    if not task_id or ".." in task_id or "/" in task_id or "\\" in task_id:
        raise ValueError("任务编号只能使用单段目录名")
    id_field = str(spec.get("id_field", "record_id")).strip() or "record_id"
    text_fields = _string_list(spec.get("text_fields"))
    metadata_fields = _string_list(spec.get("metadata_fields"))
    primary_raw = {
        "name": spec.get("primary_label_name", "label"),
        "type": spec.get("primary_label_type", "categorical"),
        "values": spec.get("primary_label_values", []),
        "title": spec.get("primary_label_title", ""),
    }
    primary_label = _normalize_label(primary_raw, primary=True)
    auxiliary_labels = [
        _normalize_label(item)
        for item in spec.get("auxiliary_labels", [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    if not text_fields:
        raise ValueError("至少需要一个文本字段")

    roots = _task_roots(tasks_root)
    root = roots[-1] if roots else Path("tasks")
    task_dir = root / task_id
    task_path = task_dir / "task.yaml"
    if task_path.exists() or _active_task_exists(roots, task_id):
        raise ValueError(f"任务已存在: {task_id}")
    if _archived_task_exists(roots, task_id):
        raise ValueError(f"任务编号已归档，不能复用: {task_id}")

    raw = {
        "task_id": task_id,
        "id_field": id_field,
        "runs_dir": "runs",
        "input": {
            "path": str(spec.get("input_path") or "raw/input.jsonl"),
            "text_fields": text_fields,
            "metadata_fields": metadata_fields,
        },
        "labels": {
            "primary": primary_label,
        },
    }
    if auxiliary_labels:
        raw["labels"]["auxiliary"] = auxiliary_labels
    annotation_guidelines = str(spec.get("annotation_guidelines", "")).strip()
    if annotation_guidelines:
        raw["annotation"] = {"guidelines": annotation_guidelines}
    task_dir.mkdir(parents=True, exist_ok=False)
    write_text_atomic(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), task_path)
    prompt = str(spec.get("prompt", "")).strip()
    if prompt:
        write_text_atomic(prompt + "\n", task_dir / "prompt.md")
        raw["prompt"] = "prompt.md"
        write_text_atomic(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), task_path)
    task = load_task(task_path)
    return {
        "task_id": task.task_id,
        "path": str(task_path),
        "id_field": task.id_field,
        "primary_label": task.primary_label,
        "auxiliary_labels": task.auxiliary_labels,
    }


def _jobs_dir(runs_root: Path, task_id: str) -> Path:
    return Path(runs_root) / task_id / "_jobs"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    text = re.sub(r"_+", "_", text).strip("_.-")
    return text or "item"


def _default_argilla_dataset(task_id: str, sample_id: str | None) -> str:
    return f"{_slug(task_id)}_{_slug(sample_id or 'sample')}_v001"


# --- core object: run + jobs -------------------------------------------------

def start_action(runs_root: Path, task_path: str, action: str, params: dict) -> dict:
    task = with_runs_root(load_task(task_path), runs_root)
    jobs_dir = _jobs_dir(runs_root, task.task_id)
    job = create_job(action, dict(params, task=task_path), jobs_dir)

    def target(j: Job) -> dict:
        j.log(f"action={action} task={task.task_id}")
        if action == "sample":
            from .sampling import sample_records
            sample_id = params["sample_id"]
            with _asset_lock(runs_root, task.task_id, f"sample-{sample_id}"):
                path = sample_records(task, int(params["rows"]), sample_id,
                                      params.get("strategy", "random"), int(params.get("seed", 20260617)),
                                      params.get("source"), params.get("source_import_id"))
            return {"artifact": str(path), "kind": "sample"}
        if action == "batch":
            from .batching import batch_records
            sample_id = Path(params["sample"]).parent.name
            batch_size = int(params["batch_size"])
            out = Path(runs_root) / task.task_id / "samples" / sample_id / "batches" / f"size_{batch_size}"
            paths = batch_records(params["sample"], out, batch_size)
            return {"artifacts": [str(p) for p in paths], "kind": "batches"}
        if action == "annotate":
            from .annotation import annotate
            run_dir = annotate(task, params["sample"], params["run_id"], params.get("provider", "local_stub"),
                               int(params.get("batch_size", 100)), bool(params.get("skip_existing", True)))
            return {"run": str(run_dir), "run_id": params["run_id"], "kind": "run"}
        if action == "argilla_push":
            from .integrations.argilla import push_sample
            sample_id = params.get("sample_id") or Path(params["sample"]).parent.name
            dataset = params.get("dataset") or _default_argilla_dataset(task.task_id, sample_id)
            result = push_sample(task, params["sample"], dataset, params.get("argilla", {}))
            annotation_id = params.get("annotation_id") or dataset
            annotation_dir = Path(runs_root) / task.task_id / "annotation_jobs" / annotation_id
            manifest = {
                "task_id": task.task_id,
                "annotation_id": annotation_id,
                "source": "argilla",
                "argilla_dataset": dataset,
                "sample_id": sample_id,
                "sample_path": params["sample"],
                "rows": result.get("records", 0),
                "status": "已分发",
                "created_at": _now(),
                "result": result,
            }
            write_json(manifest, annotation_dir / "manifest.json")
            return {"kind": "annotation_job", "annotation_id": annotation_id, "result": result}
        if action == "argilla_pull":
            from .integrations.argilla import pull_responses
            sample_id = params.get("sample_id") or (Path(params["sample"]).parent.name if params.get("sample") else None)
            dataset = params.get("dataset") or _default_argilla_dataset(task.task_id, sample_id)
            decision_id = params.get("decision_id") or dataset
            decision_dir = Path(runs_root) / task.task_id / "decisions" / decision_id
            output = Path(params.get("output") or decision_dir / "decisions.jsonl")
            result = pull_responses(task, dataset, output, params.get("argilla", {}))
            manifest = {
                "task_id": task.task_id,
                "decision_id": decision_id,
                "source": "argilla",
                "argilla_dataset": dataset,
                "sample_id": sample_id,
                "sample_path": params.get("sample"),
                "path": str(output),
                "rows": result.get("responses", 0),
                "created_at": _now(),
                "result": result,
            }
            write_json(manifest, decision_dir / "manifest.json")
            return {"kind": "decision_artifact", "artifact": str(output), "decision_id": decision_id, "result": result}
        if action == "audit":
            from .audit import audit_run
            return {"summary": audit_run(task, params["run"]), "kind": "audit"}
        if action == "merge":
            from .merge import merge_run
            return {"summary": merge_run(task, params["run"]), "kind": "merge"}
        if action == "gold":
            if params.get("sample") and params.get("decisions"):
                from .gold import build_gold_from_decisions
                path = build_gold_from_decisions(task, params["sample"], params["decisions"], params["version"])
            else:
                from .gold import build_gold
                path = build_gold(task, params["run"], params["version"], params.get("decisions"))
            return {"artifact": str(path), "version": params["version"], "kind": "gold_version"}
        if action == "train":
            from .train import train_model
            result = train_model(
                task,
                params["gold"],
                params["model_id"],
                params.get("trainer", "tfidf_sgd"),
                params.get("trainer_params", {}),
            )
            if params.get("mlflow"):
                from .integrations.mlflow import log_training_result
                result = log_training_result(task.task_id, params["model_id"], result, params.get("mlflow", {}))
            return {
                "artifact": result.get("model_path"),
                "model_id": params["model_id"],
                "trainer": result.get("trainer"),
                "mlflow": result.get("mlflow"),
                "kind": "model_version",
                "result": result,
            }
        if action == "infer":
            from .infer import infer_jsonl
            path = infer_jsonl(task, params["model"], params["corpus"], params["output"])
            return {"artifact": str(path), "kind": "inference"}
        raise ValueError(f"unknown action: {action}")

    run_job(job, target)
    return job.to_dict()


# --- core objects: artifact / gold_version / model_version / decision --------

def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _import_dir(runs_root: str | Path, task_id: str, import_id: str) -> Path:
    if not _safe_segment(task_id) or not _safe_segment(import_id):
        raise ValueError("非法任务或导入编号")
    return Path(runs_root) / task_id / "imports" / import_id


def _archive_dir(runs_root: str | Path, task_id: str, kind: str) -> Path:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    return Path(runs_root) / task_id / "_archive" / kind


def _archived_import_exists(runs_root: str | Path, task_id: str, import_id: str) -> bool:
    archive = _archive_dir(runs_root, task_id, "imports")
    return archive.is_dir() and any(path.name.startswith(f"{import_id}__") for path in archive.iterdir())


def _same_path(left: str | Path | None, right: str | Path) -> bool:
    if not left:
        return False
    try:
        return Path(left).resolve() == Path(right).resolve()
    except Exception:
        return str(left) == str(right)


def linked_samples_for_import(runs_root: str | Path, task_id: str, import_id: str) -> list[dict]:
    import_path = _import_dir(runs_root, task_id, import_id) / "raw.jsonl"
    import_manifest_path = import_path.parent / "manifest.json"
    import_manifest = read_json(import_manifest_path) if import_manifest_path.exists() else {}
    import_hash = import_manifest.get("content_sha256")
    if import_path.exists() and not import_hash:
        import_hash = _jsonl_content_hash(import_path)
    out: list[dict] = []
    base = Path(runs_root) / task_id / "samples"
    if not base.is_dir():
        return out
    for sd in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest_path = sd / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        sample_path = sd / "sample.jsonl"
        linked = manifest.get("source_import_id") == import_id or _same_path(manifest.get("input_path"), import_path)
        link_reason = "manifest"
        if not linked and import_hash and sample_path.exists():
            sample_hash = manifest.get("content_sha256") or _jsonl_content_hash(sample_path)
            if sample_hash == import_hash:
                linked = True
                link_reason = "内容哈希一致"
        if linked:
            out.append({
                "sample_id": sd.name,
                "path": str(sample_path),
                "rows": manifest.get("rows"),
                "input_path": manifest.get("input_path"),
                "link_reason": link_reason,
            })
    return out


def _sample_dir(runs_root: str | Path, task_id: str, sample_id: str) -> Path:
    if not _safe_segment(task_id) or not _safe_segment(sample_id):
        raise ValueError("非法任务或样本编号")
    return Path(runs_root) / task_id / "samples" / sample_id


def _archived_sample_exists(runs_root: str | Path, task_id: str, sample_id: str) -> bool:
    archive = _archive_dir(runs_root, task_id, "samples")
    return archive.is_dir() and any(path.name.startswith(f"{sample_id}__") for path in archive.iterdir())


def dependencies_for_sample(runs_root: str | Path, task_id: str, sample_id: str) -> list[dict[str, str]]:
    item = _sample_dir(runs_root, task_id, sample_id)
    sample_path = item / "sample.jsonl"
    base = Path(runs_root) / task_id
    deps: list[dict[str, str]] = []
    if not base.is_dir():
        return deps

    def uses_sample(manifest: dict) -> bool:
        return manifest.get("sample_id") == sample_id or _same_path(manifest.get("sample_path"), sample_path) or _same_path(manifest.get("sample"), sample_path)

    for group, key, label in [
        ("annotation_jobs", "annotation_id", "标注分发"),
        ("decisions", "decision_id", "标注结果"),
    ]:
        root = base / group
        if not root.is_dir():
            continue
        for child in sorted(path for path in root.iterdir() if path.is_dir()):
            manifest_path = child / "manifest.json"
            if manifest_path.exists():
                manifest = read_json(manifest_path)
                if uses_sample(manifest):
                    deps.append({"kind": label, "id": str(manifest.get(key) or child.name)})

    gold_dir = base / "gold"
    if gold_dir.is_dir():
        for manifest_path in sorted(gold_dir.glob("gold_*.manifest.json")):
            manifest = read_json(manifest_path)
            if uses_sample(manifest):
                deps.append({"kind": "训练集", "id": str(manifest.get("version") or manifest_path.name)})

    ignored = {
        "samples",
        "gold",
        "models",
        "schemas",
        "imports",
        "inference",
        "decisions",
        "annotation_jobs",
        "_jobs",
        "_audit",
        "_archive",
        "_locks",
        "_staging",
    }
    for run_dir in sorted(path for path in base.iterdir() if path.is_dir()):
        if run_dir.name.startswith("_") or run_dir.name in ignored:
            continue
        manifest_path = run_dir / "input" / "manifest.json"
        if manifest_path.exists() and uses_sample(read_json(manifest_path)):
            deps.append({"kind": "本地标注运行", "id": run_dir.name})

    return deps


def save_import(runs_root: str | Path, task, import_id: str, rows: list[dict], *,
                source: str = "upload") -> dict:
    import_id = str(import_id or "").strip()
    if not _safe_segment(import_id):
        raise ValueError("导入编号只能使用单段名称，不能包含路径分隔符")
    if not rows:
        raise ValueError("导入数据为空")
    try:
        _validate_import_rows(task, rows)
        incoming_hash = _content_hash(rows)
        with _asset_lock(runs_root, task.task_id, f"import-{import_id}"):
            item = _import_dir(runs_root, task.task_id, import_id)
            raw_path = item / "raw.jsonl"
            manifest_path = item / "manifest.json"
            if not item.exists() and _archived_import_exists(runs_root, task.task_id, import_id):
                raise ValueError(f"导入编号已归档，不能复用: {import_id}。请使用新的导入编号。")
            if item.exists():
                if not raw_path.exists():
                    raise ValueError(f"导入编号已存在但缺少 raw.jsonl: {import_id}")
                manifest = read_json(manifest_path) if manifest_path.exists() else {}
                if manifest.get("state") == "archived":
                    raise ValueError(f"导入编号已归档，不能复用: {import_id}。请使用新的导入编号。")
                existing_hash = manifest.get("content_sha256")
                existing_hash = existing_hash or _jsonl_content_hash(raw_path)
                if existing_hash == incoming_hash:
                    summary = _import_summary(runs_root, task.task_id, item, id_field=task.id_field)
                    summary["action"] = "reused"
                    summary["idempotent"] = True
                    record_audit_event(
                        runs_root,
                        task.task_id,
                        "import.reuse",
                        asset_type="import",
                        asset_id=import_id,
                        details={"content_sha256": incoming_hash, "rows": summary.get("rows")},
                    )
                    return summary
                raise ValueError(f"导入编号已存在且内容不同: {import_id}。请使用新的导入编号，系统不会覆盖已有数据。")

            profile = {
                **_profile_jsonl_from_rows(rows, id_field=task.id_field),
                "content_sha256": incoming_hash,
            }
            manifest = {
                "task_id": task.task_id,
                "import_id": import_id,
                "path": str(raw_path),
                "rows": profile["rows"],
                "fields": profile["fields"],
                "id_field": task.id_field,
                "unique_ids": profile["unique_ids"],
                "duplicate_ids": profile["duplicate_ids"],
                "missing_ids": profile["missing_ids"],
                "content_sha256": incoming_hash,
                "created_at": _now(),
                "source": source,
                "state": "active",
                "schema_version": 1,
            }
            staging = _staging_dir(runs_root, task.task_id, "imports", import_id)
            try:
                write_jsonl(rows, staging / "raw.jsonl")
                write_json(manifest, staging / "manifest.json")
                _publish_directory(staging, item)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            result = {
                **manifest,
                "manifest_path": str(manifest_path),
                "declared_path": None,
                "linked_samples": [],
                "action": "created",
                "idempotent": False,
            }
            record_audit_event(
                runs_root,
                task.task_id,
                "import.create",
                asset_type="import",
                asset_id=import_id,
                details={"content_sha256": incoming_hash, "rows": profile["rows"], "source": source},
            )
            return result
    except Exception as exc:
        record_audit_event(
            runs_root,
            task.task_id,
            "import.save",
            asset_type="import",
            asset_id=import_id or "-",
            status="failed",
            details={"error": str(exc), "source": source},
        )
        raise


def _profile_jsonl_from_rows(rows: list[dict], id_field: str | None = None) -> dict[str, Any]:
    fields: list[str] = []
    seen_fields: set[str] = set()
    ids: set[str] = set()
    duplicate_ids = 0
    missing_ids = 0
    for row in rows:
        for key in row:
            if key not in seen_fields:
                seen_fields.add(key)
                fields.append(key)
        if id_field:
            value = row.get(id_field)
            if value in (None, ""):
                missing_ids += 1
            else:
                text = str(value)
                if text in ids:
                    duplicate_ids += 1
                ids.add(text)
    return {
        "rows": len(rows),
        "fields": fields,
        "id_field": id_field,
        "unique_ids": len(ids) if id_field else None,
        "duplicate_ids": duplicate_ids if id_field else None,
        "missing_ids": missing_ids if id_field else None,
    }


def _validate_import_rows(task, rows: list[dict]) -> None:
    id_field = task.id_field
    text_fields = list(task.text_fields)
    fields = {key for row in rows for key in row.keys()}
    missing_fields = [field for field in [id_field, *text_fields] if field not in fields]
    if missing_fields:
        raise ValueError(f"导入数据缺少任务必需字段: {', '.join(missing_fields)}")

    seen_ids: set[str] = set()
    duplicate_ids: list[str] = []
    missing_id_rows: list[int] = []
    empty_text_rows: list[int] = []
    for index, row in enumerate(rows, start=1):
        raw_id = row.get(id_field)
        if raw_id in (None, ""):
            missing_id_rows.append(index)
        else:
            rid = str(raw_id)
            if rid in seen_ids and len(duplicate_ids) < 5:
                duplicate_ids.append(rid)
            seen_ids.add(rid)
        if not any(str(row.get(field, "") or "").strip() for field in text_fields):
            empty_text_rows.append(index)

    if missing_id_rows:
        preview = ", ".join(str(i) for i in missing_id_rows[:5])
        raise ValueError(f"导入数据存在缺失 ID 的行: {preview}")
    if duplicate_ids:
        raise ValueError(f"导入数据存在重复 ID: {', '.join(duplicate_ids)}")
    if empty_text_rows:
        preview = ", ".join(str(i) for i in empty_text_rows[:5])
        raise ValueError(f"导入数据存在文本字段全为空的行: {preview}")


def _import_summary(runs_root: str | Path, task_id: str, item: Path, *, id_field: str | None = None) -> dict:
    raw_path = item / "raw.jsonl"
    manifest_path = item / "manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    declared_path = manifest.get("path")
    fields = manifest.get("field_contract") or manifest.get("fields") or []
    rows = manifest.get("rows")
    if rows is None:
        rows = _count_jsonl(raw_path)
    summary = {
        **manifest,
        "task_id": task_id,
        "import_id": manifest.get("import_id") or item.name,
        "path": str(raw_path),
        "manifest_path": str(manifest_path),
        "declared_path": declared_path if declared_path and declared_path != str(raw_path) else None,
        "rows": rows,
        "fields": fields,
        "id_field": manifest.get("id_field") or id_field,
        "unique_ids": manifest.get("unique_ids"),
        "duplicate_ids": manifest.get("duplicate_ids"),
        "missing_ids": manifest.get("missing_ids"),
        "content_sha256": manifest.get("content_sha256"),
        "state": manifest.get("state", "active"),
        "linked_samples": linked_samples_for_import(runs_root, task_id, item.name),
    }
    if raw_path.exists() and (not fields or summary["unique_ids"] is None):
        profile = _profile_jsonl(raw_path, id_field=summary["id_field"])
        summary["fields"] = fields or profile["fields"]
        summary["rows"] = rows if rows is not None else profile["rows"]
        for key in ("unique_ids", "duplicate_ids", "missing_ids"):
            if summary.get(key) is None:
                summary[key] = profile.get(key)
    if raw_path.exists() and not summary.get("content_sha256"):
        summary["content_sha256"] = _jsonl_content_hash(raw_path)
    return summary


def list_imports(runs_root: Path, task_id: str, *, id_field: str | None = None) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "imports"
    if not base.is_dir():
        return out
    for item in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")):
        out.append(_import_summary(runs_root, task_id, item, id_field=id_field))
    return out


def import_detail(runs_root: str | Path, task_id: str, import_id: str, *, id_field: str | None = None) -> dict:
    item = _import_dir(runs_root, task_id, import_id)
    if not item.is_dir():
        raise ValueError(f"导入数据不存在: {import_id}")
    return _import_summary(runs_root, task_id, item, id_field=id_field)


def import_rows(runs_root: str | Path, task_id: str, import_id: str, *,
                offset: int = 0, limit: int = 50, query: str = "") -> dict:
    item = _import_dir(runs_root, task_id, import_id)
    raw_path = item / "raw.jsonl"
    if not raw_path.exists():
        raise ValueError(f"导入数据文件不存在: {import_id}")
    offset = max(0, int(offset))
    limit = min(max(1, int(limit)), 200)
    query = str(query or "").strip().lower()
    rows: list[dict] = []
    total = 0
    fields: list[str] = []
    seen_fields: set[str] = set()
    for row in iter_jsonl(raw_path):
        if query and query not in json_dumps(row).lower():
            continue
        total += 1
        if total <= offset:
            continue
        if len(rows) >= limit:
            continue
        rows.append(row)
        for key in row:
            if key not in seen_fields:
                seen_fields.add(key)
                fields.append(key)
    return {
        "task_id": task_id,
        "import_id": import_id,
        "offset": offset,
        "limit": limit,
        "total": total,
        "rows": rows,
        "fields": fields,
    }


def json_dumps(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def archive_import(runs_root: str | Path, task_id: str, import_id: str, *, reason: str = "") -> dict:
    try:
        with _asset_lock(runs_root, task_id, f"import-{import_id}"):
            item = _import_dir(runs_root, task_id, import_id)
            if not item.is_dir():
                raise ValueError(f"导入数据不存在: {import_id}")
            linked = linked_samples_for_import(runs_root, task_id, import_id)
            if linked:
                names = ", ".join(sample["sample_id"] for sample in linked)
                raise ValueError(f"导入数据已被样本使用，不能归档。关联样本: {names}")
            manifest_path = item / "manifest.json"
            manifest = read_json(manifest_path) if manifest_path.exists() else {
                "task_id": task_id,
                "import_id": import_id,
            }
            archived_at = _now()
            manifest.update({
                "state": "archived",
                "archived_at": archived_at,
                "archive_reason": reason,
                "archived_from": str(item),
            })
            write_json(manifest, manifest_path)
            stamp = _archive_stamp(archived_at)
            target = _archive_dir(runs_root, task_id, "imports") / f"{import_id}__{stamp}"
            _move_directory(item, target)
            result = {
                "task_id": task_id,
                "import_id": import_id,
                "archived": True,
                "archive_path": str(target),
                "archived_at": archived_at,
            }
            record_audit_event(
                runs_root,
                task_id,
                "import.archive",
                asset_type="import",
                asset_id=import_id,
                details={"archive_path": str(target), "reason": reason},
            )
            return result
    except Exception as exc:
        record_audit_event(
            runs_root,
            task_id,
            "import.archive",
            asset_type="import",
            asset_id=import_id or "-",
            status="failed",
            details={"error": str(exc), "reason": reason},
        )
        raise


def archive_sample(runs_root: str | Path, task_id: str, sample_id: str, *, reason: str = "") -> dict:
    try:
        with _asset_lock(runs_root, task_id, f"sample-{sample_id}"):
            item = _sample_dir(runs_root, task_id, sample_id)
            sample_path = item / "sample.jsonl"
            if not item.is_dir() or not sample_path.exists():
                raise ValueError(f"样本不存在: {sample_id}")
            deps = dependencies_for_sample(runs_root, task_id, sample_id)
            if deps:
                names = ", ".join(f"{dep['kind']}:{dep['id']}" for dep in deps)
                raise ValueError(f"样本已被下游资产使用，不能归档。关联资产: {names}")
            manifest_path = item / "manifest.json"
            manifest = read_json(manifest_path) if manifest_path.exists() else {
                "task_id": task_id,
                "sample_id": sample_id,
                "path": str(sample_path),
            }
            archived_at = _now()
            manifest.update({
                "state": "archived",
                "archived_at": archived_at,
                "archive_reason": reason,
                "archived_from": str(item),
            })
            write_json(manifest, manifest_path)
            stamp = _archive_stamp(archived_at)
            target = _archive_dir(runs_root, task_id, "samples") / f"{sample_id}__{stamp}"
            _move_directory(item, target)
            result = {
                "task_id": task_id,
                "sample_id": sample_id,
                "archived": True,
                "archive_path": str(target),
                "archived_at": archived_at,
            }
            record_audit_event(
                runs_root,
                task_id,
                "sample.archive",
                asset_type="sample",
                asset_id=sample_id,
                details={"archive_path": str(target), "reason": reason},
            )
            return result
    except Exception as exc:
        record_audit_event(
            runs_root,
            task_id,
            "sample.archive",
            asset_type="sample",
            asset_id=sample_id or "-",
            status="failed",
            details={"error": str(exc), "reason": reason},
        )
        raise


def list_annotation_jobs(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "annotation_jobs"
    if not base.is_dir():
        return out
    for dd in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest = dd / "manifest.json"
        if manifest.exists():
            out.append(read_json(manifest))
        else:
            out.append({
                "task_id": task_id,
                "annotation_id": dd.name,
                "source": "unknown",
            })
    return out


def list_audit_events(runs_root: str | Path, task_id: str, limit: int = 100) -> list[dict]:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    path = Path(runs_root) / task_id / "_audit" / "events.jsonl"
    if not path.exists():
        return []
    events = list(iter_jsonl(path))
    return list(reversed(events[-max(1, int(limit)):]))


def list_runs(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id
    if not base.is_dir():
        return out
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if run_dir.name.startswith("_") or run_dir.name in (
            "samples",
            "gold",
            "models",
            "schemas",
            "imports",
            "inference",
            "decisions",
            "annotation_jobs",
            "_jobs",
        ):
            continue
        audit = run_dir / "audit" / "run_summary.json"
        merged = run_dir / "merged" / "merge_summary.json"
        out.append({
            "run_id": run_dir.name,
            "path": str(run_dir),
            "has_audit": audit.exists(),
            "has_merge": merged.exists(),
            "merge": read_json(merged) if merged.exists() else None,
            "decisions": _count_decisions(run_dir),
        })
    return out


def _count_decisions(run_dir: Path) -> int:
    path = Path(run_dir) / "adjudication" / "decisions.jsonl"
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def list_samples(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "samples"
    if not base.is_dir():
        return out
    for sd in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")):
        manifest = sd / "manifest.json"
        manifest_data = read_json(manifest) if manifest.exists() else None
        out.append({
            "sample_id": sd.name,
            "path": str(sd / "sample.jsonl"),
            "manifest": manifest_data,
            "state": (manifest_data or {}).get("state", "active"),
            "dependencies": dependencies_for_sample(runs_root, task_id, sd.name),
        })
    return out


def list_models(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "models"
    if not base.is_dir():
        return out
    for md in sorted(p for p in base.iterdir() if p.is_dir()):
        metrics = md / "metrics.json"
        out.append({
            "model_id": md.name,
            "path": str((read_json(md / "manifest.json") if (md / "manifest.json").exists() else {}).get("model_path", md / "model.joblib")),
            "manifest": read_json(md / "manifest.json") if (md / "manifest.json").exists() else None,
            "metrics": read_json(metrics) if metrics.exists() else None,
        })
    return out


def list_gold_versions(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "gold"
    if not base.is_dir():
        return out
    for mp in sorted(base.glob("gold_*.manifest.json")):
        out.append(read_json(mp))
    return out


def list_decision_artifacts(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "decisions"
    if not base.is_dir():
        return out
    for dd in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest = dd / "manifest.json"
        if manifest.exists():
            out.append(read_json(manifest))
        else:
            out.append({
                "task_id": task_id,
                "decision_id": dd.name,
                "path": str(dd / "decisions.jsonl"),
                "source": "unknown",
            })
    return out


def list_decisions(runs_root: Path, task_id: str, run_id: str) -> list[dict]:
    from .io import read_jsonl
    path = Path(runs_root) / task_id / run_id / "adjudication" / "decisions.jsonl"
    if not path.exists():
        return []
    return read_jsonl(path)


def jobs_for_task(runs_root: Path, task_id: str) -> list[dict]:
    from .jobs import list_jobs
    return list_jobs(_jobs_dir(runs_root, task_id))
