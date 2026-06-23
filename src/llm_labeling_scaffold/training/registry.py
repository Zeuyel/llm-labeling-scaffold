from __future__ import annotations

from importlib import import_module
from typing import Callable

from ..config import TaskConfig

Trainer = Callable[[TaskConfig, str, str, dict], dict]

_BUILTINS = {
    "tfidf_sgd": "llm_labeling_scaffold.training.tfidf_sgd:train",
}


def available_trainers() -> list[str]:
    return sorted(_BUILTINS)


def load_trainer(name: str) -> Trainer:
    target = _BUILTINS.get(name, name)
    if ":" not in target:
        raise ValueError(f"unknown trainer: {name}")
    module_name, func_name = target.split(":", 1)
    module = import_module(module_name)
    func = getattr(module, func_name)
    return func
