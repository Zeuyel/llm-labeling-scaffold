from __future__ import annotations

from typing import Any

from .config import TaskConfig
from .training import load_trainer


def train_model(
    task: TaskConfig,
    gold_path: str,
    model_id: str,
    trainer: str = "tfidf_sgd",
    params: dict[str, Any] | None = None,
) -> dict:
    trainer_fn = load_trainer(trainer)
    result = trainer_fn(task, str(gold_path), model_id, params or {})
    if not isinstance(result, dict):
        return {"model_id": model_id, "trainer": trainer, "model_path": str(result)}
    result.setdefault("model_id", model_id)
    result.setdefault("trainer", trainer)
    return result
