from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
from typing import Any

import yaml

from .config import load_task, with_runs_root
from .io import read_json, write_json
from .jobs import Job, create_job, run_job


# --- core object: task -------------------------------------------------------

def _task_roots(tasks_root: str | Path) -> list[Path]:
    parts: list[str] = []
    for chunk in str(tasks_root).split(os.pathsep):
        parts.extend(item.strip() for item in chunk.split(","))
    return [Path(item) for item in parts if item]


def _task_file_deletable(path: Path, root: Path) -> bool:
    if root.name == "examples":
        return False
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def list_tasks(tasks_root: str | Path) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for root in _task_roots(tasks_root):
        if not root.exists():
            continue
        for yml in sorted(root.rglob("task.yaml")):
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


def delete_task(tasks_root: str | Path, task_id: str, *, runs_root: str | Path | None = None,
                delete_runs: bool = False) -> dict:
    task_id = str(task_id or "").strip()
    if not task_id or ".." in task_id or "/" in task_id or "\\" in task_id:
        raise ValueError("非法任务编号")
    for root in _task_roots(tasks_root):
        if not root.exists():
            continue
        for yml in sorted(root.rglob("task.yaml")):
            try:
                task = load_task(yml)
            except Exception:
                continue
            if task.task_id != task_id:
                continue
            if not _task_file_deletable(yml, root):
                raise ValueError("示例任务不可删除；如需隐藏示例任务，请只配置 tasks 作为任务目录")
            task_dir = yml.parent
            shutil.rmtree(task_dir)
            removed_runs = False
            if delete_runs and runs_root is not None:
                run_dir = Path(runs_root) / task_id
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                    removed_runs = True
            return {
                "task_id": task_id,
                "path": str(task_dir),
                "deleted": True,
                "deleted_runs": removed_runs,
            }
    raise ValueError(f"任务不存在: {task_id}")


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
    if task_path.exists():
        raise ValueError(f"任务已存在: {task_id}")

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
    task_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    prompt = str(spec.get("prompt", "")).strip()
    if prompt:
        (task_dir / "prompt.md").write_text(prompt + "\n", encoding="utf-8")
        raw["prompt"] = "prompt.md"
        task_path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
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
            path = sample_records(task, int(params["rows"]), params["sample_id"],
                                   params.get("strategy", "random"), int(params.get("seed", 20260617)),
                                   params.get("source"))
            return {"artifact": str(path), "kind": "sample"}
        if action == "batch":
            from .batching import batch_records
            out = Path(runs_root) / task.task_id / "samples" / Path(params["sample"]).parent.name
            paths = batch_records(params["sample"], out, int(params["batch_size"]))
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


def list_imports(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id / "imports"
    if not base.is_dir():
        return out
    for item in sorted(p for p in base.iterdir() if p.is_dir()):
        path = item / "raw.jsonl"
        manifest = item / "manifest.json"
        if manifest.exists():
            out.append(read_json(manifest))
        else:
            out.append({
                "import_id": item.name,
                "path": str(path),
                "rows": _count_jsonl(path),
            })
    return out


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


def list_runs(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id
    if not base.is_dir():
        return out
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if run_dir.name in (
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
    for sd in sorted(p for p in base.iterdir() if p.is_dir()):
        manifest = sd / "manifest.json"
        out.append({
            "sample_id": sd.name,
            "path": str(sd / "sample.jsonl"),
            "manifest": read_json(manifest) if manifest.exists() else None,
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
