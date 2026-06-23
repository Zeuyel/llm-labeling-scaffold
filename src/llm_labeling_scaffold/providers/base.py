from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import TaskConfig


class AnnotationProvider(ABC):
    @abstractmethod
    def annotate_batch(self, rows: list[dict], task: TaskConfig) -> dict:
        raise NotImplementedError
