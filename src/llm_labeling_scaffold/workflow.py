from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import TaskConfig, load_task
from .io import read_json, write_json
from .jobs import get_job
from .pipeline import start_action, start_data_lake_import
from .profiles import profile_definition


WORKFLOW_STATUSES = {"pending", "running", "waiting_for_human", "succeeded", "failed"}
STAGE_STATUSES = WORKFLOW_STATUSES | {"blocked"}
QUALITY_GATE_ACTIONS = {"gold", "gold_build", "train", "infer", "batch_infer"}

_STATE_LOCK = threading.RLock()
_WORKFLOW_THREADS: dict[str, threading.Thread] = {}
_WORKFLOW_INPUTS: dict[str, dict[str, Any]] = {}


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
    parts = set(normalized.split("_"))
    if parts & {"password", "secret", "token", "authorization", "credential", "credentials", "bearer", "cookie"}:
        return True
    if {"api", "key"}.issubset(parts) or {"access", "key"}.issubset(parts) or {"private", "key"}.issubset(parts):
        return True
    return "rclone" in parts and "config" in parts


class WorkflowError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(_jsonable(item) for item in value)
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


def _fingerprint(value: Any) -> str:
    payload = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or ".." in text:
        raise WorkflowError(f"非法{label}: {value}")
    return text


def workflow_dir(runs_root: str | Path, task_id: str, workflow_id: str) -> Path:
    return Path(runs_root) / _safe_segment(task_id, "任务编号") / "workflow_runs" / _safe_segment(workflow_id, "workflow 编号")


def _manifest_path(runs_root: str | Path, task_id: str, workflow_id: str) -> Path:
    return workflow_dir(runs_root, task_id, workflow_id) / "manifest.json"


def _stage_path(base: Path, stage_id: str) -> Path:
    return base / "stages" / f"{_safe_segment(stage_id, '阶段编号')}.json"


def _attempt_manifest_path(base: Path, stage_id: str, attempt: int) -> Path:
    return base / "stages" / _safe_segment(stage_id, "阶段编号") / "attempts" / f"attempt_{attempt:04d}.json"


def _load_task_config(task: TaskConfig | str | Path, runs_root: str | Path) -> TaskConfig:
    loaded = task if isinstance(task, TaskConfig) else load_task(task)
    raw = dict(loaded.raw)
    raw["runs_dir"] = str(Path(runs_root))
    return TaskConfig(path=loaded.path, raw=raw)


def _stage_definitions(profile_id: str) -> list[dict[str, Any]]:
    profile = profile_definition(profile_id)
    raw_stages = profile.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise WorkflowError(f"流程预设没有阶段: {profile_id}")
    stages: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in raw_stages:
        if not isinstance(raw, Mapping):
            raise WorkflowError(f"流程阶段必须是对象: {profile_id}")
        stage_id = _safe_segment(str(raw.get("id") or ""), "阶段编号")
        if stage_id in ids:
            raise WorkflowError(f"流程阶段编号重复: {stage_id}")
        ids.add(stage_id)
        depends_on = [str(item) for item in raw.get("depends_on") or []]
        stages.append(
            {
                "id": stage_id,
                "title": str(raw.get("title") or raw.get("name") or stage_id),
                "action": str(raw.get("action") or stage_id),
                "depends_on": depends_on,
                "description": str(raw.get("description") or ""),
            }
        )
    if profile_id == "manual_labeling_quality_control_v1":
        ids = {stage["id"] for stage in stages}
        pilot_index = next((index for index, stage in enumerate(stages) if stage["id"] == "pilot_calibration"), None)
        if pilot_index is not None:
            if "pilot_argilla_dispatch" not in ids:
                dispatch = {
                    "id": "pilot_argilla_dispatch",
                    "title": "试标分发",
                    "action": "argilla_push",
                    "depends_on": ["pilot_calibration"],
                    "description": "将试标批次推送到 Argilla，等待标注员完成校准标注。",
                }
                stages.insert(pilot_index + 1, dispatch)
                ids.add(dispatch["id"])
            dispatch_index = next(index for index, stage in enumerate(stages) if stage["id"] == "pilot_argilla_dispatch")
            if "pilot_argilla_pull" not in ids:
                pull = {
                    "id": "pilot_argilla_pull",
                    "title": "试标回收",
                    "action": "argilla_pull",
                    "depends_on": ["pilot_argilla_dispatch"],
                    "description": "回收试标批次的人工提交，未产生提交时保持等待。",
                }
                stages.insert(dispatch_index + 1, pull)
                ids.add(pull["id"])
        consistency = next((stage for stage in stages if stage["id"] == "consistency_check"), None)
        if consistency is not None:
            consistency["depends_on"] = [
                "pilot_argilla_pull" if dependency == "pilot_calibration" else dependency
                for dependency in consistency.get("depends_on") or []
            ]
    for stage in stages:
        missing = [item for item in stage["depends_on"] if item not in ids]
        if missing:
            raise WorkflowError(f"阶段 {stage['id']} 依赖不存在: {', '.join(missing)}")
    _topological_order(stages)
    return stages


def _topological_order(stages: list[dict[str, Any]]) -> list[str]:
    order = {stage["id"]: index for index, stage in enumerate(stages)}
    pending = {stage["id"]: set(stage["depends_on"]) for stage in stages}
    result: list[str] = []
    while pending:
        ready = sorted((stage_id for stage_id, deps in pending.items() if not deps), key=order.get)
        if not ready:
            raise WorkflowError("流程阶段依赖存在循环")
        for stage_id in ready:
            result.append(stage_id)
            pending.pop(stage_id)
        for deps in pending.values():
            deps.difference_update(ready)
    return result


def _stage_params(stages: list[dict[str, Any]], params: Mapping[str, Any] | None,
                  explicit: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    source = dict(params or {})
    nested: dict[str, Any] = {}
    for key in ("stages", "stage_params"):
        value = source.pop(key, None)
        if isinstance(value, Mapping):
            nested.update(value)
    if isinstance(explicit, Mapping):
        nested.update(explicit)
    stage_ids = {stage["id"] for stage in stages}
    actions = {stage["action"] for stage in stages}
    for key in stage_ids | actions:
        value = source.get(key)
        if isinstance(value, Mapping):
            nested.setdefault(key, value)
    common = {key: value for key, value in source.items() if key not in stage_ids and key not in actions}
    result: dict[str, dict[str, Any]] = {}
    for stage in stages:
        values = dict(common)
        for key in (stage["action"], stage["id"]):
            value = nested.get(key)
            if isinstance(value, Mapping):
                values.update(value)
        result[stage["id"]] = _jsonable(values)
    return result


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                output[str(key)] = "[REDACTED]"
            else:
                output[str(key)] = _redact(item)
        return output
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return _jsonable(value)


def _contains_redacted(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redacted(item) for item in value)
    return value == "[REDACTED]"


def _candidate_path(value: Any, key: str | None = None) -> Path | None:
    if not isinstance(value, (str, Path)):
        return None
    text = str(value).strip()
    if not text:
        return None
    key_text = str(key or "").lower()
    path_key = any(token in key_text for token in ("path", "file", "artifact", "output", "sample", "decision", "gold", "model", "corpus", "manifest", "raw"))
    candidate = Path(text).expanduser()
    if not path_key and not candidate.exists():
        return None
    return candidate


def _jsonl_rows(path: Path) -> int | None:
    if path.suffix.lower() != ".jsonl" or not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _file_reference(value: Any, key: str | None = None) -> dict[str, Any] | None:
    path = _candidate_path(value, key)
    if path is None:
        return None
    reference: dict[str, Any] = {"path": str(path)}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        reference.update({"exists": True, "bytes": path.stat().st_size, "sha256": digest.hexdigest()})
        rows = _jsonl_rows(path)
        if rows is not None:
            reference["rows"] = rows
    else:
        reference["exists"] = False
    return reference


def _references(value: Any, key: str | None = None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        for item_key, item in value.items():
            result.extend(_references(item, str(item_key)))
    elif isinstance(value, list):
        for item in value:
            result.extend(_references(item, key))
    else:
        reference = _file_reference(value, key)
        if reference is not None:
            result.append(reference)
    unique: dict[str, dict[str, Any]] = {}
    for item in result:
        unique[item["path"]] = item
    return [unique[path] for path in sorted(unique)]


def _find_values(value: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in keys and item not in (None, "", [], {}):
                found.append(item)
            found.extend(_find_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_values(item, keys))
    return found


def _prior_value(stages: list[dict[str, Any]], keys: set[str]) -> Any:
    for stage in reversed(stages):
        values = _find_values(stage.get("result") or {}, keys)
        if values:
            return values[0]
    return None


def _prior_action_value(stages: list[dict[str, Any]], actions: set[str], keys: set[str]) -> Any:
    for stage in reversed(stages):
        if stage.get("action") in actions:
            values = _find_values(stage.get("result") or {}, keys)
            if values:
                return values[0]
    return None


def _hydrate_params(stage: dict[str, Any], params: Mapping[str, Any], previous: list[dict[str, Any]]) -> dict[str, Any]:
    output = dict(params)
    action = stage["action"]
    if action == "sample":
        if not output.get("source_import_id"):
            output["source_import_id"] = _prior_value(previous, {"import_id"})
        if not output.get("source"):
            output["source"] = _prior_value(previous, {"path", "raw_path", "raw"})
    elif action == "argilla_push":
        if not output.get("sample") and not output.get("sample_path"):
            output["sample"] = _prior_action_value(previous, {"sample"}, {"sample_path", "sample", "artifact"})
        if not output.get("sample") and not output.get("sample_path") and not output.get("batch_manifest_path"):
            output["sample"] = _prior_value(previous, {"sample_path", "sample", "artifact", "dispatch_path"})
        if not output.get("sample_id"):
            output["sample_id"] = _prior_action_value(previous, {"batch", "argilla_push"}, {"sample_id"})
        if not output.get("sample_id"):
            output["sample_id"] = _prior_value(previous, {"sample_id"})
        if not output.get("batch_manifest_path"):
            output["batch_manifest_path"] = _prior_action_value(
                previous, {"batch"}, {"batch_manifest_path", "manifest_path", "manifest"}
            )
        if not output.get("batch_plan_id"):
            output["batch_plan_id"] = _prior_action_value(previous, {"batch"}, {"batch_plan_id", "plan_id"})
        if not output.get("dispatch_mode") and (output.get("batch_manifest_path") or output.get("batch_plan_id")):
            output["dispatch_mode"] = "batch_plan"
    elif action == "argilla_pull":
        if not output.get("dataset"):
            output["dataset"] = _prior_action_value(previous, {"argilla_push"}, {"dataset", "argilla_dataset"})
        if not output.get("sample") and not output.get("sample_path"):
            output["sample"] = _prior_action_value(previous, {"sample"}, {"sample_path", "sample", "artifact"})
        if not output.get("sample_id"):
            output["sample_id"] = _prior_action_value(previous, {"batch", "argilla_push"}, {"sample_id"})
        if not output.get("annotation_id"):
            output["annotation_id"] = _prior_action_value(previous, {"argilla_push"}, {"annotation_id"})
    elif action == "agreement_audit":
        if not output.get("sample"):
            output["sample"] = _prior_action_value(previous, {"sample"}, {"sample_path", "sample", "artifact"})
        if not output.get("decisions"):
            output["decisions"] = _prior_action_value(
                previous, {"argilla_pull"}, {"decisions_path", "decisions", "path", "artifact"}
            )
    elif action == "gold":
        if not output.get("sample"):
            output["sample"] = _prior_action_value(previous, {"sample"}, {"sample_path", "sample", "artifact"})
        if not output.get("decisions"):
            output["decisions"] = _prior_action_value(
                previous, {"argilla_pull"}, {"path", "decisions_path", "decisions", "artifact"}
            )
    elif action == "train" and not output.get("gold"):
        output["gold"] = _prior_value(previous, {"artifact", "gold", "gold_path"})
    elif action == "infer" and not output.get("model"):
        output["model"] = _prior_value(previous, {"artifact", "model", "model_path"})
    return {key: value for key, value in output.items() if value not in (None, "", [], {})}


def _response_count(value: Any) -> int | None:
    values = _find_values(value, {"responses", "response_count"})
    for item in values:
        try:
            return int(item)
        except (TypeError, ValueError):
            continue
    return None


def _audit_passed(value: Any) -> bool | None:
    values = _find_values(value, {"passed"})
    for item in values:
        if isinstance(item, bool):
            return item
    return None


def _persist(manifest: dict[str, Any], base: Path) -> None:
    with _STATE_LOCK:
        manifest["updated_at"] = _now()
        stages = manifest.get("stages") or []
        for stage in stages:
            write_json({**stage, "workflow_id": manifest["workflow_id"], "task_id": manifest["task_id"]}, _stage_path(base, stage["id"]))
            for attempt in stage.get("attempt_history") or []:
                attempt_number = int(attempt.get("attempt") or 0)
                if attempt_number > 0:
                    write_json(
                        {
                            **attempt,
                            "workflow_id": manifest["workflow_id"],
                            "task_id": manifest["task_id"],
                            "stage_id": stage["id"],
                        },
                        _attempt_manifest_path(base, stage["id"], attempt_number),
                    )
        write_json(manifest, base / "manifest.json")


def _read_manifest(runs_root: str | Path, task_id: str, workflow_id: str) -> dict[str, Any] | None:
    path = _manifest_path(runs_root, task_id, workflow_id)
    if not path.is_file():
        return None
    return read_json(path)


def _initial_manifest(task: TaskConfig, profile_id: str, workflow_id: str,
                      stages: list[dict[str, Any]], stage_params: dict[str, dict[str, Any]],
                      input_refs: Any) -> dict[str, Any]:
    normalized_input_refs = _references(input_refs or [])
    input_payload = {
        "task_id": task.task_id,
        "task_path": str(task.path.resolve()),
        "task": _jsonable(task.raw),
        "profile_id": profile_id,
        "input_refs": normalized_input_refs,
    }
    params_payload = {stage["id"]: stage_params.get(stage["id"], {}) for stage in stages}
    stage_states: list[dict[str, Any]] = []
    for stage in stages:
        values = stage_params.get(stage["id"], {})
        stage_states.append(
            {
                **stage,
                "status": "pending",
                "attempts": 0,
                "current_attempt": None,
                "attempt_history": [],
                "child_job_id": None,
                "child_job": None,
                "input_fingerprint": _fingerprint(values),
                "input_refs": _references(values),
                "output_fingerprint": None,
                "output_refs": [],
                "result": None,
                "error": None,
                "blocked_reason": None,
                "recovery_required": False,
                "started_at": None,
                "finished_at": None,
            }
        )
    redacted_params_payload = _redact(params_payload)
    params_fingerprint = _fingerprint(params_payload)
    try:
        profile_snapshot = profile_definition(profile_id)
    except ValueError:
        profile_snapshot = None
    profile_fingerprint = _fingerprint(profile_snapshot) if profile_snapshot is not None else None
    stages_fingerprint = _fingerprint(
        {"profile_id": profile_id, "profile": profile_snapshot, "stages": stages}
    )
    return {
        "schema_version": 2,
        "params_fingerprint_version": 2,
        "stages_fingerprint_version": 2,
        "workflow_id": workflow_id,
        "task_id": task.task_id,
        "task_path": str(task.path.resolve()),
        "profile_id": profile_id,
        "status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "blocking_reason": None,
        "recovery_required": False,
        "input_fingerprint": _fingerprint(input_payload),
        "params_fingerprint": params_fingerprint,
        "params_recoverable": not _contains_redacted(redacted_params_payload),
        "profile_fingerprint": profile_fingerprint,
        "stages_fingerprint": stages_fingerprint,
        "workflow_fingerprint": _fingerprint(
            {"workflow_id": workflow_id, "task_id": task.task_id, "profile_id": profile_id,
             "input": _fingerprint(input_payload), "params": params_fingerprint,
             "stages": stages_fingerprint}
        ),
        "input_refs": normalized_input_refs,
        "params": redacted_params_payload,
        "stages": stage_states,
    }


def _job_result(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result")
    return result if isinstance(result, dict) else ({"value": result} if result is not None else {})


def _child_job_summary(job: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        key: job.get(key)
        for key in ("id", "kind", "status", "created_at", "started_at", "finished_at", "error")
        if job.get(key) is not None
    }
    if job.get("result") is not None:
        summary["result"] = _redact(job.get("result"))
    return summary


def _wait_for_job(job: dict[str, Any], jobs_dir: Path, poll_interval: float, timeout: float | None) -> dict[str, Any]:
    job_id = job.get("id")
    status = str(job.get("status") or "")
    if not job_id and status in {"succeeded", "failed"}:
        return job
    if not job_id:
        raise WorkflowError("子任务没有 job id")
    if status not in {"pending", "running", "succeeded", "failed"}:
        raise WorkflowError(f"子任务状态未知: {job_id} ({status or 'empty'})")
    started = time.monotonic()
    current = job
    while True:
        if current.get("status") in {"succeeded", "failed"}:
            return current
        if current.get("status") not in {"pending", "running"}:
            raise WorkflowError(f"子任务状态未知: {job_id} ({current.get('status') or 'empty'})")
        if timeout is not None and time.monotonic() - started >= timeout:
            raise WorkflowError(f"等待子任务超时: {job_id}")
        if poll_interval > 0:
            time.sleep(poll_interval)
        current = get_job(str(job_id), jobs_dir)
        if current is None:
            raise WorkflowError(f"子任务不存在，无法安全恢复: {job_id}")


def _start_stage_job(runs_root: Path, task: TaskConfig, stage: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    action = stage["action"]
    if action in {"import", "lake_import"} or stage["id"] == "lake_import":
        return start_data_lake_import(
            runs_root,
            task,
            import_id=params.get("import_id"),
            overrides=params.get("overrides") or {},
            max_bytes=params.get("max_bytes"),
        )
    return start_action(runs_root, str(task.path), action, params)


def _stage_by_id(manifest: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in manifest.get("stages") or []:
        if stage.get("id") == stage_id:
            return stage
    raise WorkflowError(f"workflow 阶段不存在: {stage_id}")


def _downstream_ids(manifest: dict[str, Any], roots: set[str]) -> set[str]:
    result = set(roots)
    changed = True
    while changed:
        changed = False
        for stage in manifest.get("stages") or []:
            if stage["id"] not in result and any(dep in result for dep in stage.get("depends_on") or []):
                result.add(stage["id"])
                changed = True
    return result


def _mark_quality_block(manifest: dict[str, Any], reason: str, roots: set[str]) -> None:
    downstream = _downstream_ids(manifest, roots)
    for stage in manifest.get("stages") or []:
        if stage["id"] in downstream and stage["status"] == "pending":
            stage["status"] = "blocked"
            stage["blocked_reason"] = reason


def _mark_waiting_after_push(manifest: dict[str, Any]) -> None:
    pushed = [stage["id"] for stage in manifest.get("stages") or [] if stage.get("action") == "argilla_push" and stage.get("status") == "succeeded"]
    for stage in manifest.get("stages") or []:
        if stage.get("status") == "pending" and stage.get("action") == "argilla_pull" and any(dep in pushed for dep in stage.get("depends_on") or []):
            stage["status"] = "waiting_for_human"
            stage["blocked_reason"] = "等待 Argilla 中产生人工标注结果"


def _has_quality_failure(manifest: dict[str, Any]) -> bool:
    return any(stage.get("quality_passed") is False for stage in manifest.get("stages") or [])


def _set_workflow_status(manifest: dict[str, Any], status: str, *, error: str | None = None,
                         blocking_reason: str | None = None) -> None:
    if status not in WORKFLOW_STATUSES:
        raise WorkflowError(f"非法 workflow 状态: {status}")
    manifest["status"] = status
    if status == "running" and not manifest.get("started_at"):
        manifest["started_at"] = _now()
    if status in {"succeeded", "failed"}:
        manifest["finished_at"] = _now()
    manifest["error"] = error
    manifest["blocking_reason"] = blocking_reason


def _set_stage_child(stage: dict[str, Any], child: Mapping[str, Any]) -> None:
    child_id = child.get("id")
    if child_id is not None:
        stage["child_job_id"] = str(child_id)
    stage["child_job"] = _child_job_summary(child)


def _attempt_output_path(value: str | Path, attempt: int) -> str:
    path = Path(value)
    suffix = path.suffix
    stem = path.name[:-len(suffix)] if suffix else path.name
    filename = f"{stem}.attempt_{attempt:04d}{suffix}"
    return str(path.with_name(filename))


def _attempt_params(
    runs_root: Path,
    task: TaskConfig,
    manifest: Mapping[str, Any],
    stage: Mapping[str, Any],
    params: Mapping[str, Any],
    attempt: int,
) -> dict[str, Any]:
    output = dict(params)
    if stage.get("action") != "argilla_pull":
        return output
    base_decision_id = str(
        output.get("decision_id")
        or f"{manifest['workflow_id']}__{stage['id']}"
    )
    decision_id = f"{base_decision_id}__attempt_{attempt:04d}"
    output["decision_id"] = decision_id
    if output.get("output"):
        output["output"] = _attempt_output_path(str(output["output"]), attempt)
    else:
        output["output"] = str(
            runs_root / task.task_id / "decisions" / decision_id / "decisions.jsonl"
        )
    return output


def _current_attempt(stage: dict[str, Any]) -> dict[str, Any]:
    history = stage.setdefault("attempt_history", [])
    attempt_number = int(stage.get("current_attempt") or stage.get("attempts") or 0)
    for attempt in reversed(history):
        if int(attempt.get("attempt") or 0) == attempt_number:
            return attempt
    attempt = {
        "attempt": attempt_number,
        "attempt_id": f"{stage.get('id')}:{attempt_number:04d}",
        "status": "running",
        "started_at": stage.get("started_at"),
        "finished_at": None,
        "input_fingerprint": stage.get("input_fingerprint"),
        "input_refs": stage.get("input_refs") or [],
        "params": {},
        "child_job_id": stage.get("child_job_id"),
        "output_fingerprint": None,
        "output_refs": [],
        "result": None,
        "error": None,
        "blocked_reason": None,
    }
    history.append(attempt)
    return attempt


def _begin_attempt(stage: dict[str, Any], params: Mapping[str, Any]) -> None:
    attempt = _current_attempt(stage)
    attempt.update(
        {
            "status": "running",
            "started_at": stage.get("started_at"),
            "finished_at": None,
            "input_fingerprint": stage.get("input_fingerprint"),
            "input_refs": stage.get("input_refs") or [],
            "params": _redact(params),
            "child_job_id": stage.get("child_job_id"),
            "error": None,
            "blocked_reason": None,
        }
    )


def _finish_attempt(
    stage: dict[str, Any],
    status: str,
    *,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    attempt = _current_attempt(stage)
    result_value = dict(result or {})
    attempt.update(
        {
            "status": status,
            "finished_at": stage.get("finished_at"),
            "child_job_id": stage.get("child_job_id"),
            "output_fingerprint": stage.get("output_fingerprint"),
            "output_refs": stage.get("output_refs") or [],
            "result": _redact(result_value) if result is not None else None,
            "error": error,
            "blocked_reason": blocked_reason,
        }
    )


def _stage_child_snapshot(stage: Mapping[str, Any]) -> dict[str, Any] | None:
    child = stage.get("child_job")
    if not isinstance(child, Mapping):
        return None
    child_id = stage.get("child_job_id") or child.get("id")
    if not child_id:
        return None
    snapshot = dict(child)
    snapshot["id"] = str(child_id)
    return snapshot


def _load_running_child(stage: Mapping[str, Any], jobs_dir: Path) -> dict[str, Any]:
    child_id = stage.get("child_job_id")
    snapshot = _stage_child_snapshot(stage)
    if not child_id:
        raise WorkflowError(f"阶段 {stage.get('id')} 已开始但没有持久化子任务编号，禁止自动重试")
    current = get_job(str(child_id), jobs_dir)
    if current is not None:
        return current
    if snapshot is not None and snapshot.get("status") in {"succeeded", "failed"}:
        return snapshot
    raise WorkflowError(f"阶段 {stage.get('id')} 的子任务不存在，禁止自动重试: {child_id}")


def _complete_stage(manifest: dict[str, Any], stage: dict[str, Any], child: Mapping[str, Any]) -> str:
    _set_stage_child(stage, child)
    if child.get("status") != "succeeded":
        stage["status"] = "failed"
        stage["error"] = str(child.get("error") or "子任务执行失败")
        stage["finished_at"] = _now()
        _finish_attempt(stage, "failed", error=stage["error"])
        return "failed"
    result = _job_result(dict(child))
    stage["result"] = result
    stage["output_fingerprint"] = _fingerprint(result)
    stage["output_refs"] = _references(result)
    stage["finished_at"] = _now()
    if stage.get("action") == "argilla_pull" and _response_count(result) == 0:
        stage["status"] = "waiting_for_human"
        stage["blocked_reason"] = "Argilla 尚无人工提交结果"
        _finish_attempt(
            stage,
            "waiting_for_human",
            result=result,
            blocked_reason=stage["blocked_reason"],
        )
        return "waiting"
    stage["status"] = "succeeded"
    stage["error"] = None
    if stage.get("action") == "agreement_audit":
        passed = _audit_passed(result)
        if passed is not None:
            stage["quality_passed"] = passed
        if passed is False:
            reason = "一致性检查未通过，禁止构建训练集、训练模型和批量推理"
            _mark_quality_block(manifest, reason, {stage["id"]})
            stage["blocked_reason"] = reason
            _finish_attempt(stage, "succeeded", result=result, blocked_reason=reason)
            return "quality_failed"
    _finish_attempt(stage, "succeeded", result=result)
    return "succeeded"


def _after_stage_completion(manifest: dict[str, Any], stage: dict[str, Any]) -> bool:
    if stage.get("action") != "argilla_push":
        return False
    _mark_waiting_after_push(manifest)
    return any(item.get("status") == "waiting_for_human" for item in manifest.get("stages") or [])


def _mark_recovery_failure(manifest: dict[str, Any], stage: dict[str, Any], error: str) -> None:
    stage["status"] = "failed"
    stage["error"] = error
    stage["blocked_reason"] = error
    stage["recovery_required"] = True
    stage["finished_at"] = _now()
    _finish_attempt(stage, "failed", error=error, blocked_reason=error)
    manifest["recovery_required"] = True
    manifest["blocking_reason"] = error


def _recover_running_stage(manifest: dict[str, Any], jobs_dir: Path, poll_interval: float,
                           timeout: float | None) -> str | None:
    running = [stage for stage in manifest.get("stages") or [] if stage.get("status") == "running"]
    if not running:
        return None
    if len(running) > 1:
        raise WorkflowError("workflow 同时存在多个运行中的阶段，无法安全恢复")
    stage = running[0]
    try:
        child = _load_running_child(stage, jobs_dir)
        child = _wait_for_job(child, jobs_dir, poll_interval, timeout)
    except WorkflowError as exc:
        _mark_recovery_failure(manifest, stage, str(exc))
        return "failed"
    return _complete_stage(manifest, stage, child)


def _run_workflow(key: str, runs_root: Path, task: TaskConfig, manifest: dict[str, Any],
                  raw_stage_params: dict[str, dict[str, Any]], poll_interval: float,
                  timeout: float | None) -> None:
    base = workflow_dir(runs_root, task.task_id, manifest["workflow_id"])
    jobs_dir = runs_root / task.task_id / "_jobs"
    try:
        manifest["recovery_required"] = bool(manifest.get("recovery_required"))
        _set_workflow_status(manifest, "running", error=None, blocking_reason=None)
        _persist(manifest, base)
        recovery_stage = next(
            (stage for stage in manifest.get("stages") or [] if stage.get("status") == "running"),
            None,
        )
        recovery_stage_id = recovery_stage.get("id") if recovery_stage is not None else None
        recovered = _recover_running_stage(manifest, jobs_dir, poll_interval, timeout)
        if recovered is not None:
            _persist(manifest, base)
            if recovered == "failed":
                failed = next(stage for stage in manifest.get("stages") or [] if stage.get("status") == "failed")
                _set_workflow_status(manifest, "failed", error=failed.get("error") or "阶段恢复失败",
                                     blocking_reason=failed.get("blocked_reason"))
                _persist(manifest, base)
                return
            if recovered == "waiting":
                _set_workflow_status(manifest, "waiting_for_human", blocking_reason="等待 Argilla 中产生人工标注结果")
                _persist(manifest, base)
                return
            if recovered == "quality_failed":
                failed = next(stage for stage in manifest.get("stages") or [] if stage.get("quality_passed") is False)
                reason = failed.get("blocked_reason") or "一致性检查未通过"
                _set_workflow_status(manifest, "failed", error=reason, blocking_reason=reason)
                _persist(manifest, base)
                return
            recovered_stage = (
                _stage_by_id(manifest, recovery_stage_id)
                if recovery_stage_id is not None
                else None
            )
            if recovered_stage is not None and _after_stage_completion(manifest, recovered_stage):
                _set_workflow_status(manifest, "waiting_for_human", blocking_reason="等待 Argilla 中产生人工标注结果")
                _persist(manifest, base)
                return
        while True:
            stages = manifest.get("stages") or []
            for stage in stages:
                if stage.get("status") == "pending":
                    dependency_states = [_stage_by_id(manifest, dep).get("status") for dep in stage.get("depends_on") or []]
                    if any(state in {"failed", "blocked"} for state in dependency_states):
                        stage["status"] = "blocked"
                        stage["blocked_reason"] = "依赖阶段未成功完成"
            if any(stage.get("status") == "failed" for stage in stages):
                failed = next(stage for stage in stages if stage.get("status") == "failed")
                _set_workflow_status(manifest, "failed", error=failed.get("error") or "阶段执行失败")
                _persist(manifest, base)
                return
            if any(stage.get("status") == "waiting_for_human" for stage in stages):
                _set_workflow_status(manifest, "waiting_for_human", blocking_reason="等待人工标注或复核")
                _persist(manifest, base)
                return
            if all(stage.get("status") in {"succeeded", "blocked"} for stage in stages):
                blocked = next((stage for stage in stages if stage.get("status") == "blocked"), None)
                if blocked is not None:
                    _set_workflow_status(manifest, "failed", error=blocked.get("blocked_reason") or "阶段被阻断")
                else:
                    _set_workflow_status(manifest, "succeeded", error=None, blocking_reason=None)
                _persist(manifest, base)
                return
            ready = [
                stage for stage in stages
                if stage.get("status") == "pending"
                and all(_stage_by_id(manifest, dep).get("status") == "succeeded" for dep in stage.get("depends_on") or [])
            ]
            if not ready:
                _set_workflow_status(manifest, "failed", error="没有可执行的阶段，流程依赖状态不一致")
                _persist(manifest, base)
                return
            stage = ready[0]
            previous = [item for item in stages if item["id"] != stage["id"] and item.get("status") == "succeeded"]
            params = _hydrate_params(stage, raw_stage_params.get(stage["id"], {}), previous)
            attempt_number = int(stage.get("attempts") or 0) + 1
            params = _attempt_params(runs_root, task, manifest, stage, params, attempt_number)
            stage["attempts"] = attempt_number
            stage["current_attempt"] = attempt_number
            stage["status"] = "running"
            stage["started_at"] = _now()
            stage["error"] = None
            stage["blocked_reason"] = None
            stage["input_fingerprint"] = _fingerprint(params)
            stage["input_refs"] = _references(params)
            _begin_attempt(stage, params)
            _persist(manifest, base)
            try:
                child = _start_stage_job(runs_root, task, stage, params)
                if not isinstance(child, Mapping):
                    raise WorkflowError("子任务返回值不是对象")
                _set_stage_child(stage, child)
                _persist(manifest, base)
                child_job = _wait_for_job(child, runs_root / task.task_id / "_jobs", poll_interval, timeout)
                outcome = _complete_stage(manifest, stage, child_job)
                _persist(manifest, base)
                if outcome == "failed":
                    _set_workflow_status(manifest, "failed", error=stage.get("error") or "阶段执行失败")
                    _persist(manifest, base)
                    return
                if outcome == "waiting":
                    _set_workflow_status(manifest, "waiting_for_human", blocking_reason=stage["blocked_reason"])
                    _persist(manifest, base)
                    return
                if outcome == "quality_failed":
                    reason = stage.get("blocked_reason") or "一致性检查未通过"
                    _set_workflow_status(manifest, "failed", error=reason, blocking_reason=reason)
                    _persist(manifest, base)
                    return
                if _after_stage_completion(manifest, stage):
                    _set_workflow_status(manifest, "waiting_for_human", blocking_reason="等待 Argilla 中产生人工标注结果")
                    _persist(manifest, base)
                    return
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                if stage.get("child_job_id") and "子任务不存在" in str(exc):
                    _mark_recovery_failure(manifest, stage, error)
                else:
                    stage["status"] = "failed"
                    stage["error"] = error
                    stage["finished_at"] = _now()
                    _finish_attempt(stage, "failed", error=error, blocked_reason=stage.get("blocked_reason"))
                _set_workflow_status(manifest, "failed", error=error,
                                     blocking_reason=stage.get("blocked_reason"))
                _persist(manifest, base)
                return
    finally:
        with _STATE_LOCK:
            current = _WORKFLOW_THREADS.get(key)
            if current is threading.current_thread():
                _WORKFLOW_THREADS.pop(key, None)


def _start_thread(key: str, runs_root: Path, task: TaskConfig, manifest: dict[str, Any],
                  raw_stage_params: dict[str, dict[str, Any]], poll_interval: float,
                  timeout: float | None) -> bool:
    with _STATE_LOCK:
        current = _WORKFLOW_THREADS.get(key)
        if current is not None and current.is_alive():
            return False
        _WORKFLOW_INPUTS[key] = deepcopy(raw_stage_params)
        thread = threading.Thread(
            target=_run_workflow,
            args=(key, runs_root, task, manifest, raw_stage_params, poll_interval, timeout),
            daemon=True,
            name=f"lls-workflow-{manifest['workflow_id']}",
        )
        _WORKFLOW_THREADS[key] = thread
        thread.start()
        return True


def start_workflow(
    runs_root: str | Path,
    task: TaskConfig | str | Path,
    profile_id: str | None = None,
    workflow_id: str | None = None,
    params: Mapping[str, Any] | None = None,
    *,
    profile: str | None = None,
    stage_params: Mapping[str, Any] | None = None,
    input_refs: Any = None,
    poll_interval: float = 0.1,
    timeout: float | None = 3600.0,
) -> dict[str, Any]:
    runs_root_path = Path(runs_root)
    task_config = _load_task_config(task, runs_root_path)
    selected_profile = str(profile_id or profile or task_config.profile).strip() or task_config.profile
    selected_workflow = str(workflow_id or f"workflow_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}")
    selected_workflow = _safe_segment(selected_workflow, "workflow 编号")
    stages = _stage_definitions(selected_profile)
    params_map = _stage_params(stages, params, stage_params)
    manifest = _initial_manifest(task_config, selected_profile, selected_workflow, stages, params_map, input_refs)
    path = _manifest_path(runs_root_path, task_config.task_id, selected_workflow)
    key = str(path.resolve())
    with _STATE_LOCK:
        existing = read_json(path) if path.is_file() else None
        if existing is not None:
            if (
                existing.get("input_fingerprint") != manifest["input_fingerprint"]
                or existing.get("params_fingerprint") != manifest["params_fingerprint"]
                or existing.get("stages_fingerprint") != manifest["stages_fingerprint"]
            ):
                raise WorkflowError(f"workflow 编号已存在但输入、参数或阶段定义指纹不同: {selected_workflow}")
            if existing.get("status") == "pending":
                _start_thread(key, runs_root_path, task_config, existing, params_map, poll_interval, timeout)
            return {**existing, "reused": True}
        base = path.parent
        try:
            base.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            existing = read_json(path) if path.is_file() else None
            if existing is None:
                raise WorkflowError(f"workflow 编号正在初始化，请稍后重试: {selected_workflow}")
            if (
                existing.get("input_fingerprint") != manifest["input_fingerprint"]
                or existing.get("params_fingerprint") != manifest["params_fingerprint"]
                or existing.get("stages_fingerprint") != manifest["stages_fingerprint"]
            ):
                raise WorkflowError(f"workflow 编号已存在但输入、参数或阶段定义指纹不同: {selected_workflow}")
            return {**existing, "reused": True}
        _persist(manifest, base)
        _start_thread(key, runs_root_path, task_config, manifest, params_map, poll_interval, timeout)
        return {**manifest, "reused": False}


def resume_workflow(
    runs_root: str | Path,
    task: TaskConfig | str | Path | None,
    workflow_id: str,
    *,
    params: Mapping[str, Any] | None = None,
    profile_id: str | None = None,
    poll_interval: float = 0.1,
    timeout: float | None = 3600.0,
) -> dict[str, Any]:
    runs_root_path = Path(runs_root)
    workflow_text = _safe_segment(workflow_id, "workflow 编号")
    if task is None:
        current = next((item for item in list_workflows(runs_root_path, None) if item.get("workflow_id") == workflow_text), None)
        if current is None:
            raise WorkflowError(f"workflow 不存在: {workflow_text}")
        task = current.get("task_path")
    task_config = _load_task_config(task, runs_root_path)
    manifest = _read_manifest(runs_root_path, task_config.task_id, workflow_text)
    if manifest is None:
        raise WorkflowError(f"workflow 不存在: {workflow_text}")
    selected_profile = str(profile_id or manifest.get("profile_id") or task_config.profile)
    stages = _stage_definitions(selected_profile)
    manifest_path = _manifest_path(runs_root_path, task_config.task_id, workflow_text)
    key = str(manifest_path.resolve())
    if params is not None:
        params_map = _stage_params(stages, params)
    else:
        params_map = _WORKFLOW_INPUTS.get(key)
        if params_map is None:
            redacted_params = manifest.get("params") or {}
            if manifest.get("params_recoverable") is False or _contains_redacted(redacted_params):
                raise WorkflowError("workflow 含敏感参数，进程重启后恢复必须显式提供 params")
            params_map = _stage_params(stages, redacted_params)
    probe = _initial_manifest(task_config, selected_profile, workflow_text, stages, params_map, manifest.get("input_refs"))
    if (
        probe["input_fingerprint"] != manifest.get("input_fingerprint")
        or probe["params_fingerprint"] != manifest.get("params_fingerprint")
        or probe["stages_fingerprint"] != manifest.get("stages_fingerprint")
    ):
        raise WorkflowError(f"workflow 编号已存在但恢复输入、参数或阶段定义指纹不同: {workflow_text}")
    while True:
        waiting_thread: threading.Thread | None = None
        with _STATE_LOCK:
            latest = _read_manifest(runs_root_path, task_config.task_id, workflow_text)
            if latest is None:
                raise WorkflowError(f"workflow 不存在: {workflow_text}")
            manifest = latest
            if (
                probe["input_fingerprint"] != manifest.get("input_fingerprint")
                or probe["params_fingerprint"] != manifest.get("params_fingerprint")
                or probe["stages_fingerprint"] != manifest.get("stages_fingerprint")
            ):
                raise WorkflowError(f"workflow 编号已存在但恢复输入、参数或阶段定义指纹不同: {workflow_text}")
            if manifest.get("status") == "waiting_for_human":
                current = _WORKFLOW_THREADS.get(key)
                if current is not None and current is not threading.current_thread() and current.is_alive():
                    waiting_thread = current
            if waiting_thread is None:
                if manifest.get("status") == "succeeded":
                    return {**manifest, "reused": True}
                if manifest.get("recovery_required"):
                    raise WorkflowError("workflow 需要人工确认子任务状态后才能恢复，请使用新的 workflow 编号")
                if manifest.get("status") == "running":
                    runtime_params = params_map if params is not None else _WORKFLOW_INPUTS.get(key, params_map)
                    _start_thread(key, runs_root_path, task_config, manifest, runtime_params, poll_interval, timeout)
                    return {**manifest, "reused": True}
                if manifest.get("status") == "failed":
                    failed = next((stage for stage in manifest.get("stages") or [] if stage.get("status") == "failed"), None)
                    if failed is None:
                        raise WorkflowError("质量门禁或阻断导致 workflow 失败，请使用新的 workflow 编号重新执行")
                    reset_ids = _downstream_ids(manifest, {failed["id"]})
                    for stage in manifest.get("stages") or []:
                        if stage["id"] in reset_ids:
                            stage.update(
                                {
                                    "status": "pending",
                                    "error": None,
                                    "blocked_reason": None,
                                    "recovery_required": False,
                                    "finished_at": None,
                                }
                            )
                    manifest["error"] = None
                    manifest["blocking_reason"] = None
                for stage in manifest.get("stages") or []:
                    if stage.get("status") == "waiting_for_human":
                        stage.update(
                            {
                                "status": "pending",
                                "blocked_reason": None,
                                "error": None,
                                "recovery_required": False,
                                "finished_at": None,
                            }
                        )
                manifest["status"] = "pending"
                manifest["blocking_reason"] = None
                _persist(manifest, workflow_dir(runs_root_path, task_config.task_id, workflow_text))
                runtime_params = params_map if params is not None else _WORKFLOW_INPUTS.get(key, params_map)
                _start_thread(key, runs_root_path, task_config, manifest, runtime_params, poll_interval, timeout)
                return {**manifest, "reused": False}
        waiting_thread.join()


def get_workflow_status(runs_root: str | Path, task_id: str, workflow_id: str) -> dict[str, Any] | None:
    return _read_manifest(runs_root, task_id, workflow_id)


def workflow_status(runs_root: str | Path, task_id: str, workflow_id: str) -> dict[str, Any] | None:
    return get_workflow_status(runs_root, task_id, workflow_id)


def list_workflows(runs_root: str | Path, task_id: str | None = None) -> list[dict[str, Any]]:
    root = Path(runs_root)
    if task_id is None:
        candidates = root.glob("*/workflow_runs/*/manifest.json")
    else:
        candidates = (root / _safe_segment(task_id, "任务编号") / "workflow_runs").glob("*/manifest.json")
    output: list[dict[str, Any]] = []
    for path in candidates:
        try:
            output.append(read_json(path))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(output, key=lambda item: item.get("created_at", ""), reverse=True)


def list_workflow_runs(runs_root: str | Path, task_id: str | None) -> list[dict[str, Any]]:
    return list_workflows(runs_root, task_id)
