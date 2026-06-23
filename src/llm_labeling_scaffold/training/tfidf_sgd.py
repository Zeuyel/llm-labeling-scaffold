from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import TaskConfig, build_text
from ..io import read_jsonl, write_json


def train(task: TaskConfig, gold_path: str, model_id: str, params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    try:
        import joblib
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import SGDClassifier
        from sklearn.metrics import classification_report, confusion_matrix
        from sklearn.model_selection import train_test_split
        from sklearn.pipeline import Pipeline
    except ImportError as exc:
        raise RuntimeError(
            "trainer 'tfidf_sgd' requires optional dependencies: scikit-learn and joblib"
        ) from exc

    seed = int(params.get("seed", 20260617))
    test_size = float(params.get("test_size", 0.25))
    ngram_min = int(params.get("ngram_min", 2))
    ngram_max = int(params.get("ngram_max", 4))
    max_features = int(params.get("max_features", 180000))

    rows = read_jsonl(gold_path)
    label = task.primary_label["name"]
    texts = [build_text(row, task) or " " for row in rows]
    y = [str(row[label]) for row in rows]
    stratify = y if len(set(y)) > 1 and min(y.count(v) for v in set(y)) >= 2 else None
    x_train, x_test, y_train, y_test = train_test_split(
        texts, y, test_size=test_size, random_state=seed, stratify=stratify
    )
    pipe = Pipeline(
        [
            ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(ngram_min, ngram_max), max_features=max_features)),
            ("clf", SGDClassifier(loss="log_loss", class_weight="balanced", random_state=seed)),
        ]
    )
    pipe.fit(x_train, y_train)
    pred = pipe.predict(x_test)

    out = task.runs_dir / "models" / model_id
    out.mkdir(parents=True, exist_ok=True)
    model_path = out / "model.joblib"
    joblib.dump({"pipeline": pipe, "task": task.raw, "model_id": model_id, "trainer": "tfidf_sgd"}, model_path)

    labels = sorted(set(y))
    metrics = {
        "model_id": model_id,
        "trainer": "tfidf_sgd",
        "gold_path": str(gold_path),
        "train_rows": len(x_train),
        "test_rows": len(x_test),
        "labels": labels,
        "params": {
            "seed": seed,
            "test_size": test_size,
            "ngram_min": ngram_min,
            "ngram_max": ngram_max,
            "max_features": max_features,
        },
        "classification_report": classification_report(y_test, pred, output_dict=True, zero_division=0),
        "confusion_matrix": confusion_matrix(y_test, pred, labels=labels).tolist(),
    }
    write_json(metrics, out / "metrics.json")
    write_json(
        {
            "model_id": model_id,
            "trainer": "tfidf_sgd",
            "model_path": str(model_path),
            "gold_path": str(gold_path),
            "metrics_path": str(out / "metrics.json"),
        },
        out / "manifest.json",
    )
    (out / "summary.md").write_text(
        "# Model Card\n\n"
        f"- model_id: `{model_id}`\n"
        "- trainer: `tfidf_sgd`\n"
        "- purpose: `baseline text classifier`\n"
        f"- train_rows: `{len(x_train)}`\n"
        f"- test_rows: `{len(x_test)}`\n",
        encoding="utf-8",
    )
    return {"model_path": str(model_path), "model_dir": str(out), "trainer": "tfidf_sgd", "metrics": metrics}
