from pathlib import Path
import time

from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.io import read_json, write_json
from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.sampling import sample_records


def test_create_task_writes_custom_task_with_auxiliary_labels(tmp_path: Path):
    task = pipeline.create_task(
        tmp_path,
        {
            "task_id": "patent_boundary_demo",
            "id_field": "patent_id",
            "text_fields": ["patent_title", "patent_abstract"],
            "metadata_fields": "firm_name, application_year",
            "primary_label_name": "innovation_boundary_label",
            "primary_label_values": ["new_product_or_application", "unclear_or_insufficient"],
            "annotation_guidelines": "请阅读专利标题、摘要和权利要求节选后完成标注。",
            "auxiliary_labels": [
                {"name": "new_product_application_flag", "type": "integer", "values": ["0", "1"]},
                {"name": "reason", "type": "string"},
                {"name": "confidence", "type": "integer", "min": "0", "max": "100"},
                {"name": "evidence_product_application", "type": "string", "required": False},
            ],
        },
    )

    created = load_task(task["path"])

    assert created.task_id == "patent_boundary_demo"
    assert created.id_field == "patent_id"
    assert created.text_fields == ["patent_title", "patent_abstract"]
    assert created.metadata_fields == ["firm_name", "application_year"]
    assert created.primary_label["name"] == "innovation_boundary_label"
    assert [item["name"] for item in created.auxiliary_labels] == [
        "new_product_application_flag",
        "reason",
        "confidence",
        "evidence_product_application",
    ]
    assert created.auxiliary_labels[0]["values"] == [0, 1]
    assert created.auxiliary_labels[2]["min"] == 0
    assert created.auxiliary_labels[3]["required"] is False
    assert created.annotation_guidelines == "请阅读专利标题、摘要和权利要求节选后完成标注。"


def test_list_tasks_reads_multiple_roots_and_deduplicates(tmp_path: Path):
    root_a = tmp_path / "examples"
    root_b = tmp_path / "tasks"
    pipeline.create_task(
        root_a,
        {
            "task_id": "task_a",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    pipeline.create_task(
        root_b,
        {
            "task_id": "task_a",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )

    tasks = pipeline.list_tasks(f"{root_a},{root_b}")

    assert [task["task_id"] for task in tasks] == ["task_a"]


def test_delete_task_archives_writable_task_and_keeps_examples_readonly(tmp_path: Path):
    tasks_root = tmp_path / "tasks"
    examples_root = tmp_path / "examples"
    pipeline.create_task(
        tasks_root,
        {
            "task_id": "delete_me",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    pipeline.create_task(
        examples_root,
        {
            "task_id": "demo_task",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )

    removed = pipeline.delete_task(tasks_root, "delete_me", runs_root=tmp_path / "runs")

    assert removed["archived"] is True
    assert not (tasks_root / "delete_me").exists()
    assert (tasks_root / "_archive").exists()
    try:
        pipeline.delete_task(examples_root, "demo_task")
    except ValueError as exc:
        assert "示例任务不可归档" in str(exc)
    else:
        raise AssertionError("example task should not be deletable")


def test_import_asset_is_idempotent_and_blocks_conflicting_overwrite(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    rows = [
        {"record_id": "r1", "title": "A"},
        {"record_id": "r2", "title": "B"},
    ]

    first = pipeline.save_import(tmp_path / "runs", task, "seed_1", rows)
    second = pipeline.save_import(tmp_path / "runs", task, "seed_1", rows)

    assert first["action"] == "created"
    assert second["action"] == "reused"
    assert second["idempotent"] is True
    assert second["content_sha256"] == first["content_sha256"]
    try:
        pipeline.save_import(tmp_path / "runs", task, "seed_1", [{"record_id": "r3", "title": "C"}])
    except ValueError as exc:
        assert "内容不同" in str(exc)
    else:
        raise AssertionError("conflicting import should fail")
    audit = tmp_path / "runs" / task.task_id / "_audit" / "events.jsonl"
    assert audit.exists()


def test_import_rows_and_archive_respect_sample_dependencies(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    rows = [
        {"record_id": "r1", "title": "Alpha"},
        {"record_id": "r2", "title": "Beta"},
    ]
    imported = pipeline.save_import(tmp_path / "runs", task, "seed_1", rows)

    page = pipeline.import_rows(tmp_path / "runs", task.task_id, "seed_1", query="Beta")
    assert page["total"] == 1
    assert page["rows"][0]["record_id"] == "r2"

    task = pipeline.with_runs_root(task, tmp_path / "runs")
    sample_records(task, 1, "sample_a", "head", source_path=imported["path"], source_import_id=imported["import_id"])
    detail = pipeline.import_detail(tmp_path / "runs", task.task_id, "seed_1", id_field=task.id_field)
    assert detail["linked_samples"][0]["sample_id"] == "sample_a"
    try:
        pipeline.archive_import(tmp_path / "runs", task.task_id, "seed_1")
    except ValueError as exc:
        assert "已被样本使用" in str(exc)
    else:
        raise AssertionError("linked import should not be archived")


def test_import_dependency_can_be_inferred_for_legacy_full_sample(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    rows = [
        {"record_id": "r1", "title": "Alpha"},
        {"record_id": "r2", "title": "Beta"},
    ]
    imported = pipeline.save_import(tmp_path / "runs", task, "seed_1", rows)
    task = pipeline.with_runs_root(task, tmp_path / "runs")
    legacy_source = tmp_path / "legacy_source.jsonl"
    legacy_source.write_text(
        '{"record_id":"r1","title":"Alpha"}\n{"record_id":"r2","title":"Beta"}\n',
        encoding="utf-8",
    )
    sample_records(task, 2, "legacy_sample", "head", source_path=legacy_source)

    detail = pipeline.import_detail(tmp_path / "runs", task.task_id, imported["import_id"], id_field=task.id_field)

    assert detail["linked_samples"][0]["sample_id"] == "legacy_sample"
    assert detail["linked_samples"][0]["link_reason"] == "内容哈希一致"


def test_archived_import_id_cannot_be_reused(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    rows = [{"record_id": "r1", "title": "A"}]

    pipeline.save_import(tmp_path / "runs", task, "seed_1", rows)
    archived = pipeline.archive_import(tmp_path / "runs", task.task_id, "seed_1")

    assert archived["archived"] is True
    try:
        pipeline.save_import(tmp_path / "runs", task, "seed_1", rows)
    except ValueError as exc:
        assert "已归档" in str(exc)
    else:
        raise AssertionError("archived import id should not be reused")


def test_import_rejects_missing_required_fields_and_duplicate_ids(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])

    for bad_rows, expected in [
        ([{"title": "A"}], "缺少任务必需字段"),
        ([{"record_id": "r1", "title": "A"}, {"record_id": "r1", "title": "B"}], "重复 ID"),
        ([{"record_id": "r1", "title": ""}], "文本字段全为空"),
    ]:
        try:
            pipeline.save_import(tmp_path / "runs", task, "bad_seed", bad_rows)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("bad import should fail")


def test_sample_asset_is_idempotent_and_blocks_conflicting_overwrite(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source_a = tmp_path / "source_a.jsonl"
    source_b = tmp_path / "source_b.jsonl"
    source_a.write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    source_b.write_text('{"record_id":"r2","title":"B"}\n', encoding="utf-8")

    first = sample_records(task, 1, "sample_a", "head", source_path=source_a)
    second = sample_records(task, 1, "sample_a", "head", source_path=source_a)

    assert first == second
    try:
        sample_records(task, 1, "sample_a", "head", source_path=source_b)
    except ValueError as exc:
        assert "内容不同" in str(exc)
    else:
        raise AssertionError("conflicting sample should fail")


def test_task_archive_blocks_existing_run_assets(tmp_path: Path):
    pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    (tmp_path / "runs" / "data_task" / "imports").mkdir(parents=True)

    try:
        pipeline.delete_task(tmp_path / "tasks", "data_task", runs_root=tmp_path / "runs")
    except ValueError as exc:
        assert "已有数据资产" in str(exc)
    else:
        raise AssertionError("task with run assets should not be archived")


def test_sample_archive_blocks_dependencies_and_prevents_id_reuse(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source = tmp_path / "source.jsonl"
    source.write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    sample_path = sample_records(task, 1, "sample_a", "head", source_path=source)

    write_json(
        {
            "task_id": task.task_id,
            "annotation_id": "job_a",
            "sample_id": "sample_a",
            "sample_path": str(sample_path),
        },
        tmp_path / "runs" / task.task_id / "annotation_jobs" / "job_a" / "manifest.json",
    )
    try:
        pipeline.archive_sample(tmp_path / "runs", task.task_id, "sample_a")
    except ValueError as exc:
        assert "下游资产" in str(exc)
    else:
        raise AssertionError("sample with annotation dependency should not be archived")

    (tmp_path / "runs" / task.task_id / "annotation_jobs" / "job_a" / "manifest.json").unlink()
    archived = pipeline.archive_sample(tmp_path / "runs", task.task_id, "sample_a")
    assert archived["archived"] is True
    assert not (tmp_path / "runs" / task.task_id / "samples" / "sample_a").exists()
    try:
        sample_records(task, 1, "sample_a", "head", source_path=source)
    except ValueError as exc:
        assert "已归档" in str(exc)
    else:
        raise AssertionError("archived sample id should not be reused")


def test_batch_action_does_not_overwrite_sample_manifest(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source = tmp_path / "source.jsonl"
    source.write_text(
        '{"record_id":"r1","title":"A"}\n{"record_id":"r2","title":"B"}\n',
        encoding="utf-8",
    )
    sample_path = sample_records(task, 2, "sample_a", "head", source_path=source)
    manifest_path = tmp_path / "runs" / task.task_id / "samples" / "sample_a" / "manifest.json"
    before = read_json(manifest_path)

    job = pipeline.start_action(
        tmp_path / "runs",
        created["path"],
        "batch",
        {"sample": str(sample_path), "batch_size": 1},
    )
    current = None
    for _ in range(50):
        current = next((item for item in pipeline.jobs_for_task(tmp_path / "runs", task.task_id) if item["id"] == job["id"]), None)
        if current and current["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)
    assert current and current["status"] == "succeeded"

    after = read_json(manifest_path)
    assert after == before
    assert (tmp_path / "runs" / task.task_id / "samples" / "sample_a" / "batches" / "size_1" / "manifest.json").exists()
