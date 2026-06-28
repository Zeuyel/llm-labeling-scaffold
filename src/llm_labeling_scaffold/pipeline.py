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

from .config import load_task, resolve_profile_id, with_runs_root
from .io import append_jsonl, iter_jsonl, read_json, write_json, write_jsonl, write_text_atomic
from .jobs import Job, create_job, run_job
from .profiles import DEFAULT_PROFILE, list_profile_presets, profile_definition, status_label

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


def _copy_file_fsync(source: str | Path, target: str | Path) -> None:
    source_path = Path(source)
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as src, target_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())


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


DATA_LAKE_LINEAGE_KEYS = (
    "lake_registry_uri",
    "source_dataset_id",
    "source_manifest_uri",
    "source_object_uri",
    "source_object_path",
    "source_object_bytes",
    "source_object_sha256",
    "source_rows",
    "source_asset_type",
    "source_id_field",
    "source_unique_ids",
    "source_created_by",
    "source_upstream_uri",
    "source_sampling_strategy",
)


def _metadata_mismatches(existing: dict[str, Any], incoming: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for key in DATA_LAKE_LINEAGE_KEYS:
        if key not in incoming:
            continue
        if existing.get(key) != incoming[key]:
            mismatches.append(key)
    return mismatches


def _profile_meta(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key != "stages"}


def _visible_children(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(item for item in path.iterdir() if not item.name.startswith("_"))


def _child_dir_artifact_state(base: Path, filenames: tuple[str, ...]) -> dict[str, Any]:
    evidence: list[str] = []
    children = _visible_children(base)
    for child in children:
        if not child.is_dir():
            continue
        if any((child / filename).exists() for filename in filenames):
            evidence.append(str(child))
    return {
        "done": bool(evidence),
        "partial": bool(children) and not evidence,
        "evidence": evidence,
    }


def _gold_artifact_state(base: Path) -> dict[str, Any]:
    evidence = [str(path) for path in sorted(base.glob("gold_*.jsonl"))] if base.is_dir() else []
    return {
        "done": bool(evidence),
        "partial": bool(_visible_children(base)) and not evidence,
        "evidence": evidence,
    }


def _inference_artifact_state(base: Path) -> dict[str, Any]:
    evidence: list[str] = []
    if base.is_dir():
        evidence = [
            str(path)
            for name in ("predictions.jsonl", "inference_summary.json")
            for path in sorted(base.rglob(name))
        ]
    return {
        "done": bool(evidence),
        "partial": bool(_visible_children(base)) and not evidence,
        "evidence": evidence,
    }


def _agreement_audit_artifact_state(task_dir: Path) -> dict[str, Any]:
    agreement_summaries = sorted((task_dir / "agreement_audits").glob("*/summary.json"))
    passed_evidence: list[str] = []
    failed_evidence: list[str] = []
    for path in agreement_summaries:
        try:
            summary = read_json(path)
        except Exception:
            failed_evidence.append(str(path))
            continue
        if summary.get("passed") is True:
            passed_evidence.append(str(path))
        else:
            failed_evidence.append(str(path))
    if passed_evidence:
        return {"done": True, "partial": False, "evidence": passed_evidence}
    if failed_evidence:
        return {"done": False, "partial": True, "evidence": failed_evidence}

    reserved = {
        "samples",
        "gold",
        "models",
        "schemas",
        "imports",
        "inference",
        "decisions",
        "annotation_jobs",
        "agreement_audits",
        "_jobs",
    }
    evidence = [
        str(path)
        for path in sorted(task_dir.glob("*/audit/run_summary.json"))
        if path.parent.parent.name not in reserved and not path.parent.parent.name.startswith("_")
    ]
    if evidence:
        return {"done": True, "partial": False, "evidence": evidence}

    gold_state = _gold_artifact_state(task_dir / "gold")
    if gold_state["done"]:
        return {"done": True, "partial": False, "evidence": gold_state["evidence"]}
    return {"done": False, "partial": False, "evidence": []}


def _sample_batch_artifact_state(task_dir: Path) -> dict[str, Any]:
    evidence: list[str] = []
    batches_dirs: list[Path] = []
    samples_dir = task_dir / "samples"
    for sample_dir in _visible_children(samples_dir):
        if not sample_dir.is_dir():
            continue
        batches_dir = sample_dir / "batches"
        if not batches_dir.is_dir():
            continue
        batches_dirs.append(batches_dir)
        for batch_dir in _visible_children(batches_dir):
            if not batch_dir.is_dir():
                continue
            manifest = batch_dir / "manifest.json"
            if manifest.exists():
                evidence.append(str(manifest))
    return {
        "done": bool(evidence),
        "partial": bool(batches_dirs) and not evidence,
        "evidence": evidence,
    }


def _profile_stage_artifact_state(task_dir: Path, stage_id: str) -> dict[str, Any]:
    if stage_id == "lake_import":
        return _child_dir_artifact_state(task_dir / "imports", ("raw.jsonl",))
    if stage_id == "sample":
        return _child_dir_artifact_state(task_dir / "samples", ("sample.jsonl",))
    if stage_id == "pilot_calibration":
        return _sample_batch_artifact_state(task_dir)
    if stage_id == "consistency_check":
        return _agreement_audit_artifact_state(task_dir)
    if stage_id == "main_annotation":
        return _child_dir_artifact_state(task_dir / "annotation_jobs", ("manifest.json",))
    if stage_id == "review_adjudication":
        return _agreement_audit_artifact_state(task_dir)
    if stage_id == "argilla_dispatch":
        return _child_dir_artifact_state(task_dir / "annotation_jobs", ("manifest.json",))
    if stage_id == "argilla_pull":
        return _child_dir_artifact_state(task_dir / "decisions", ("decisions.jsonl", "manifest.json"))
    if stage_id == "agreement_audit":
        return _agreement_audit_artifact_state(task_dir)
    if stage_id == "gold_build":
        return _gold_artifact_state(task_dir / "gold")
    if stage_id == "train":
        return _child_dir_artifact_state(task_dir / "models", ("model.joblib", "manifest.json"))
    if stage_id == "batch_infer":
        return _inference_artifact_state(task_dir / "inference")
    return {"done": False, "partial": False, "evidence": []}


def task_profile_status(runs_root: str | Path, task, profile_id: str | None = None) -> dict[str, Any]:
    task_id = str(getattr(task, "task_id", task)).strip()
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    task_profile_id = resolve_profile_id(getattr(task, "profile", DEFAULT_PROFILE))
    selected_profile_id = resolve_profile_id(profile_id) if profile_id else task_profile_id
    profile = profile_definition(selected_profile_id)
    task_dir = Path(runs_root) / task_id
    statuses: dict[str, str] = {}
    stages: list[dict[str, Any]] = []
    for stage in profile.get("stages", []):
        stage_id = str(stage["id"])
        artifact = _profile_stage_artifact_state(task_dir, stage_id)
        depends_on = [str(item) for item in stage.get("depends_on", [])]
        waiting_for = [dep for dep in depends_on if statuses.get(dep) != "done"]
        blocked_by = [dep for dep in waiting_for if statuses.get(dep) == "blocked"]

        if artifact["done"]:
            status = "done"
        elif artifact["partial"] or blocked_by:
            status = "blocked"
        elif waiting_for:
            status = "not_started"
        else:
            status = "ready"
        statuses[stage_id] = status

        item = dict(stage)
        item["status"] = status
        item["status_label"] = status_label(status)
        if artifact["evidence"]:
            item["evidence"] = artifact["evidence"]
            item["outputs"] = artifact["evidence"]
        if waiting_for and status != "done":
            item["waiting_for"] = waiting_for
            item["blocking_reason"] = f"等待前置阶段完成: {', '.join(waiting_for)}"
        if blocked_by:
            item["blocked_by"] = blocked_by
            item["blocking_reason"] = f"前置阶段受阻: {', '.join(blocked_by)}"
        if artifact["partial"] and status == "blocked":
            item["blocking_reason"] = "检测到阶段目录存在，但缺少可识别的 manifest 或产物文件。"
        stages.append(item)
    return {
        "task_id": task_id,
        "task_profile_id": task_profile_id,
        "selected_profile_id": selected_profile_id,
        "profile": _profile_meta(profile),
        "presets": list_profile_presets(),
        "stages": stages,
    }


def _graph_route(task_id: str, key: str) -> str:
    suffix = {
        "task": "",
        "stage": "",
        "import": "/imports",
        "sample": "/samples",
        "batch": "/samples",
        "annotation_job": "/annotations",
        "agreement_audit": "/annotations",
        "run": "/annotations",
        "decision": "/annotations",
        "gold": "/gold",
        "model": "/models",
        "inference": "/models",
    }.get(key, "")
    return f"/task/{task_id}{suffix}"


def _graph_stage_route(task_id: str, stage: dict[str, Any]) -> str:
    action = str(stage.get("action") or stage.get("id") or "")
    kind = {
        "import": "import",
        "lake_import": "import",
        "sample": "sample",
        "batch": "batch",
        "argilla_push": "annotation_job",
        "argilla_dispatch": "annotation_job",
        "argilla_pull": "decision",
        "agreement_audit": "agreement_audit",
        "audit": "agreement_audit",
        "gold": "gold",
        "gold_build": "gold",
        "train": "model",
        "infer": "inference",
        "batch_infer": "inference",
    }.get(action, "stage")
    return _graph_route(task_id, kind)


def _graph_status(value: Any, *, exists: bool = True) -> str:
    status = str(value or "").strip().lower()
    if status in {"done", "completed", "complete", "succeeded", "success", "finished", "active"}:
        return "completed"
    if status in {"ready", "available", "runnable", "running", "in_progress", "pending"}:
        return "ready"
    if status in {"blocked", "failed", "error", "incomplete"}:
        return "blocked"
    if not exists:
        return "not_started"
    return "completed"


def _graph_summary(parts: list[Any]) -> str:
    labels = [str(part) for part in parts if part not in (None, "", [], {})]
    return " · ".join(labels)


def _graph_add_node(nodes: list[dict[str, Any]], seen: set[str], node: dict[str, Any]) -> None:
    if node["id"] in seen:
        return
    seen.add(node["id"])
    nodes.append(node)


def _graph_add_edge(edges: list[dict[str, str]], seen: set[tuple[str, str, str]], source: str, target: str, reason: str) -> None:
    if not source or not target or source == target:
        return
    key = (source, target, reason)
    if key in seen:
        return
    seen.add(key)
    edges.append({"source": source, "target": target, "reason": reason})


def _graph_file_node(
    nodes: list[dict[str, Any]],
    seen_nodes: set[str],
    *,
    task_id: str,
    node_id: str,
    node_type: str,
    title: str,
    status: str = "completed",
    summary: str = "",
    path: str | None = None,
    route_kind: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    node = {
        "id": node_id,
        "type": node_type,
        "title": title,
        "status": status,
        "summary": summary,
        "path": path,
        "route": _graph_route(task_id, route_kind or node_type),
    }
    if data:
        node["data"] = data
    _graph_add_node(nodes, seen_nodes, node)


def _graph_list_batches(runs_root: str | Path, task_id: str, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        for manifest in sample.get("batch_manifests") or []:
            if not isinstance(manifest, dict):
                continue
            plan_id = str(manifest.get("plan_id") or manifest.get("batch_id") or "")
            if not plan_id:
                continue
            batches.append({**manifest, "sample_id": manifest.get("sample_id") or sample_id})
    return batches


def _graph_list_inference(runs_root: str | Path, task_id: str) -> list[dict[str, Any]]:
    base = Path(runs_root) / task_id / "inference"
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for item in sorted(path for path in base.iterdir() if path.is_dir() and not path.name.startswith("_")):
        manifest_path = item / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        prediction_path = item / "predictions.jsonl"
        out.append({
            **manifest,
            "run_id": manifest.get("run_id") or manifest.get("inference_id") or item.name,
            "path": str(manifest.get("path") or prediction_path),
            "rows": manifest.get("rows") if manifest.get("rows") is not None else _count_jsonl(prediction_path),
            "manifest_path": str(manifest_path) if manifest_path.exists() else None,
            "state": manifest.get("state", "active" if prediction_path.exists() else "incomplete"),
        })
    return out


def _graph_first_existing_node(node_ids: list[str], seen_nodes: set[str]) -> str | None:
    return next((node_id for node_id in node_ids if node_id in seen_nodes), None)


def task_asset_graph(runs_root: str | Path, task, profile_id: str | None = None) -> dict[str, Any]:
    task_id = str(getattr(task, "task_id", task)).strip()
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")

    id_field = getattr(task, "id_field", None)
    profile = task_profile_status(runs_root, task, profile_id=profile_id)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()

    _graph_add_node(nodes, seen_nodes, {
        "id": "task",
        "type": "task",
        "title": task_id,
        "status": "completed",
        "summary": "任务配置",
        "path": str(getattr(task, "path", "") or ""),
        "route": _graph_route(task_id, "task"),
    })

    for stage in profile.get("stages", []):
        stage_id = str(stage.get("id") or "")
        if not stage_id:
            continue
        node_id = f"stage:{stage_id}"
        _graph_add_node(nodes, seen_nodes, {
            "id": node_id,
            "type": "stage",
            "title": stage.get("title") or stage.get("name") or stage_id,
            "status": _graph_status(stage.get("status"), exists=False),
            "summary": stage.get("description") or stage.get("action_hint") or "",
            "path": None,
            "route": _graph_stage_route(task_id, stage),
            "data": {
                "stage_id": stage_id,
                "required_inputs": stage.get("required_inputs") or [],
                "outputs": stage.get("outputs") or [],
                "status_label": stage.get("status_label"),
                "blocking_reason": stage.get("blocking_reason"),
            },
        })
        depends_on = [str(item) for item in stage.get("depends_on") or []]
        if depends_on:
            for dep in depends_on:
                _graph_add_edge(edges, seen_edges, f"stage:{dep}", node_id, "阶段依赖")
        else:
            _graph_add_edge(edges, seen_edges, "task", node_id, "任务入口")

    imports = list_imports(Path(runs_root), task_id, id_field=id_field)
    samples = list_samples(Path(runs_root), task_id)
    batches = _graph_list_batches(runs_root, task_id, samples)
    annotation_jobs = list_annotation_jobs(Path(runs_root), task_id)
    agreement_audits = list_agreement_audits(Path(runs_root), task_id)
    runs = list_runs(Path(runs_root), task_id)
    decisions = list_decision_artifacts(Path(runs_root), task_id)
    gold_versions = list_gold_versions(Path(runs_root), task_id)
    models = list_models(Path(runs_root), task_id)
    inference_runs = _graph_list_inference(runs_root, task_id)

    for item in imports:
        import_id = str(item.get("import_id") or "")
        if not import_id:
            continue
        node_id = f"import:{import_id}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="import",
            title=import_id,
            status=_graph_status(item.get("state")),
            summary=_graph_summary([f"{item.get('rows', 0)} 行", item.get("source")]),
            path=item.get("path"),
            data={"rows": item.get("rows"), "fields": item.get("fields") or [], "source": item.get("source")},
        )
        _graph_add_edge(edges, seen_edges, "stage:lake_import", node_id, "生成导入资产")

    for item in samples:
        sample_id = str(item.get("sample_id") or "")
        if not sample_id:
            continue
        node_id = f"sample:{sample_id}"
        manifest = item.get("manifest") or {}
        sample_path = item.get("path")
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="sample",
            title=sample_id,
            status=_graph_status(item.get("state"), exists=Path(sample_path or "").exists()),
            summary=_graph_summary([f"{manifest.get('rows', 0)} 行" if manifest else None, f"{item.get('batch_count', 0)} 个批次"]),
            path=sample_path,
            data={"manifest": manifest, "dependencies": item.get("dependencies") or []},
        )
        _graph_add_edge(edges, seen_edges, "stage:sample", node_id, "生成样本")
        import_id = manifest.get("source_import_id") or manifest.get("import_id")
        source = f"import:{import_id}" if import_id else _graph_first_existing_node([f"import:{x.get('import_id')}" for x in imports], seen_nodes)
        if source:
            _graph_add_edge(edges, seen_edges, source, node_id, "抽样来源")

    for item in batches:
        plan_id = str(item.get("plan_id") or item.get("batch_id") or "")
        if not plan_id:
            continue
        sample_id = str(item.get("sample_id") or "")
        node_id = f"batch:{sample_id}:{plan_id}" if sample_id else f"batch:{plan_id}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="batch",
            title=plan_id,
            status="completed",
            summary=_graph_summary([f"{item.get('batch_count', 0)} 个批次", f"样本 {sample_id}" if sample_id else None]),
            path=item.get("manifest_path") or item.get("plan_dir"),
            route_kind="batch",
            data={"manifest": item},
        )
        source_stage = _graph_first_existing_node(["stage:pilot_calibration", "stage:sample"], seen_nodes)
        if source_stage:
            _graph_add_edge(edges, seen_edges, source_stage, node_id, "生成批次计划")
        if f"sample:{sample_id}" in seen_nodes:
            _graph_add_edge(edges, seen_edges, f"sample:{sample_id}", node_id, "切分批次")

    for item in annotation_jobs:
        annotation_id = str(item.get("annotation_id") or item.get("job_id") or item.get("id") or "")
        if not annotation_id:
            continue
        node_id = f"annotation_job:{annotation_id}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="annotation_job",
            title=annotation_id,
            status=_graph_status(item.get("state") or item.get("status")),
            summary=_graph_summary([item.get("source"), item.get("argilla_dataset") or item.get("dataset")]),
            path=item.get("manifest_path") or item.get("path"),
            route_kind="annotation_job",
            data={"manifest": item},
        )
        source_stage = _graph_first_existing_node(["stage:argilla_dispatch", "stage:main_annotation"], seen_nodes)
        if source_stage:
            _graph_add_edge(edges, seen_edges, source_stage, node_id, "分发标注")
        sample_id = str(item.get("sample_id") or "")
        batch_plan_id = str(item.get("batch_plan_id") or "")
        batch_node_id = f"batch:{sample_id}:{batch_plan_id}" if sample_id and batch_plan_id else ""
        if batch_node_id in seen_nodes:
            _graph_add_edge(edges, seen_edges, batch_node_id, node_id, "批次分发")
        elif f"sample:{sample_id}" in seen_nodes:
            _graph_add_edge(edges, seen_edges, f"sample:{sample_id}", node_id, "标注输入")

    for item in agreement_audits:
        audit_id = str(item.get("audit_id") or "")
        if not audit_id:
            continue
        node_id = f"agreement_audit:{audit_id}"
        passed = item.get("passed")
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="agreement_audit",
            title=audit_id,
            status="completed" if passed is not False else "blocked",
            summary=_graph_summary(["通过" if passed is True else "需复核" if passed is False else None, f"{item.get('coverage_rate')} 覆盖率" if item.get("coverage_rate") is not None else None]),
            path=item.get("summary_path"),
            route_kind="agreement_audit",
            data={"summary": item},
        )
        source = _graph_first_existing_node([f"batch:{x.get('sample_id')}:{x.get('plan_id')}" for x in batches] + [f"decision:{x.get('decision_id')}" for x in decisions], seen_nodes)
        if source:
            _graph_add_edge(edges, seen_edges, source, node_id, "一致性审计")
        source_stage = _graph_first_existing_node(["stage:agreement_audit", "stage:consistency_check", "stage:review_adjudication"], seen_nodes)
        if source_stage:
            _graph_add_edge(edges, seen_edges, source_stage, node_id, "审计产物")

    for item in runs:
        run_id = str(item.get("run_id") or "")
        if not run_id:
            continue
        node_id = f"run:{run_id}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="run",
            title=run_id,
            status="completed" if item.get("has_merge") or item.get("has_audit") else "ready",
            summary=_graph_summary([f"{item.get('decisions', 0)} 条裁决", "已合并" if item.get("has_merge") else None]),
            path=item.get("path"),
            route_kind="run",
            data={"run": item},
        )
        _graph_add_edge(edges, seen_edges, "stage:argilla_pull", node_id, "本地运行")

    for item in decisions:
        decision_id = str(item.get("decision_id") or "")
        if not decision_id:
            continue
        node_id = f"decision:{decision_id}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="decision",
            title=decision_id,
            status=_graph_status(item.get("state")),
            summary=_graph_summary([f"{item.get('rows', 0)} 条结果", item.get("source")]),
            path=item.get("path"),
            route_kind="decision",
            data={"manifest": item},
        )
        _graph_add_edge(edges, seen_edges, "stage:argilla_pull", node_id, "回收标注")
        sample_id = str(item.get("sample_id") or "")
        if f"sample:{sample_id}" in seen_nodes:
            _graph_add_edge(edges, seen_edges, f"sample:{sample_id}", node_id, "结果来源样本")
        annotation_id = str(item.get("annotation_id") or item.get("job_id") or item.get("source_annotation_id") or "")
        if f"annotation_job:{annotation_id}" in seen_nodes:
            _graph_add_edge(edges, seen_edges, f"annotation_job:{annotation_id}", node_id, "回收结果")

    for item in gold_versions:
        version = str(item.get("version") or item.get("gold_id") or "")
        if not version:
            continue
        node_id = f"gold:{version}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="gold",
            title=version,
            status=_graph_status(item.get("state")),
            summary=_graph_summary([f"{item.get('rows', 0)} 行", item.get("source")]),
            path=item.get("path"),
            data={"manifest": item},
        )
        _graph_add_edge(edges, seen_edges, "stage:gold_build", node_id, "构建训练集")
        source = None
        decisions_path = item.get("decisions")
        if decisions_path:
            source = next((f"decision:{d.get('decision_id')}" for d in decisions if str(d.get("path")) == str(decisions_path)), None)
        source = source or _graph_first_existing_node([f"agreement_audit:{x.get('audit_id')}" for x in agreement_audits] + [f"decision:{x.get('decision_id')}" for x in decisions], seen_nodes)
        if source:
            _graph_add_edge(edges, seen_edges, source, node_id, "训练集来源")

    for item in models:
        model_id = str(item.get("model_id") or "")
        if not model_id:
            continue
        manifest = item.get("manifest") or {}
        node_id = f"model:{model_id}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="model",
            title=model_id,
            status=_graph_status(manifest.get("state")),
            summary=_graph_summary([manifest.get("trainer"), item.get("metrics") and "有评估指标"]),
            path=item.get("path"),
            data={"manifest": manifest, "metrics": item.get("metrics")},
        )
        _graph_add_edge(edges, seen_edges, "stage:train", node_id, "训练模型")
        gold_path = str(manifest.get("gold_path") or manifest.get("gold") or "")
        source = next((f"gold:{g.get('version')}" for g in gold_versions if gold_path and str(g.get("path")) == gold_path), None)
        source = source or _graph_first_existing_node([f"gold:{x.get('version')}" for x in gold_versions], seen_nodes)
        if source:
            _graph_add_edge(edges, seen_edges, source, node_id, "训练输入")

    for item in inference_runs:
        run_id = str(item.get("run_id") or "")
        if not run_id:
            continue
        node_id = f"inference:{run_id}"
        _graph_file_node(
            nodes,
            seen_nodes,
            task_id=task_id,
            node_id=node_id,
            node_type="inference",
            title=run_id,
            status=_graph_status(item.get("state")),
            summary=_graph_summary([f"{item.get('rows', 0)} 条预测"]),
            path=item.get("path"),
            data={"manifest": item},
        )
        _graph_add_edge(edges, seen_edges, "stage:batch_infer", node_id, "批量推理")
        model_id = str(item.get("model_id") or "")
        source = f"model:{model_id}" if f"model:{model_id}" in seen_nodes else _graph_first_existing_node([f"model:{x.get('model_id')}" for x in models], seen_nodes)
        if source:
            _graph_add_edge(edges, seen_edges, source, node_id, "推理模型")

    return {
        "task_id": task_id,
        "profile": profile.get("profile"),
        "nodes": nodes,
        "edges": edges,
    }


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
                    "profile": task.profile,
                    "id_field": task.id_field,
                    "primary_label": task.primary_label,
                    "auxiliary_labels": task.auxiliary_labels,
                    "data_lake": task.data_lake,
                })
            except Exception as exc:  # noqa: BLE001
                out.append({"task_id": None, "path": str(yml), "error": str(exc)})
    return out


def _task_run_assets(runs_root: str | Path, task_id: str) -> list[str]:
    run_dir = Path(runs_root) / task_id
    if not run_dir.is_dir():
        return []
    ignored = {"_archive", "_audit", "_jobs", "_locks", "_staging"}
    return sorted(item.name for item in run_dir.iterdir() if item.name not in ignored)


def _task_config_archive_info(tasks_root: str | Path, task_id: str) -> dict[str, Any]:
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
                return {
                    "task_id": task_id,
                    "path": str(yml),
                    "task_dir": str(yml.parent),
                    "root": str(root),
                    "deletable": _task_file_deletable(yml, root),
                }
    raise ValueError(f"任务不存在: {task_id}")


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


ARCHIVE_STEP_ORDER: tuple[dict[str, str], ...] = (
    {"asset_type": "inference", "label": "推理结果", "group": "inference"},
    {"asset_type": "model", "label": "模型", "group": "models"},
    {"asset_type": "gold", "label": "训练集", "group": "gold"},
    {"asset_type": "agreement_audit", "label": "一致性检查", "group": "agreement_audits"},
    {"asset_type": "decision", "label": "标注结果", "group": "decisions"},
    {"asset_type": "annotation_job", "label": "标注分发记录", "group": "annotation_jobs"},
    {"asset_type": "run", "label": "本地标注运行", "group": ""},
    {"asset_type": "sample", "label": "样本", "group": "samples"},
    {"asset_type": "import", "label": "导入数据", "group": "imports"},
)


def _dir_size_bytes(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    return total


def _archive_visible_children(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(item for item in path.iterdir() if not item.name.startswith("_"))


def _archive_group_operation(runs_root: str | Path, task_id: str, step: dict[str, str]) -> dict[str, Any] | None:
    task_dir = Path(runs_root) / task_id
    group = step["group"]
    if group:
        source = task_dir / group
        if not source.exists() or not _archive_visible_children(source):
            return None
        return {
            "asset_type": step["asset_type"],
            "label": step["label"],
            "asset_id": group,
            "source_path": str(source),
            "target_relative_path": group,
            "size_bytes": _dir_size_bytes(source),
            "item_count": len(_archive_visible_children(source)),
        }

    reserved = {
        "samples",
        "gold",
        "models",
        "schemas",
        "imports",
        "inference",
        "decisions",
        "annotation_jobs",
        "agreement_audits",
        "_archive",
        "_audit",
        "_jobs",
        "_locks",
        "_staging",
    }
    run_dirs = [
        item
        for item in sorted(task_dir.iterdir()) if task_dir.is_dir() and item.is_dir()
        and not item.name.startswith("_")
        and item.name not in reserved
    ] if task_dir.is_dir() else []
    if not run_dirs:
        return None
    return {
        "asset_type": "run",
        "label": step["label"],
        "asset_id": "runs",
        "source_paths": [str(item) for item in run_dirs],
        "target_relative_path": "runs",
        "size_bytes": sum(_dir_size_bytes(item) for item in run_dirs),
        "item_count": len(run_dirs),
    }


def _active_asset_nodes(runs_root: str | Path, task) -> list[dict[str, Any]]:
    graph = task_asset_graph(runs_root, task)
    order = {step["asset_type"]: index for index, step in enumerate(ARCHIVE_STEP_ORDER)}
    out: list[dict[str, Any]] = []
    for node in graph.get("nodes", []):
        node_type = node.get("type")
        if node_type not in order:
            continue
        out.append({
            "asset_type": node_type,
            "asset_id": str(node.get("id", "")).split(":", 1)[-1],
            "title": node.get("title"),
            "summary": node.get("summary"),
            "path": node.get("path"),
            "status": node.get("status"),
        })
    out.sort(key=lambda item: (order.get(item["asset_type"], 99), item["asset_id"]))
    return out


def _archive_dependency_edges(runs_root: str | Path, task) -> list[dict[str, str]]:
    graph = task_asset_graph(runs_root, task)
    asset_prefixes = tuple(f"{step['asset_type']}:" for step in ARCHIVE_STEP_ORDER)
    out: list[dict[str, str]] = []
    for edge in graph.get("edges", []):
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source.startswith(asset_prefixes) and target.startswith(asset_prefixes):
            out.append({
                "source": source,
                "target": target,
                "reason": str(edge.get("reason") or ""),
            })
    return out


def _cleanup_file_list(runs_root: str | Path, task_id: str) -> list[dict[str, Any]]:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    base = Path(runs_root) / task_id
    if not base.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        if "_audit" in path.relative_to(base).parts:
            continue
        try:
            stat = path.stat()
            writable = os.access(path.parent, os.W_OK | os.X_OK)
            files.append({
                "path": str(path),
                "relative_path": str(path.relative_to(base)),
                "size_bytes": stat.st_size,
                "writable": writable,
                "owner_uid": stat.st_uid,
                "owner_gid": stat.st_gid,
            })
        except OSError as exc:
            files.append({
                "path": str(path),
                "relative_path": str(path.relative_to(base)),
                "size_bytes": 0,
                "writable": False,
                "error": str(exc),
            })
    return files


def task_archive_plan(
    tasks_root: str | Path,
    runs_root: str | Path,
    task,
    *,
    r2_task_source: bool = False,
) -> dict[str, Any]:
    task_id = str(getattr(task, "task_id", task)).strip()
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    operations = [op for step in ARCHIVE_STEP_ORDER if (op := _archive_group_operation(runs_root, task_id, step))]
    warnings: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    task_config: dict[str, Any] | None = None
    try:
        task_config = _task_config_archive_info(tasks_root, task_id)
        if not task_config.get("deletable"):
            blocked.append({
                "code": "task_config_readonly",
                "message": "任务配置来自只读目录，不能由面板归档 task.yaml。",
                "path": task_config.get("path"),
            })
    except Exception as exc:
        blocked.append({"code": "task_config_missing", "message": str(exc)})
    if r2_task_source:
        blocked.append({
            "code": "r2_registry_authority",
            "message": "生产 R2 模式下，任务启停以数据湖登记表为准；面板不能伪造 registry 归档。",
        })
    cleanup_files = _cleanup_file_list(runs_root, task_id)
    permission_issues = [item for item in cleanup_files if not item.get("writable")]
    if permission_issues:
        warnings.append({
            "code": "cache_permission",
            "message": "部分本地缓存当前用户不可删除，可能由容器 root 写入；请统一容器运行 UID/GID 后再清理。",
            "paths": [item["path"] for item in permission_issues[:20]],
        })
    return {
        "task_id": task_id,
        "mode": "r2" if r2_task_source else "local",
        "can_archive": not blocked,
        "blocked": blocked,
        "warnings": warnings,
        "active_assets": _active_asset_nodes(runs_root, task),
        "dependencies": _archive_dependency_edges(runs_root, task),
        "archive_order": [
            {**step, "operation": next((op for op in operations if op["asset_type"] == step["asset_type"]), None)}
            for step in ARCHIVE_STEP_ORDER
        ],
        "operations": operations,
        "task_config": task_config,
        "cleanup": {
            "r2_protected": True,
            "note": "清理只删除本机 runs 缓存文件，不调用 R2 删除，也不删除数据湖权威对象。",
            "files": cleanup_files,
            "total_bytes": sum(int(item.get("size_bytes") or 0) for item in cleanup_files),
        },
    }


def execute_task_archive(
    tasks_root: str | Path,
    runs_root: str | Path,
    task_id: str,
    *,
    reason: str = "",
    actor: str = "system",
    r2_task_source: bool = False,
) -> dict[str, Any]:
    if r2_task_source:
        raise ValueError("生产 R2 模式下不能在面板中归档本地 task.yaml；请先在数据湖登记表中停用任务")
    task = load_task_by_id(tasks_root, task_id)
    plan = task_archive_plan(tasks_root, runs_root, task)
    if plan["blocked"]:
        raise ValueError("; ".join(item["message"] for item in plan["blocked"]))
    archived_at = _now()
    stamp = _archive_stamp(archived_at)
    archive_root = Path(runs_root) / task_id / "_archive" / stamp
    moved: list[dict[str, Any]] = []
    with _asset_lock(runs_root, task_id, "task-archive"):
        for operation in plan["operations"]:
            try:
                if operation.get("source_paths"):
                    for source_text in operation["source_paths"]:
                        source = Path(source_text)
                        target = archive_root / operation["target_relative_path"] / source.name
                        if source.exists():
                            _move_directory(source, target)
                            moved.append({**operation, "source_path": str(source), "archive_path": str(target)})
                else:
                    source = Path(operation["source_path"])
                    target = archive_root / operation["target_relative_path"]
                    if source.exists():
                        _move_directory(source, target)
                        moved.append({**operation, "archive_path": str(target)})
                record_audit_event(
                    runs_root,
                    task_id,
                    "task_archive.step",
                    asset_type=operation["asset_type"],
                    asset_id=operation["asset_id"],
                    actor=actor,
                    details={"archive_root": str(archive_root), "operation": operation, "reason": reason},
                )
            except Exception as exc:
                record_audit_event(
                    runs_root,
                    task_id,
                    "task_archive.step",
                    asset_type=operation["asset_type"],
                    asset_id=operation["asset_id"],
                    status="failed",
                    actor=actor,
                    details={"error": str(exc), "operation": operation, "reason": reason},
                )
                raise
        task_result = archive_task(tasks_root, task_id, runs_root=runs_root, reason=reason)
    record_audit_event(
        runs_root,
        task_id,
        "task_archive.complete",
        asset_type="task",
        asset_id=task_id,
        actor=actor,
        details={"archive_root": str(archive_root), "task_archive": task_result, "moved_assets": moved, "reason": reason},
    )
    return {
        "task_id": task_id,
        "archived": True,
        "archived_at": archived_at,
        "archive_root": str(archive_root),
        "moved_assets": moved,
        "task": task_result,
    }


def execute_task_cache_cleanup(runs_root: str | Path, task_id: str, *, actor: str = "system") -> dict[str, Any]:
    if not _safe_segment(task_id):
        raise ValueError("非法任务编号")
    base = Path(runs_root) / task_id
    files = _cleanup_file_list(runs_root, task_id)
    deleted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for item in files:
        path = Path(item["path"])
        try:
            path.resolve().relative_to(base.resolve())
        except ValueError:
            errors.append({"path": str(path), "error": "cleanup path escaped task run directory"})
            continue
        if not item.get("writable"):
            errors.append({
                "path": str(path),
                "error": "permission denied",
                "owner_uid": item.get("owner_uid"),
                "owner_gid": item.get("owner_gid"),
                "suggestion": "请让 panel 容器使用与宿主机一致的 PANEL_UID/PANEL_GID，或先修正 runs 目录所有权。",
            })
            continue
        try:
            path.unlink()
            deleted.append(item)
        except PermissionError as exc:
            errors.append({
                "path": str(path),
                "error": str(exc),
                "owner_uid": item.get("owner_uid"),
                "owner_gid": item.get("owner_gid"),
                "suggestion": "请让 panel 容器使用与宿主机一致的 PANEL_UID/PANEL_GID，或先修正 runs 目录所有权。",
            })
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    if base.is_dir():
        for directory in sorted((item for item in base.rglob("*") if item.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if directory.name == "_audit":
                continue
            try:
                directory.rmdir()
            except OSError:
                continue
    status = "failed" if errors else "succeeded"
    record_audit_event(
        runs_root,
        task_id,
        "task.cache_cleanup",
        asset_type="task_cache",
        asset_id=task_id,
        status=status,
        actor=actor,
        details={"deleted_files": len(deleted), "errors": errors, "r2_deleted": False},
    )
    return {
        "task_id": task_id,
        "ok": not errors,
        "r2_deleted": False,
        "deleted_files": deleted,
        "errors": errors,
    }


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
    profile = resolve_profile_id(spec.get("profile"))
    profile_definition(profile)
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
        "profile": profile,
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
    data_lake = spec.get("data_lake")
    if isinstance(data_lake, dict):
        cleaned = {str(key): value for key, value in data_lake.items() if value not in (None, "")}
        if cleaned:
            raw["data_lake"] = cleaned
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
        "profile": task.profile,
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


def _batch_plan_dir_name(batch_size: int, params: dict[str, Any]) -> str:
    plan_id = str(params.get("plan_id") or "").strip()
    if plan_id:
        if not _safe_segment(plan_id):
            raise ValueError("批次计划编号只能使用单段名称，不能包含路径分隔符")
        return plan_id
    overlap_rate = float(params.get("overlap_rate") or 0)
    gold_rate = float(params.get("gold_rate") or 0)
    strategy_id = str(params.get("strategy_id") or "").strip()
    has_quality_plan = overlap_rate > 0 or gold_rate > 0 or bool(strategy_id)
    if not has_quality_plan:
        return f"size_{batch_size}"
    min_annotators = int(params.get("min_annotators_per_overlap_item", 2))
    return _slug(f"qc_size_{batch_size}_{strategy_id or 'quality_control_overlap_v1'}_overlap_{overlap_rate:g}_min_{min_annotators}")


def _run_batch_action(runs_root: str | Path, task, params: dict[str, Any]) -> dict[str, Any]:
    from .batching import batch_records

    sample = params["sample"]
    sample_id = str(params.get("sample_id") or Path(sample).parent.name)
    batch_size = int(params["batch_size"])
    plan_dir_name = _batch_plan_dir_name(batch_size, params)
    out = Path(runs_root) / task.task_id / "samples" / sample_id / "batches" / plan_dir_name
    plan_id = str(params.get("plan_id") or plan_dir_name)
    paths = batch_records(
        sample,
        out,
        batch_size,
        overlap_rate=params.get("overlap_rate", 0.0),
        min_annotators_per_overlap_item=params.get("min_annotators_per_overlap_item", 2),
        gold_rate=params.get("gold_rate", 0.0),
        strategy_id=params.get("strategy_id"),
        plan_id=plan_id,
        id_field=getattr(task, "id_field", None),
    )
    manifest_path = out / "manifest.json"
    manifest = read_json(manifest_path)
    return {
        "artifacts": [str(p) for p in paths],
        "manifest": str(manifest_path),
        "manifest_path": str(manifest_path),
        "kind": "batches",
        "sample_id": sample_id,
        "plan_id": manifest.get("plan_id"),
        "strategy_id": manifest.get("strategy_id"),
        "overlap_rate": manifest.get("overlap_rate"),
        "min_annotators_per_overlap_item": manifest.get("min_annotators_per_overlap_item"),
    }


def _default_argilla_dataset(task_id: str, sample_id: str | None) -> str:
    return f"{_slug(task_id)}_{_slug(sample_id or 'sample')}_v001"


def _argilla_dispatch_mode(params: dict[str, Any]) -> str:
    mode = str(params.get("dispatch_mode") or "").strip() or "sample"
    if params.get("batch_plan_id") or params.get("batch_manifest_path"):
        mode = "batch_plan" if mode == "sample" else mode
    if mode not in {"batch_plan", "single_batch", "sample"}:
        raise ValueError("dispatch_mode 只能是 batch_plan、single_batch 或 sample")
    return mode


def _resolve_batch_plan_manifest(runs_root: str | Path, task, params: dict[str, Any]) -> dict[str, Any]:
    manifest_param = str(params.get("batch_manifest_path") or "").strip()
    sample_param = str(params.get("sample") or params.get("sample_path") or "").strip()
    plan_param = str(params.get("batch_plan_id") or params.get("plan_id") or "").strip()
    if manifest_param:
        manifest_path = Path(manifest_param)
    else:
        if not sample_param:
            raise ValueError("批次计划分发需要 sample 或 batch_manifest_path")
        if not plan_param:
            raise ValueError("批次计划分发需要 batch_plan_id 或 batch_manifest_path")
        if not _safe_segment(plan_param):
            raise ValueError("批次计划编号只能使用单段名称，不能包含路径分隔符")
        sample_path = Path(sample_param)
        manifest_path = sample_path.parent / "batches" / plan_param / "manifest.json"
        if not manifest_path.is_file():
            sample_id = str(params.get("sample_id") or sample_path.parent.name)
            candidate = Path(runs_root) / task.task_id / "samples" / sample_id / "batches" / plan_param / "manifest.json"
            if candidate.is_file():
                manifest_path = candidate
    if not manifest_path.is_file():
        raise ValueError(f"批次计划 manifest 不存在: {manifest_path}")

    manifest = read_json(manifest_path)
    plan_dir = manifest_path.parent
    sample_path_text = str(sample_param or manifest.get("sample") or "").strip()
    if not sample_path_text and plan_dir.parent.name == "batches":
        candidate_sample = plan_dir.parent.parent / "sample.jsonl"
        if candidate_sample.exists():
            sample_path_text = str(candidate_sample)
    sample_id = str(params.get("sample_id") or manifest.get("sample_id") or "").strip()
    if not sample_id and sample_path_text:
        sample_id = Path(sample_path_text).parent.name
    if not sample_id and plan_dir.parent.name == "batches":
        sample_id = plan_dir.parent.parent.name

    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "plan_dir": plan_dir,
        "batch_plan_id": str(manifest.get("plan_id") or plan_param or plan_dir.name),
        "sample_id": sample_id or None,
        "sample_path": sample_path_text or None,
    }


def _batch_file_from_value(batch_root: Path, value: Any) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("批次文件不能为空")
    path = Path(text)
    if not path.is_absolute():
        path = batch_root / path
    try:
        path.resolve().relative_to(batch_root.resolve())
    except ValueError as exc:
        raise ValueError(f"批次文件必须位于批次计划目录内: {path}") from exc
    return path


def _select_batch_dispatch_files(plan: dict[str, Any], params: dict[str, Any], dispatch_mode: str) -> list[dict[str, Any]]:
    batch_root = Path(plan["plan_dir"]) / "batches"
    manifest = plan["manifest"]
    manifest_entries = manifest.get("batches") if isinstance(manifest.get("batches"), list) else []
    entries: list[dict[str, Any]] = []
    for entry in manifest_entries:
        if not isinstance(entry, dict):
            continue
        batch_name = str(entry.get("batch") or entry.get("file") or entry.get("path") or "").strip()
        if not batch_name:
            continue
        path = _batch_file_from_value(batch_root, batch_name)
        entries.append({"entry": entry, "path": path, "batch_id": path.name, "batch_file": str(path)})
    if not entries and batch_root.is_dir():
        entries = [
            {"entry": {"batch": path.name}, "path": path, "batch_id": path.name, "batch_file": str(path)}
            for path in sorted(batch_root.glob("batch_*.jsonl"))
        ]
    if not entries:
        raise ValueError("批次计划没有可分发的 batch jsonl 文件")

    by_key: dict[str, dict[str, Any]] = {}
    for item in entries:
        path = item["path"]
        by_key[path.name] = item
        by_key[path.stem] = item

    selected: list[dict[str, Any]] = []
    for batch_id in _string_list(params.get("batch_ids")):
        item = by_key.get(batch_id)
        if item is None:
            item = by_key.get(Path(batch_id).name) or by_key.get(Path(batch_id).stem)
        if item is None:
            raise ValueError(f"批次计划中不存在 batch_id: {batch_id}")
        selected.append(item)

    for batch_file in _string_list(params.get("batch_files")):
        path = _batch_file_from_value(batch_root, batch_file)
        item = by_key.get(path.name) or by_key.get(path.stem)
        if item is None:
            item = {"entry": {"batch": path.name}, "path": path, "batch_id": path.name, "batch_file": str(path)}
        selected.append(item)

    if not selected:
        if dispatch_mode == "single_batch":
            if len(entries) != 1:
                raise ValueError("single_batch 分发需要指定一个 batch_ids 或 batch_files")
            selected = entries
        else:
            selected = entries

    deduped: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in selected:
        path = Path(item["path"])
        if not path.is_file():
            raise ValueError(f"批次文件不存在: {path}")
        key = str(path.resolve())
        if key in seen_paths:
            continue
        seen_paths.add(key)
        deduped.append(item)

    if dispatch_mode == "single_batch" and len(deduped) != 1:
        raise ValueError("single_batch 分发只能选择一个 batch 文件")
    return deduped


def _overlap_role_for_row(entry: dict[str, Any], record_id: str) -> str:
    overlap_ids = {str(item_id) for item_id in entry.get("overlap_item_ids") or []}
    if record_id in overlap_ids:
        return "overlap"
    regular_ids = {str(item_id) for item_id in entry.get("regular_item_ids") or []}
    if record_id in regular_ids:
        return "regular"
    return "unknown"


def _collect_dispatch_rows(
    selected: list[dict[str, Any]],
    id_field: str,
    *,
    dispatch_mode: str,
    batch_plan_id: str,
    batch_manifest_path: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    same_batch_duplicates: list[str] = []
    seen_record_ids: set[str] = set()
    duplicate_record_ids: list[str] = []
    for item in selected:
        path = Path(item["path"])
        batch_id = str(item["batch_id"])
        seen_in_batch: set[str] = set()
        for row_no, row in enumerate(iter_jsonl(path), start=1):
            value = row.get(id_field)
            if value in (None, ""):
                missing.append(f"{path.name}:{row_no}")
                continue
            original_id = str(value)
            if original_id in seen_in_batch:
                same_batch_duplicates.append(f"{original_id}@{batch_id}")
            seen_in_batch.add(original_id)

            argilla_record_id = f"{original_id}__{batch_id}"
            if argilla_record_id in seen_record_ids and argilla_record_id not in duplicate_record_ids:
                duplicate_record_ids.append(argilla_record_id)
            seen_record_ids.add(argilla_record_id)

            rows.append({
                **row,
                "__lls_dispatch_mode": dispatch_mode,
                "__lls_batch_plan_id": batch_plan_id,
                "__lls_batch_manifest_path": batch_manifest_path,
                "__lls_batch_file": str(path),
                "__lls_batch_id": batch_id,
                "__lls_original_id": original_id,
                "__lls_overlap_role": _overlap_role_for_row(item["entry"], original_id),
                "__lls_argilla_record_id": argilla_record_id,
            })
    if missing:
        raise ValueError(f"批次分发文件存在缺失 Argilla record id 的行: {', '.join(missing[:5])}")
    if same_batch_duplicates:
        preview = ", ".join(same_batch_duplicates[:10])
        raise ValueError(f"批次分发文件存在同一 batch 内重复原始 ID: {preview}")
    if duplicate_record_ids:
        preview = ", ".join(duplicate_record_ids[:10])
        raise ValueError(f"批次分发生成的 Argilla record id 仍存在重复: {preview}")
    return rows


def _selected_overlap_item_ids(plan: dict[str, Any], selected: list[dict[str, Any]]) -> list[str]:
    selected_names = {Path(item["path"]).name for item in selected}
    out: list[str] = []
    seen: set[str] = set()
    batches = plan["manifest"].get("batches")
    if isinstance(batches, list):
        for entry in batches:
            if not isinstance(entry, dict) or str(entry.get("batch") or "") not in selected_names:
                continue
            for item_id in entry.get("overlap_item_ids") or []:
                text = str(item_id)
                if text not in seen:
                    seen.add(text)
                    out.append(text)
    return out


def _resolve_argilla_dispatch(runs_root: str | Path, task, params: dict[str, Any], dispatch_mode: str) -> dict[str, Any]:
    if dispatch_mode == "sample":
        sample = params.get("sample") or params.get("sample_path")
        if not sample:
            raise ValueError("sample 分发需要 sample 文件")
        sample_path = str(sample)
        sample_id = str(params.get("sample_id") or Path(sample_path).parent.name)
        return {
            "dispatch_mode": "sample",
            "dispatch_path": sample_path,
            "sample_id": sample_id,
            "sample_path": sample_path,
            "batch_plan_id": None,
            "batch_manifest_path": None,
            "batch_ids": [],
            "batch_files": [],
            "overlap_item_ids": [],
            "batch_plan_overlap_item_ids": [],
            "rows": None,
        }

    plan = _resolve_batch_plan_manifest(runs_root, task, params)
    selected = _select_batch_dispatch_files(plan, params, dispatch_mode)
    selected_paths = [Path(item["path"]) for item in selected]
    rows = _collect_dispatch_rows(
        selected,
        task.id_field,
        dispatch_mode=dispatch_mode,
        batch_plan_id=str(plan["batch_plan_id"]),
        batch_manifest_path=str(plan["manifest_path"]),
    )
    selected_overlap_ids = _selected_overlap_item_ids(plan, selected)
    return {
        "dispatch_mode": dispatch_mode,
        "dispatch_path": str(selected_paths[0]) if dispatch_mode == "single_batch" else None,
        "sample_id": plan.get("sample_id"),
        "sample_path": plan.get("sample_path"),
        "batch_plan_id": plan.get("batch_plan_id"),
        "batch_manifest_path": str(plan["manifest_path"]),
        "batch_ids": [item["batch_id"] for item in selected],
        "batch_files": [item["batch_file"] for item in selected],
        "overlap_item_ids": list(plan["manifest"].get("overlap_item_ids") or []),
        "selected_overlap_item_ids": selected_overlap_ids,
        "batch_plan_overlap_item_ids": list(plan["manifest"].get("overlap_item_ids") or []),
        "rows": len(rows),
        "_rows": rows,
    }


def _materialize_argilla_dispatch(dispatch: dict[str, Any], annotation_dir: Path) -> str:
    if dispatch["dispatch_mode"] == "batch_plan":
        dispatch_path = annotation_dir / "dispatch.jsonl"
        write_jsonl(dispatch.pop("_rows"), dispatch_path)
        dispatch["dispatch_path"] = str(dispatch_path)
        return str(dispatch_path)
    dispatch.pop("_rows", None)
    return str(dispatch["dispatch_path"])


def _argilla_push_params(params: dict[str, Any], dispatch: dict[str, Any]) -> dict[str, Any]:
    out = dict(params.get("argilla") or {})
    if dispatch.get("dispatch_mode") == "sample":
        return out
    if dispatch.get("dispatch_mode") == "batch_plan":
        out.setdefault("record_id_strategy", "batch_scoped")
    for key in ("dispatch_mode", "batch_plan_id", "batch_manifest_path"):
        value = dispatch.get(key)
        if value not in (None, "", [], {}):
            out.setdefault(key, value)
    return out


def _argilla_lineage_for_pull(runs_root: str | Path, task_id: str, params: dict[str, Any], dataset: str) -> dict[str, Any]:
    annotation_manifest: dict[str, Any] = {}
    annotation_id = str(params.get("annotation_id") or params.get("job_id") or "").strip()
    if annotation_id and _safe_segment(annotation_id):
        manifest_path = Path(runs_root) / task_id / "annotation_jobs" / annotation_id / "manifest.json"
        if manifest_path.exists():
            annotation_manifest = read_json(manifest_path)
    if not annotation_manifest:
        for item in list_annotation_jobs(Path(runs_root), task_id):
            if item.get("argilla_dataset") == dataset or item.get("dataset") == dataset:
                annotation_manifest = item
                annotation_id = str(item.get("annotation_id") or annotation_id)
                break

    lineage: dict[str, Any] = {}
    for key in (
        "dispatch_mode",
        "batch_plan_id",
        "batch_manifest_path",
        "batch_ids",
        "batch_files",
        "overlap_item_ids",
        "sample_id",
        "sample_path",
    ):
        value = params.get(key)
        if value in (None, "", [], {}):
            value = annotation_manifest.get(key)
        if value not in (None, "", [], {}):
            lineage[key] = value
    argilla_dataset = params.get("dataset") or annotation_manifest.get("argilla_dataset") or annotation_manifest.get("dataset")
    if argilla_dataset not in (None, ""):
        lineage["argilla_dataset"] = argilla_dataset
    if annotation_id:
        lineage["annotation_id"] = annotation_id
        lineage["source_annotation_id"] = annotation_id
    return lineage


def _has_passing_agreement_audit(task, sample_path: str | Path, decisions_path: str | Path) -> bool:
    base = task.runs_dir / "agreement_audits"
    if not base.is_dir():
        return False
    for summary_path in sorted(base.glob("*/summary.json")):
        try:
            summary = read_json(summary_path)
        except Exception:
            continue
        if summary.get("passed") is not True:
            continue
        if _same_path(summary.get("sample_path"), sample_path) and _same_path(summary.get("decisions_path"), decisions_path):
            return True
    return False


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
            return _run_batch_action(runs_root, task, params)
        if action == "annotate":
            from .annotation import annotate
            run_dir = annotate(task, params["sample"], params["run_id"], params.get("provider", "local_stub"),
                               int(params.get("batch_size", 100)), bool(params.get("skip_existing", True)))
            return {"run": str(run_dir), "run_id": params["run_id"], "kind": "run"}
        if action == "argilla_push":
            from .integrations.argilla import push_sample
            dispatch_mode = _argilla_dispatch_mode(params)
            dispatch = _resolve_argilla_dispatch(runs_root, task, params, dispatch_mode)
            sample_id = dispatch.get("sample_id") or params.get("sample_id")
            dataset = params.get("dataset") or _default_argilla_dataset(task.task_id, sample_id)
            annotation_id = params.get("annotation_id") or dataset
            annotation_dir = Path(runs_root) / task.task_id / "annotation_jobs" / annotation_id
            if not annotation_dir.exists() and _archived_annotation_job_exists(runs_root, task.task_id, annotation_id):
                raise ValueError(f"标注任务编号已归档，不能复用: {annotation_id}。请使用新的标注任务编号。")
            dispatch_path = _materialize_argilla_dispatch(dispatch, annotation_dir)
            argilla_params = _argilla_push_params(params, dispatch)
            result = push_sample(task, dispatch_path, dataset, argilla_params)
            manifest = {
                "task_id": task.task_id,
                "annotation_id": annotation_id,
                "source": "argilla",
                "argilla_dataset": dataset,
                "dispatch_mode": dispatch_mode,
                "dispatch_path": dispatch_path,
                "sample_id": sample_id,
                "sample_path": dispatch.get("sample_path"),
                "batch_plan_id": dispatch.get("batch_plan_id"),
                "batch_manifest_path": dispatch.get("batch_manifest_path"),
                "batch_ids": dispatch.get("batch_ids") or [],
                "batch_files": dispatch.get("batch_files") or [],
                "overlap_item_ids": dispatch.get("overlap_item_ids") or [],
                "selected_overlap_item_ids": dispatch.get("selected_overlap_item_ids") or [],
                "rows": result.get("records", 0),
                "record_id_policy": result.get("record_id_policy"),
                "duplicate_record_ids": result.get("duplicate_record_ids"),
                "status": "已分发",
                "created_at": _now(),
                "result": result,
            }
            write_json(manifest, annotation_dir / "manifest.json")
            return {
                "kind": "annotation_job",
                "annotation_id": annotation_id,
                "dispatch_mode": dispatch_mode,
                "dispatch_path": dispatch_path,
                "result": result,
            }
        if action == "argilla_pull":
            from .integrations.argilla import pull_responses
            dataset_param = params.get("dataset") or ""
            lineage = _argilla_lineage_for_pull(runs_root, task.task_id, params, dataset_param)
            sample_id = (
                params.get("sample_id")
                or lineage.get("sample_id")
                or (Path(params["sample"]).parent.name if params.get("sample") else None)
            )
            dataset = params.get("dataset") or lineage.get("argilla_dataset") or _default_argilla_dataset(task.task_id, sample_id)
            if not lineage or lineage.get("argilla_dataset") != dataset:
                lineage = _argilla_lineage_for_pull(runs_root, task.task_id, params, dataset)
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
                "sample_path": lineage.get("sample_path") or params.get("sample"),
                "path": str(output),
                "rows": result.get("responses", 0),
                "created_at": _now(),
                "result": result,
            }
            for key in ("dispatch_mode", "batch_plan_id", "batch_manifest_path", "batch_ids", "batch_files", "overlap_item_ids", "annotation_id", "source_annotation_id"):
                if lineage.get(key) not in (None, "", [], {}):
                    manifest[key] = lineage[key]
            write_json(manifest, decision_dir / "manifest.json")
            return {"kind": "decision_artifact", "artifact": str(output), "decision_id": decision_id, "result": result}
        if action == "prelabel_suggest":
            from .suggestions import generate_suggestions_for_annotation_job

            annotation_id = str(params.get("annotation_id") or params.get("job_id") or "").strip()
            suggestion_id = str(params.get("suggestion_id") or "suggestions_v001").strip()
            if not annotation_id:
                raise ValueError("缺少标注任务编号 annotation_id")
            with _asset_lock(runs_root, task.task_id, f"suggestions-{annotation_id}-{suggestion_id}"):
                return generate_suggestions_for_annotation_job(
                    runs_root,
                    task,
                    annotation_id,
                    suggestion_id,
                    provider=str(params.get("provider") or "local_stub"),
                    prompt_version=str(params.get("prompt_version") or "v001"),
                    publish=bool(params.get("publish")),
                    argilla=params.get("argilla") or {},
                )
        if action == "audit":
            from .audit import audit_run
            return {"summary": audit_run(task, params["run"]), "kind": "audit"}
        if action == "agreement_audit":
            from .agreement import audit_agreement
            audit_id = str(params.get("audit_id") or "").strip()
            if not audit_id:
                raise ValueError("缺少一致性检查编号 audit_id")
            sample = params.get("sample")
            decisions = params.get("decisions")
            if not sample:
                raise ValueError("缺少样本文件 sample")
            if not decisions:
                raise ValueError("缺少标注决策文件 decisions")
            with _asset_lock(runs_root, task.task_id, f"agreement-audit-{audit_id}"):
                result = audit_agreement(
                    task,
                    sample,
                    decisions,
                    audit_id,
                    min_submitted=int(params.get("min_submitted", 1)),
                )
            return {"kind": "agreement_audit", **result}
        if action == "merge":
            from .merge import merge_run
            return {"summary": merge_run(task, params["run"]), "kind": "merge"}
        if action == "gold":
            if params.get("sample") and params.get("decisions"):
                if not _has_passing_agreement_audit(task, params["sample"], params["decisions"]):
                    raise ValueError("构建训练集前必须先完成并通过同一样本和标注结果的一致性检查")
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


def run_action(runs_root: str | Path, task_path: str, action: str, params: dict) -> dict:
    task = with_runs_root(load_task(task_path), runs_root)
    if action == "batch":
        return _run_batch_action(runs_root, task, params)
    raise ValueError(f"run_action does not support action: {action}")


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


def _annotation_job_dir(runs_root: str | Path, task_id: str, annotation_id: str) -> Path:
    if not _safe_segment(task_id) or not _safe_segment(annotation_id):
        raise ValueError("非法任务或标注任务编号")
    return Path(runs_root) / task_id / "annotation_jobs" / annotation_id


def _archived_sample_exists(runs_root: str | Path, task_id: str, sample_id: str) -> bool:
    archive = _archive_dir(runs_root, task_id, "samples")
    return archive.is_dir() and any(path.name.startswith(f"{sample_id}__") for path in archive.iterdir())


def _archived_annotation_job_exists(runs_root: str | Path, task_id: str, annotation_id: str) -> bool:
    archive = _archive_dir(runs_root, task_id, "annotation_jobs")
    return archive.is_dir() and any(path.name.startswith(f"{annotation_id}__") for path in archive.iterdir())


def dependencies_for_annotation_job(runs_root: str | Path, task_id: str, annotation_id: str) -> list[dict[str, str]]:
    base = Path(runs_root) / task_id / "decisions"
    if not base.is_dir():
        return []
    deps: list[dict[str, str]] = []
    annotation_manifest_path = _annotation_job_dir(runs_root, task_id, annotation_id) / "manifest.json"
    annotation_manifest = read_json(annotation_manifest_path) if annotation_manifest_path.exists() else {}
    dataset = str(annotation_manifest.get("argilla_dataset") or annotation_manifest.get("dataset") or "")
    for child in sorted(path for path in base.iterdir() if path.is_dir()):
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = read_json(manifest_path)
        decision_id = str(manifest.get("decision_id") or child.name)
        decision_annotation = str(manifest.get("annotation_id") or manifest.get("source_annotation_id") or "")
        decision_dataset = str(manifest.get("argilla_dataset") or manifest.get("dataset") or "")
        if decision_annotation == annotation_id or (dataset and decision_dataset == dataset):
            deps.append({"kind": "标注结果", "id": decision_id})
    return deps


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
                source: str = "upload", metadata: dict[str, Any] | None = None,
                raw_source_path: str | Path | None = None) -> dict:
    import_id = str(import_id or "").strip()
    metadata = metadata or {}
    source_file = Path(raw_source_path) if raw_source_path is not None else None
    if source_file is not None and not source_file.is_file():
        raise ValueError(f"导入源文件不存在: {source_file}")
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
                    mismatches = _metadata_mismatches(manifest, metadata)
                    if mismatches:
                        raise ValueError(
                            f"导入编号已存在且内容相同，但数据湖血缘不同: {', '.join(mismatches)}。"
                            "请使用新的导入编号。"
                        )
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
            for key in (
                "lake_registry_uri",
                "source_dataset_id",
                "source_manifest_uri",
                "source_object_uri",
                "source_object_path",
                "source_object_bytes",
                "source_object_sha256",
                "source_content_sha256",
                "source_rows",
                "source_asset_type",
                "source_id_field",
                "source_unique_ids",
                "source_created_by",
                "source_upstream_uri",
                "source_sampling_strategy",
            ):
                if metadata.get(key) not in (None, ""):
                    manifest[key] = metadata[key]
            if metadata:
                manifest["source_metadata"] = metadata
            staging = _staging_dir(runs_root, task.task_id, "imports", import_id)
            try:
                if source_file is not None:
                    _copy_file_fsync(source_file, staging / "raw.jsonl")
                else:
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
                details={"content_sha256": incoming_hash, "rows": profile["rows"], "source": source, **metadata},
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
            details={"error": str(exc), "source": source, **metadata},
        )
        raise


def import_from_data_lake(runs_root: str | Path, task, import_id: str | None = None,
                          overrides: dict[str, Any] | None = None,
                          max_bytes: int | None = None) -> dict:
    from .data_lake import default_import_id, materialize_source, read_source_rows

    source = materialize_source(task, overrides=overrides, max_bytes=max_bytes)
    try:
        rows = read_source_rows(source)
        profile = _profile_jsonl_from_rows(rows, id_field=task.id_field)
        if profile["rows"] != source.source_object_rows:
            raise ValueError(f"数据湖对象行数校验失败: manifest={source.source_object_rows}, actual={profile['rows']}")
        if profile["unique_ids"] != source.source_unique_ids:
            raise ValueError(f"数据湖对象唯一 ID 数校验失败: manifest={source.source_unique_ids}, actual={profile['unique_ids']}")
        incoming_hash = _content_hash(rows)
        lineage = source.lineage(content_sha256=incoming_hash)
        target_import_id = str(import_id or default_import_id(task, source)).strip()
        result = save_import(
            runs_root,
            task,
            target_import_id,
            rows,
            source="data_lake",
            metadata=lineage,
            raw_source_path=source.local_path,
        )
        result["data_lake"] = lineage
        return result
    finally:
        if source.local_path.exists():
            source.local_path.unlink()


def start_data_lake_import(runs_root: str | Path, task, import_id: str | None = None,
                           overrides: dict[str, Any] | None = None,
                           max_bytes: int | None = None) -> dict:
    overrides = dict(overrides or {})
    params = {
        "task_id": task.task_id,
        "import_id": import_id,
        "overrides": overrides,
        "max_bytes": max_bytes,
    }
    job = create_job("data_lake_import", params, _jobs_dir(Path(runs_root), task.task_id))

    def target(j: Job) -> dict:
        j.log(f"data_lake_import task={task.task_id}")
        if import_id:
            j.log(f"requested_import_id={import_id}")
        if overrides:
            j.log("使用 data_lake override 配置执行导入")
        lock_name = f"data-lake-import-{import_id or 'default'}"
        with _asset_lock(runs_root, task.task_id, lock_name):
            result = import_from_data_lake(
                runs_root,
                task,
                import_id=import_id,
                overrides=overrides,
                max_bytes=max_bytes,
            )
        j.log(
            "导入完成 "
            f"import_id={result.get('import_id')} rows={result.get('rows')} action={result.get('action')}"
        )
        return {
            "kind": "import",
            "import_id": result.get("import_id"),
            "action": result.get("action"),
            "rows": result.get("rows"),
            "import": result,
        }

    run_job(job, target)
    return job.to_dict()


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


def archive_annotation_job(runs_root: str | Path, task_id: str, annotation_id: str, *, reason: str = "") -> dict:
    try:
        with _asset_lock(runs_root, task_id, f"annotation-job-{annotation_id}"):
            item = _annotation_job_dir(runs_root, task_id, annotation_id)
            if not item.is_dir():
                raise ValueError(f"标注任务不存在: {annotation_id}")
            deps = dependencies_for_annotation_job(runs_root, task_id, annotation_id)
            if deps:
                names = ", ".join(f"{dep['kind']}:{dep['id']}" for dep in deps)
                raise ValueError(f"标注任务已被下游资产使用，不能归档。关联资产: {names}")
            manifest_path = item / "manifest.json"
            manifest = read_json(manifest_path) if manifest_path.exists() else {
                "task_id": task_id,
                "annotation_id": annotation_id,
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
            target = _archive_dir(runs_root, task_id, "annotation_jobs") / f"{annotation_id}__{stamp}"
            _move_directory(item, target)
            result = {
                "task_id": task_id,
                "annotation_id": annotation_id,
                "archived": True,
                "archive_path": str(target),
                "archived_at": archived_at,
            }
            record_audit_event(
                runs_root,
                task_id,
                "annotation_job.archive",
                asset_type="annotation_job",
                asset_id=annotation_id,
                details={"archive_path": str(target), "reason": reason, "remote_affected": False},
            )
            return result
    except Exception as exc:
        record_audit_event(
            runs_root,
            task_id,
            "annotation_job.archive",
            asset_type="annotation_job",
            asset_id=annotation_id or "-",
            status="failed",
            details={"error": str(exc), "reason": reason, "remote_affected": False},
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
            item = read_json(manifest)
            item.setdefault("annotation_id", dd.name)
            item["manifest_path"] = str(manifest)
            out.append(item)
        else:
            out.append({
                "task_id": task_id,
                "annotation_id": dd.name,
                "source": "unknown",
            })
    return out


def list_agreement_audits(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "agreement_audits"
    if not base.is_dir():
        return out
    for dd in sorted(p for p in base.iterdir() if p.is_dir()):
        summary_path = dd / "summary.json"
        if summary_path.exists():
            summary = read_json(summary_path)
            out.append({
                **summary,
                "audit_id": summary.get("audit_id") or dd.name,
                "summary_path": str(summary_path),
            })
        else:
            out.append({
                "task_id": task_id,
                "audit_id": dd.name,
                "summary_path": str(summary_path),
                "state": "incomplete",
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
            "agreement_audits",
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


def _list_batch_manifests(sample_dir: Path) -> list[dict[str, Any]]:
    batches_dir = sample_dir / "batches"
    if not batches_dir.is_dir():
        return []
    manifests: list[tuple[int, str, dict[str, Any]]] = []
    for plan_dir in sorted(p for p in batches_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        manifest_path = plan_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest_data = read_json(manifest_path)
        manifest_data.setdefault("plan_id", plan_dir.name)
        manifest_data["manifest_path"] = str(manifest_path)
        manifest_data["plan_dir"] = str(plan_dir)
        try:
            mtime_ns = manifest_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        manifests.append((mtime_ns, plan_dir.name, manifest_data))
    manifests.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in manifests]


def _batch_count_from_manifest(manifest: dict[str, Any] | None) -> int:
    if not manifest:
        return 0
    batch_count = manifest.get("batch_count")
    if batch_count is not None:
        return int(batch_count)
    batches = manifest.get("batches")
    if isinstance(batches, list):
        return len(batches)
    return 0


def list_samples(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "samples"
    if not base.is_dir():
        return out
    for sd in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith("_")):
        manifest = sd / "manifest.json"
        manifest_data = read_json(manifest) if manifest.exists() else None
        batch_manifests = _list_batch_manifests(sd)
        latest_batch_manifest = batch_manifests[-1] if batch_manifests else None
        out.append({
            "sample_id": sd.name,
            "path": str(sd / "sample.jsonl"),
            "manifest": manifest_data,
            "state": (manifest_data or {}).get("state", "active"),
            "dependencies": dependencies_for_sample(runs_root, task_id, sd.name),
            "batch_manifests": batch_manifests,
            "latest_batch_manifest": latest_batch_manifest,
            "batch_manifest": latest_batch_manifest,
            "batches": batch_manifests,
            "batch_count": _batch_count_from_manifest(latest_batch_manifest),
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
