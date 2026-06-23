from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_task
from .io import read_json
from .jobs import Job, create_job, run_job


# --- core object: task -------------------------------------------------------

def list_tasks(tasks_root: Path) -> list[dict]:
    out: list[dict] = []
    root = Path(tasks_root)
    if not root.exists():
        return out
    for yml in sorted(root.rglob("task.yaml")):
        try:
            task = load_task(yml)
            out.append({
                "task_id": task.task_id,
                "path": str(yml),
                "id_field": task.id_field,
                "primary_label": task.primary_label,
                "auxiliary_labels": task.auxiliary_labels,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"task_id": None, "path": str(yml), "error": str(exc)})
    return out


def _jobs_dir(runs_root: Path, task_id: str) -> Path:
    return Path(runs_root) / task_id / "_jobs"


# --- core object: run + jobs -------------------------------------------------

def start_action(runs_root: Path, task_path: str, action: str, params: dict) -> dict:
    task = load_task(task_path)
    jobs_dir = _jobs_dir(runs_root, task.task_id)
    job = create_job(action, dict(params, task=task_path), jobs_dir)

    def target(j: Job) -> dict:
        j.log(f"action={action} task={task.task_id}")
        if action == "sample":
            from .sampling import sample_records
            path = sample_records(task, int(params["rows"]), params["sample_id"],
                                   params.get("strategy", "random"), int(params.get("seed", 20260617)))
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
            result = push_sample(task, params["sample"], params["dataset"], params.get("argilla", {}))
            return {"kind": "argilla_dataset", "result": result}
        if action == "argilla_pull":
            from .integrations.argilla import pull_responses
            output = params.get("output") or (
                Path(runs_root) / task.task_id / "argilla" / f"{params['dataset']}.decisions.jsonl"
            )
            result = pull_responses(task, params["dataset"], output, params.get("argilla", {}))
            return {"kind": "decision", "artifact": str(output), "result": result}
        if action == "audit":
            from .audit import audit_run
            return {"summary": audit_run(task, params["run"]), "kind": "audit"}
        if action == "merge":
            from .merge import merge_run
            return {"summary": merge_run(task, params["run"]), "kind": "merge"}
        if action == "gold":
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

def list_runs(runs_root: Path, task_id: str) -> list[dict]:
    out: list[dict] = []
    base = Path(runs_root) / task_id
    if not base.is_dir():
        return out
    for run_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if run_dir.name in ("samples", "gold", "models", "schemas", "imports", "inference", "_jobs"):
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


def list_decisions(runs_root: Path, task_id: str, run_id: str) -> list[dict]:
    from .io import read_jsonl
    path = Path(runs_root) / task_id / run_id / "adjudication" / "decisions.jsonl"
    if not path.exists():
        return []
    return read_jsonl(path)


def jobs_for_task(runs_root: Path, task_id: str) -> list[dict]:
    from .jobs import list_jobs
    return list_jobs(_jobs_dir(runs_root, task_id))
