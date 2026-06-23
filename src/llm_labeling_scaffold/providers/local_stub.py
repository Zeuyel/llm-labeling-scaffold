from __future__ import annotations

from .base import AnnotationProvider
from ..config import TaskConfig, build_text


class LocalStubProvider(AnnotationProvider):
    """Deterministic provider for smoke tests and CI.

    It uses task-config label values and simple keyword matching. It is not a model.
    """

    def annotate_batch(self, rows: list[dict], task: TaskConfig) -> dict:
        primary = task.primary_label
        values = list(primary.get("values", [])) or ["negative", "positive"]
        negative = values[0]
        positive = values[1] if len(values) > 1 else values[0]
        results = []
        for row in rows:
            text = build_text(row, task).lower()
            hit = any(token in text for token in ("service", "solution", "platform", "remote", "predictive", "服务", "解决方案"))
            label = positive if hit else negative
            result = {
                task.id_field: str(row[task.id_field]),
                primary["name"]: label,
                "confidence": 90 if hit else 70,
                "reason": "local_stub keyword rule",
                "evidence": text[:160],
            }
            for aux in task.auxiliary_labels:
                name = aux["name"]
                if aux.get("type") == "integer" and set(aux.get("values", [])) == {0, 1}:
                    result[name] = 1 if hit else 0
                elif aux.get("type") == "categorical":
                    result[name] = aux.get("values", [""])[0]
            results.append(result)
        return {"results": results}
