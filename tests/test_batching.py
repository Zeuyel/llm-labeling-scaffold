from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from llm_labeling_scaffold.batching import batch_records
from llm_labeling_scaffold.io import read_json, read_jsonl, write_jsonl


def _rows(count: int) -> list[dict]:
    return [{"record_id": f"r{idx}", "title": f"Title {idx}"} for idx in range(1, count + 1)]


def test_batch_records_keeps_legacy_split_contract(tmp_path: Path):
    sample = tmp_path / "sample.jsonl"
    rows = _rows(5)
    write_jsonl(rows, sample)

    paths = batch_records(sample, tmp_path / "out", 2)

    assert [path.name for path in paths] == ["batch_00001.jsonl", "batch_00002.jsonl", "batch_00003.jsonl"]
    assert read_jsonl(paths[0]) == rows[:2]
    assert read_jsonl(paths[1]) == rows[2:4]
    assert read_jsonl(paths[2]) == rows[4:]

    manifest = read_json(tmp_path / "out" / "manifest.json")
    assert manifest["sample"] == str(sample)
    assert manifest["batch_size"] == 2
    assert manifest["batch_count"] == 3
    assert manifest["regular_assignment_count"] == 5
    assert manifest["overlap_item_count"] == 0
    assert [batch["rows"] for batch in manifest["batches"]] == [2, 2, 1]
    assert [batch["regular_rows"] for batch in manifest["batches"]] == [2, 2, 1]
    assert [batch["overlap_rows"] for batch in manifest["batches"]] == [0, 0, 0]


def test_batch_records_writes_overlap_plan_manifest_and_shared_rows(tmp_path: Path):
    sample = tmp_path / "sample.jsonl"
    rows = _rows(6)
    write_jsonl(rows, sample)

    paths = batch_records(
        sample,
        tmp_path / "qc_out",
        2,
        overlap_rate=0.34,
        min_annotators_per_overlap_item=2,
        gold_rate=0.1,
        strategy_id="qc_overlap_test",
        plan_id="qc_plan_test",
        id_field="record_id",
    )

    manifest = read_json(tmp_path / "qc_out" / "manifest.json")
    assert manifest["plan_id"] == "qc_plan_test"
    assert manifest["strategy_id"] == "qc_overlap_test"
    assert manifest["total_sample_rows"] == 6
    assert manifest["regular_assignment_count"] == 6
    assert manifest["overlap_item_count"] == 3
    assert manifest["overlap_assignment_count"] == 3
    assert manifest["overlap_rate"] == 0.34
    assert manifest["min_annotators_per_overlap_item"] == 2
    assert manifest["gold_rate"] == 0.1
    assert manifest["gold_item_count"] == 0
    assert len(manifest["overlap_item_ids"]) == 3
    assert sum(batch["overlap_rows"] for batch in manifest["batches"]) == 3

    batches_by_id: dict[str, set[str]] = defaultdict(set)
    key_sets = []
    for path in paths:
        for row in read_jsonl(path):
            key_sets.append(set(row))
            batches_by_id[row["record_id"]].add(path.name)

    assert all(keys == {"record_id", "title"} for keys in key_sets)
    assert all(len(batches_by_id[item_id]) == 2 for item_id in manifest["overlap_item_ids"])
    assert any(len(batch_names) > 1 for batch_names in batches_by_id.values())

    appearance_counts = Counter()
    for path in paths:
        for row in read_jsonl(path):
            appearance_counts[row["record_id"]] += 1
    assert sum(count - 1 for count in appearance_counts.values() if count > 1) == 3
