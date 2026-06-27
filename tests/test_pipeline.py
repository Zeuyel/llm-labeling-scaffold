from pathlib import Path
import hashlib
import time
from unittest.mock import patch

import yaml

from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.io import read_json, write_json
from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.profiles import DEFAULT_PROFILE
from llm_labeling_scaffold.sampling import sample_records


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_status(profile: dict, stage_id: str) -> str:
    return next(stage["status"] for stage in profile["stages"] if stage["id"] == stage_id)


def _create_local_data_lake_task(tmp_path: Path, *, task_id: str = "data_task"):
    source = tmp_path / f"{task_id}_lake_source.jsonl"
    source.write_text(
        '{"record_id":"r1","title":"A"}\n{"record_id":"r2","title":"B"}\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / f"{task_id}_manifest.json"
    write_json(
        {
            "dataset_id": f"{task_id}_lake_seed",
            "layer": "labels",
            "domain": "patent",
            "objects": [
                {
                    "path": "inputs/manual_seed/v1/raw.jsonl",
                    "storage_uri": str(source),
                    "asset_type": "label_import_jsonl",
                    "rows": 2,
                    "id_field": "record_id",
                    "unique_ids": 2,
                    "bytes": source.stat().st_size,
                    "sha256": _file_sha256(source),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/source.jsonl"],
                    "sampling_strategy": "unit_test_seed",
                }
            ],
        },
        manifest_path,
    )
    registry_path = tmp_path / f"{task_id}_data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {"datasets": {f"{task_id}_lake_seed": {"manifest": str(manifest_path)}}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": task_id,
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": f"{task_id}_lake_seed",
                "source_object_path": "inputs/manual_seed/v1/raw.jsonl",
                "default_import_id": "lake_import",
            },
        },
    )
    return load_task(created["path"]), source


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
    assert task["profile"] == DEFAULT_PROFILE
    assert created.profile == DEFAULT_PROFILE
    assert created.raw["profile"] == DEFAULT_PROFILE
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
    assert tasks[0]["profile"] == DEFAULT_PROFILE


def test_task_profile_accepts_preset_mapping(tmp_path: Path):
    task = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "profile_mapping_task",
            "profile": {"preset": DEFAULT_PROFILE},
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )

    loaded = load_task(task["path"])

    assert loaded.profile == DEFAULT_PROFILE
    assert task["profile"] == DEFAULT_PROFILE


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


def test_task_profile_status_tracks_profile_artifacts(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "profile_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    runs_root = tmp_path / "runs"

    empty = pipeline.task_profile_status(runs_root, task)
    assert empty["profile"]["id"] == DEFAULT_PROFILE
    assert [stage["id"] for stage in empty["stages"]] == [
        "lake_import",
        "sample",
        "argilla_dispatch",
        "argilla_pull",
        "agreement_audit",
        "gold_build",
        "train",
        "batch_infer",
    ]
    assert _stage_status(empty, "lake_import") == "ready"
    assert _stage_status(empty, "sample") == "not_started"
    assert next(stage for stage in empty["stages"] if stage["id"] == "lake_import")["action_hint"]
    assert next(stage for stage in empty["stages"] if stage["id"] == "sample")["required_inputs"]

    pipeline.save_import(
        runs_root,
        task,
        "seed_1",
        [{"record_id": "r1", "title": "A"}, {"record_id": "r2", "title": "B"}],
    )
    imported = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(imported, "lake_import") == "done"
    assert _stage_status(imported, "sample") == "ready"
    assert _stage_status(imported, "argilla_dispatch") == "not_started"

    sample_dir = runs_root / task.task_id / "samples" / "sample_a"
    sample_dir.mkdir(parents=True)
    broken_sample = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(broken_sample, "sample") == "blocked"
    assert _stage_status(broken_sample, "argilla_dispatch") == "blocked"

    (sample_dir / "sample.jsonl").write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    write_json({"sample_id": "sample_a", "rows": 1}, sample_dir / "manifest.json")
    sampled = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(sampled, "sample") == "done"
    assert _stage_status(sampled, "argilla_dispatch") == "ready"

    write_json(
        {"annotation_id": "job_a", "source": "argilla"},
        runs_root / task.task_id / "annotation_jobs" / "job_a" / "manifest.json",
    )
    dispatched = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(dispatched, "argilla_dispatch") == "done"
    assert _stage_status(dispatched, "argilla_pull") == "ready"

    decisions_dir = runs_root / task.task_id / "decisions" / "job_a"
    write_json({"decision_id": "job_a", "rows": 1}, decisions_dir / "manifest.json")
    (decisions_dir / "decisions.jsonl").write_text(
        '{"record_id":"r1","human_label":{"label":"yes"}}\n',
        encoding="utf-8",
    )
    pulled = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(pulled, "argilla_pull") == "done"
    assert _stage_status(pulled, "agreement_audit") == "ready"
    assert _stage_status(pulled, "gold_build") == "not_started"

    gold_dir = runs_root / task.task_id / "gold"
    gold_dir.mkdir(parents=True)
    (gold_dir / "gold_v1.jsonl").write_text('{"record_id":"r1","label":"yes"}\n', encoding="utf-8")
    write_json({"version": "v1", "rows": 1}, gold_dir / "gold_v1.manifest.json")
    gold = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(gold, "agreement_audit") == "done"
    assert _stage_status(gold, "gold_build") == "done"
    assert _stage_status(gold, "train") == "ready"

    model_dir = runs_root / task.task_id / "models" / "model_a"
    model_dir.mkdir(parents=True)
    (model_dir / "model.joblib").write_text("placeholder", encoding="utf-8")
    trained = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(trained, "train") == "done"
    assert _stage_status(trained, "batch_infer") == "ready"

    infer_dir = runs_root / task.task_id / "inference" / "batch_a"
    infer_dir.mkdir(parents=True)
    (infer_dir / "predictions.jsonl").write_text('{"record_id":"r1","pred_label":"yes"}\n', encoding="utf-8")
    inferred = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(inferred, "batch_infer") == "done"


def test_import_from_data_lake_manifest_records_lineage(tmp_path: Path):
    source = tmp_path / "lake_source.jsonl"
    source.write_text(
        '{"record_id":"r1","title":"A"}\n{"title":"B","record_id":"r2"}\n',
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    write_json(
        {
            "dataset_id": "lake_seed",
            "layer": "labels",
            "domain": "patent",
            "objects": [
                {
                    "path": "imports/seed/raw.jsonl",
                    "storage_uri": str(source),
                    "asset_type": "label_import_jsonl",
                    "rows": 2,
                    "id_field": "record_id",
                    "unique_ids": 2,
                    "bytes": source.stat().st_size,
                    "sha256": _file_sha256(source),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/source.jsonl"],
                    "sampling_strategy": "unit_test_seed",
                },
                {
                    "path": "imports/seed_alias/raw.jsonl",
                    "storage_uri": str(source),
                    "asset_type": "label_import_jsonl",
                    "rows": 2,
                    "id_field": "record_id",
                    "unique_ids": 2,
                    "bytes": source.stat().st_size,
                    "sha256": _file_sha256(source),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/source_alias.jsonl"],
                    "sampling_strategy": "unit_test_seed_alias",
                }
            ],
        },
        manifest_path,
    )
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "datasets": {
                    "lake_seed": {
                        "layer": "labels",
                        "domain": "patent",
                        "manifest": str(manifest_path),
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "lake_seed",
                "source_object_path": "imports/seed/raw.jsonl",
                "default_import_id": "lake_import",
            },
        },
    )
    task = load_task(created["path"])

    with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
        imported = pipeline.import_from_data_lake(tmp_path / "runs", task)
        reused = pipeline.import_from_data_lake(tmp_path / "runs", task)
    manifest = read_json(tmp_path / "runs" / task.task_id / "imports" / "lake_import" / "manifest.json")
    raw_cache = tmp_path / "runs" / task.task_id / "imports" / "lake_import" / "raw.jsonl"

    assert imported["import_id"] == "lake_import"
    assert reused["action"] == "reused"
    assert _file_sha256(raw_cache) == _file_sha256(source)
    assert manifest["source"] == "data_lake"
    assert manifest["source_dataset_id"] == "lake_seed"
    assert manifest["source_manifest_uri"] == str(manifest_path)
    assert manifest["source_object_uri"] == str(source)
    assert manifest["source_object_path"] == "imports/seed/raw.jsonl"
    assert manifest["source_object_sha256"] == _file_sha256(source)
    assert manifest["source_content_sha256"] == manifest["content_sha256"]
    assert manifest["source_rows"] == 2
    assert manifest["source_asset_type"] == "label_import_jsonl"
    assert manifest["source_created_by"] == "tests"
    assert manifest["source_upstream_uri"] == ["r2:test/upstream/source.jsonl"]
    assert manifest["source_sampling_strategy"] == "unit_test_seed"

    task.raw["data_lake"]["source_object_path"] = "imports/seed_alias/raw.jsonl"
    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            pipeline.import_from_data_lake(tmp_path / "runs", task, import_id="lake_import")
    except ValueError as exc:
        assert "血缘不同" in str(exc)
    else:
        raise AssertionError("same import id with different lake lineage should fail")


def test_start_data_lake_import_job_writes_import_manifest(tmp_path: Path):
    task, source = _create_local_data_lake_task(tmp_path)

    with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
        job = pipeline.start_data_lake_import(tmp_path / "runs", task)
        current = None
        for _ in range(100):
            current = next(
                (item for item in pipeline.jobs_for_task(tmp_path / "runs", task.task_id) if item["id"] == job["id"]),
                None,
            )
            if current and current["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)

    assert current and current["status"] == "succeeded"
    assert current["kind"] == "data_lake_import"
    assert current["result"]["import_id"] == "lake_import"
    manifest_path = tmp_path / "runs" / task.task_id / "imports" / "lake_import" / "manifest.json"
    assert manifest_path.exists()
    manifest = read_json(manifest_path)
    assert manifest["source"] == "data_lake"
    assert manifest["source_object_sha256"] == _file_sha256(source)


def test_data_lake_import_accepts_source_path_relative_to_canonical_uri(tmp_path: Path):
    canonical_root = tmp_path / "canonical"
    source = canonical_root / "manual_seed_500" / "v1" / "raw.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_json(
        {
            "dataset_id": "lake_seed",
            "objects": [
                {
                    "path": "inputs/manual_seed_500/v1/raw.jsonl",
                    "storage_uri": str(source),
                    "asset_type": "label_import_jsonl",
                    "rows": 1,
                    "id_field": "record_id",
                    "unique_ids": 1,
                    "bytes": source.stat().st_size,
                    "sha256": _file_sha256(source),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/source.jsonl"],
                    "sampling_strategy": "unit_test_seed",
                }
            ],
        },
        manifest_path,
    )
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "datasets": {
                    "lake_seed": {
                        "manifest": str(manifest_path),
                        "canonical_uri": str(canonical_root),
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "lake_seed",
                "source_object_path": "manual_seed_500/v1/raw.jsonl",
                "default_import_id": "lake_import",
            },
        },
    )
    task = load_task(created["path"])

    with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
        imported = pipeline.import_from_data_lake(tmp_path / "runs", task)

    assert imported["import_id"] == "lake_import"
    assert imported["data_lake"]["source_object_path"] == "inputs/manual_seed_500/v1/raw.jsonl"


def test_data_lake_import_requires_manifest_relative_object_and_matching_manifest(tmp_path: Path):
    source_a = tmp_path / "a.jsonl"
    source_b = tmp_path / "b.jsonl"
    source_a.write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    source_b.write_text('{"record_id":"r2","title":"B"}\n', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_json(
        {
            "dataset_id": "lake_seed",
            "objects": [
                {
                    "path": "inputs/a/raw.jsonl",
                    "storage_uri": str(source_a),
                    "asset_type": "label_import_jsonl",
                    "rows": 1,
                    "id_field": "record_id",
                    "unique_ids": 1,
                    "bytes": source_a.stat().st_size,
                    "sha256": _file_sha256(source_a),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/a.jsonl"],
                    "sampling_strategy": "unit_test_seed",
                },
                {
                    "path": "inputs/b/raw.jsonl",
                    "storage_uri": str(source_b),
                    "asset_type": "label_import_jsonl",
                    "rows": 1,
                    "id_field": "record_id",
                    "unique_ids": 1,
                    "bytes": source_b.stat().st_size,
                    "sha256": _file_sha256(source_b),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/b.jsonl"],
                    "sampling_strategy": "unit_test_seed",
                },
            ],
        },
        manifest_path,
    )
    other_manifest = tmp_path / "other_manifest.json"
    write_json({"objects": []}, other_manifest)
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump({"datasets": {"lake_seed": {"manifest": str(manifest_path)}}}),
        encoding="utf-8",
    )
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "lake_seed",
                "source_manifest_uri": str(other_manifest),
            },
        },
    )
    task = load_task(created["path"])

    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            pipeline.import_from_data_lake(tmp_path / "runs", task)
    except Exception as exc:
        assert "source_manifest_uri 与 registry 不一致" in str(exc)
    else:
        raise AssertionError("mismatched manifest uri should fail")

    task = load_task(created["path"])
    task.raw["data_lake"].pop("source_manifest_uri")
    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            pipeline.import_from_data_lake(tmp_path / "runs", task)
    except Exception as exc:
        assert "匹配到多个 JSONL 对象" in str(exc)
    else:
        raise AssertionError("multiple jsonl candidates should require source_object_path")

    task.raw["data_lake"]["source_object_path"] = "../raw.jsonl"
    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            pipeline.import_from_data_lake(tmp_path / "runs", task)
    except Exception as exc:
        assert "安全相对路径" in str(exc)
    else:
        raise AssertionError("unsafe source_object_path should fail")


def test_data_lake_import_requires_task_level_manifest_fields(tmp_path: Path):
    source = tmp_path / "raw.jsonl"
    source.write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_json(
        {
            "dataset_id": "lake_seed",
            "objects": [
                {
                    "path": "inputs/manual_seed/v1/raw.jsonl",
                    "storage_uri": str(source),
                    "asset_type": "label_import_jsonl",
                    "rows": 1,
                    "id_field": "record_id",
                    "unique_ids": 1,
                    "bytes": source.stat().st_size,
                    "sha256": _file_sha256(source),
                }
            ],
        },
        manifest_path,
    )
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump({"datasets": {"lake_seed": {"manifest": str(manifest_path)}}}),
        encoding="utf-8",
    )
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "lake_seed",
                "source_object_path": "inputs/manual_seed/v1/raw.jsonl",
            },
        },
    )
    task = load_task(created["path"])

    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            pipeline.import_from_data_lake(tmp_path / "runs", task)
    except Exception as exc:
        assert "created_by" in str(exc)
    else:
        raise AssertionError("task-level manifest governance fields should be required")


def test_data_lake_import_rejects_local_uris_without_test_mode(tmp_path: Path):
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(yaml.safe_dump({"datasets": {}}), encoding="utf-8")
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "lake_seed",
                "source_object_path": "inputs/manual_seed/v1/raw.jsonl",
            },
        },
    )
    task = load_task(created["path"])

    try:
        pipeline.import_from_data_lake(tmp_path / "runs", task)
    except Exception as exc:
        assert "生产模式不允许本地数据湖 URI" in str(exc)
    else:
        raise AssertionError("local data lake uris should require explicit test mode")


def test_data_lake_import_requires_manifest_identity_to_match_registry(tmp_path: Path):
    source = tmp_path / "raw.jsonl"
    source.write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    write_json(
        {
            "dataset_id": "wrong_dataset",
            "layer": "labels",
            "domain": "patent",
            "objects": [
                {
                    "path": "inputs/manual_seed/v1/raw.jsonl",
                    "storage_uri": str(source),
                    "asset_type": "label_import_jsonl",
                    "rows": 1,
                    "id_field": "record_id",
                    "unique_ids": 1,
                    "bytes": source.stat().st_size,
                    "sha256": _file_sha256(source),
                    "created_by": "tests",
                    "upstream_uri": ["r2:test/upstream/source.jsonl"],
                    "sampling_strategy": "unit_test_seed",
                }
            ],
        },
        manifest_path,
    )
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(
        yaml.safe_dump({"datasets": {"lake_seed": {"layer": "labels", "domain": "patent", "manifest": str(manifest_path)}}}),
        encoding="utf-8",
    )
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "data_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "lake_seed",
                "source_object_path": "inputs/manual_seed/v1/raw.jsonl",
            },
        },
    )
    task = load_task(created["path"])

    try:
        with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
            pipeline.import_from_data_lake(tmp_path / "runs", task)
    except Exception as exc:
        assert "manifest.dataset_id 与 registry 不一致" in str(exc)
    else:
        raise AssertionError("manifest dataset_id should match registry dataset id")


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
