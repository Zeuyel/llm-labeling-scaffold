from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..io import read_json, write_json


def _load_mlflow():
    try:
        import mlflow
    except ImportError as exc:
        raise RuntimeError("MLflow integration requires `pip install -e '.[mlflow]'`") from exc
    return mlflow


def log_training_result(task_id: str, model_id: str, result: dict[str, Any], params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    mlflow = _load_mlflow()
    tracking_uri = params.get("tracking_uri") or os.environ.get("MLFLOW_TRACKING_URI")
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)
    experiment = params.get("experiment") or os.environ.get("MLFLOW_EXPERIMENT_NAME") or task_id
    mlflow.set_experiment(experiment)

    with mlflow.start_run(run_name=model_id) as run:
        mlflow.set_tag("task_id", task_id)
        mlflow.set_tag("model_id", model_id)
        mlflow.set_tag("trainer", result.get("trainer", "unknown"))
        mlflow.log_param("trainer", result.get("trainer", "unknown"))
        if "gold_path" in result:
            mlflow.log_param("gold_path", result["gold_path"])
        metrics = result.get("metrics") or {}
        for key in ("train_rows", "test_rows"):
            if key in metrics:
                mlflow.log_metric(key, metrics[key])
        report = metrics.get("classification_report", {})
        if isinstance(report, dict):
            for key, value in report.get("macro avg", {}).items():
                if isinstance(value, (int, float)):
                    mlflow.log_metric(f"macro_{key}", value)
        model_dir = result.get("model_dir")
        if model_dir and Path(model_dir).exists():
            mlflow.log_artifacts(model_dir)
        out = dict(result)
        out["mlflow"] = {
            "experiment": experiment,
            "run_id": run.info.run_id,
            "artifact_uri": run.info.artifact_uri,
        }
    if result.get("model_dir"):
        model_dir = Path(result["model_dir"])
        write_json(out, model_dir / "mlflow.json")
        manifest_path = model_dir / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        manifest["mlflow"] = out["mlflow"]
        write_json(manifest, manifest_path)
    return out
