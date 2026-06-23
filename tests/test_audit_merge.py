from pathlib import Path

from llm_labeling_scaffold.annotation import annotate
from llm_labeling_scaffold.audit import audit_run
from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.merge import merge_run
from llm_labeling_scaffold.sampling import sample_records


def test_local_stub_audit_merge():
    task = load_task(Path("examples/toy_text_classification/task.yaml"))
    sample = sample_records(task, rows=6, sample_id="pytest", strategy="head")
    run = annotate(task, sample, "pytest_demo", "local_stub", 3)
    summary = audit_run(task, run)
    assert summary["usable_batches"] == 2
    merged = merge_run(task, run)
    assert merged["merged_rows"] == 6
