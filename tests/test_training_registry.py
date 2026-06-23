import shutil
import tempfile
from pathlib import Path

from llm_labeling_scaffold.config import TaskConfig, load_task
from llm_labeling_scaffold.train import train_model
from llm_labeling_scaffold.training import available_trainers


def dummy_trainer(task, gold_path, model_id, params):
    out = task.runs_dir / "models" / model_id
    out.mkdir(parents=True, exist_ok=True)
    model_path = out / "model.txt"
    model_path.write_text("dummy", encoding="utf-8")
    return {"model_path": str(model_path), "trainer": params["name"], "gold_path": gold_path}


def test_available_trainers_contains_baseline():
    assert "tfidf_sgd" in available_trainers()


def test_train_model_accepts_dotted_trainer():
    tmp = Path(tempfile.mkdtemp())
    try:
        base = load_task(Path("examples/toy_text_classification/task.yaml"))
        task = TaskConfig(path=base.path, raw={**base.raw, "runs_dir": str(tmp)})
        result = train_model(
            task,
            "example_gold.jsonl",
            "dummy_v001",
            "tests.test_training_registry:dummy_trainer",
            {"name": "custom_dummy"},
        )
        assert result["trainer"] == "custom_dummy"
        assert Path(result["model_path"]).exists()
    finally:
        shutil.rmtree(tmp)
