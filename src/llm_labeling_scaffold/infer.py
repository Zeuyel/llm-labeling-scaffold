from __future__ import annotations

from pathlib import Path

from .config import TaskConfig, build_text
from .io import read_jsonl, write_json, write_jsonl


def infer_jsonl(task: TaskConfig, model_path: str | Path, corpus_path: str | Path, output_dir: str | Path) -> Path:
    try:
        import joblib
    except ImportError as exc:
        raise RuntimeError("infer_jsonl requires joblib; install the matching trainer/predictor dependencies") from exc

    artifact = joblib.load(model_path)
    pipe = artifact["pipeline"]
    rows = read_jsonl(corpus_path)
    texts = [build_text(row, task) or " " for row in rows]
    pred = pipe.predict(texts)
    probs = pipe.predict_proba(texts) if hasattr(pipe, "predict_proba") else None
    classes = list(pipe.classes_) if hasattr(pipe, "classes_") else list(pipe.named_steps["clf"].classes_)
    out_rows = []
    for idx, row in enumerate(rows):
        item = {task.id_field: str(row[task.id_field]), "pred_label": str(pred[idx])}
        for field in task.metadata_fields:
            if field in row:
                item[field] = row[field]
        if probs is not None:
            for c_idx, klass in enumerate(classes):
                item[f"prob_{klass}"] = float(probs[idx][c_idx])
            item["pred_confidence"] = float(max(probs[idx]))
        out_rows.append(item)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "predictions.jsonl"
    write_jsonl(out_rows, path)
    write_json({"rows": len(out_rows), "model_path": str(model_path), "corpus_path": str(corpus_path)}, out / "inference_summary.json")
    return path
