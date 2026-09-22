from pathlib import Path

import pytest

from llm_labeling_scaffold.annotation import annotate
from llm_labeling_scaffold.audit import audit_run
from llm_labeling_scaffold.batching import batch_records
from llm_labeling_scaffold.gold import build_gold, validate_gold_artifact
from llm_labeling_scaffold.io import read_json, read_jsonl, write_json, write_jsonl
from llm_labeling_scaffold.merge import merge_run
from llm_labeling_scaffold.providers.local_stub import LocalStubProvider
from llm_labeling_scaffold.sampling import sample_records


def test_local_stub_audit_merge(toy_task):
    task = toy_task
    sample = sample_records(task, rows=6, sample_id="pytest", strategy="head")
    run = annotate(task, sample, "pytest_demo", "local_stub", 3)
    summary = audit_run(task, run)
    assert summary["usable_batches"] == 2
    merged = merge_run(task, run)
    assert merged["merged_rows"] == 6


@pytest.mark.parametrize("mode", ["missing", "tampered"])
def test_audit_blocks_missing_or_tampered_batch(toy_task, mode: str):
    sample = sample_records(toy_task, rows=4, sample_id=f"integrity_{mode}", strategy="head")
    run = annotate(toy_task, sample, f"integrity_{mode}", "local_stub", 2)
    batch = run / "input" / "batches" / "batch_00001.jsonl"
    if mode == "missing":
        batch.unlink()
    else:
        write_jsonl(read_jsonl(batch)[:1], batch)

    summary = audit_run(toy_task, run)

    assert not summary["input_evidence"]["valid"]
    assert not summary["is_usable"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("batch_count", 99), ("sample_rows", 99)],
)
def test_audit_blocks_tampered_manifest_counts(toy_task, field: str, value: int):
    sample = sample_records(toy_task, rows=4, sample_id=f"manifest_{field}", strategy="head")
    run = annotate(toy_task, sample, f"manifest_{field}", "local_stub", 2)
    manifest_path = run / "input" / "manifest.json"
    manifest = read_json(manifest_path)
    manifest[field] = value
    write_json(manifest, manifest_path)

    summary = audit_run(toy_task, run)

    assert not summary["input_evidence"]["valid"]
    assert not summary["is_usable"]


def test_merge_blocks_cross_batch_id_content_conflict(toy_task, tmp_path: Path):
    sample = tmp_path / "sample.jsonl"
    write_jsonl(
        [
            {"record_id": "r001", "title": "General notice", "body": "Annual update", "year": 2025, "firm_id": "f001"},
            {"record_id": "r002", "title": "Service", "body": "Remote platform", "year": 2025, "firm_id": "f002"},
        ],
        sample,
    )
    run = toy_task.runs_dir / "cross_batch"
    batches = batch_records(sample, run / "input", 1, overlap_rate=0.5, min_annotators_per_overlap_item=2, id_field=toy_task.id_field)
    provider = LocalStubProvider()
    duplicate_ids: dict[str, int] = {}
    for batch in batches:
        rows = read_jsonl(batch)
        for row in rows:
            duplicate_ids[row[toy_task.id_field]] = duplicate_ids.get(row[toy_task.id_field], 0) + 1
        write_json(provider.annotate_batch(rows, toy_task), run / "llm" / f"{batch.stem}.json")
    duplicate_id = next(record_id for record_id, count in duplicate_ids.items() if count > 1)
    changed = False
    for batch in batches:
        output = run / "llm" / f"{batch.stem}.json"
        payload = read_json(output)
        for row in payload["results"]:
            if row[toy_task.id_field] == duplicate_id and changed:
                row["reason"] = "different but schema-valid evidence"
                write_json(payload, output)
                break
            if row[toy_task.id_field] == duplicate_id:
                changed = True

    audit = audit_run(toy_task, run)
    merged = merge_run(toy_task, run)

    assert audit["is_usable"]
    assert merged["cross_batch_conflict_count"] == 1
    assert not merged["is_usable"]
    assert list(read_jsonl(run / "merged" / "conflict_pool.jsonl"))[0]["kind"] == "cross_batch_content_conflict"
    with pytest.raises(ValueError, match="合并结果未通过"):
        build_gold(toy_task, run, "v001")


def test_gold_rejects_reuse_after_content_replacement(toy_task):
    sample = sample_records(toy_task, rows=4, sample_id="gold_integrity", strategy="head")
    run = annotate(toy_task, sample, "gold_integrity", "local_stub", 2)
    audit_run(toy_task, run)
    merge_run(toy_task, run)
    gold_path = build_gold(toy_task, run, "v001")
    assert validate_gold_artifact(toy_task, "v001")["data_sha256"]

    rows = read_jsonl(gold_path)
    rows[0]["reason"] = "replaced"
    write_jsonl(rows, gold_path)

    with pytest.raises(ValueError, match="内容哈希"):
        validate_gold_artifact(toy_task, "v001")
    with pytest.raises(ValueError, match="内容哈希"):
        build_gold(toy_task, run, "v001")
