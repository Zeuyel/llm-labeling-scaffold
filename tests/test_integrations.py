from pathlib import Path
import sys
import tempfile
import types

from llm_labeling_scaffold.integrations.mlflow import log_training_result
from llm_labeling_scaffold.io import read_json, write_json


class _RunInfo:
    run_id = "run_123"
    artifact_uri = "file:///tmp/mlruns/run_123"


class _Run:
    info = _RunInfo()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_mlflow_result_updates_model_manifest():
    fake = types.SimpleNamespace(
        set_tracking_uri=lambda uri: None,
        set_experiment=lambda name: None,
        start_run=lambda run_name: _Run(),
        set_tag=lambda key, value: None,
        log_param=lambda key, value: None,
        log_metric=lambda key, value: None,
        log_artifacts=lambda path: None,
    )
    old = sys.modules.get("mlflow")
    sys.modules["mlflow"] = fake
    tmp = Path(tempfile.mkdtemp())
    try:
        model_dir = tmp / "model"
        model_dir.mkdir()
        write_json({"model_id": "m1"}, model_dir / "manifest.json")

        result = log_training_result(
            "toy_multiclass_v1",
            "m1",
            {
                "trainer": "dummy",
                "model_dir": str(model_dir),
                "metrics": {"train_rows": 8, "test_rows": 2, "classification_report": {"macro avg": {"f1-score": 0.5}}},
            },
            {"experiment": "toy"},
        )

        assert result["mlflow"]["run_id"] == "run_123"
        assert read_json(model_dir / "manifest.json")["mlflow"]["run_id"] == "run_123"
    finally:
        if old is None:
            sys.modules.pop("mlflow", None)
        else:
            sys.modules["mlflow"] = old
        import shutil
        shutil.rmtree(tmp)
