from pathlib import Path
import hashlib
import time
from unittest.mock import patch

import pytest
import yaml

from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.io import read_json, read_jsonl, write_json, write_jsonl
from llm_labeling_scaffold import pipeline, suggestions as suggestions_module
from llm_labeling_scaffold.profiles import DEFAULT_PROFILE, QUALITY_CONTROL_PROFILE, list_profile_presets, profile_definition
from llm_labeling_scaffold.sampling import sample_records


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_status(profile: dict, stage_id: str) -> str:
    return next(stage["status"] for stage in profile["stages"] if stage["id"] == stage_id)


def _wait_for_job(runs_root: Path, task_id: str, job_id: str, *, attempts: int = 100) -> dict:
    current = None
    for _ in range(attempts):
        current = next((item for item in pipeline.jobs_for_task(runs_root, task_id) if item["id"] == job_id), None)
        if current and current["status"] in {"succeeded", "failed"}:
            return current
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {job_id}")


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


def test_task_discovery_ignores_hidden_backup_directories(tmp_path: Path):
    tasks_root = tmp_path / "tasks"
    created = pipeline.create_task(
        tasks_root,
        {
            "task_id": "shadowed_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "current_label",
            "primary_label_values": ["yes", "no"],
        },
    )
    hidden_backup = tasks_root / ".shadowed_task.old.1" / "task.yaml"
    hidden_backup.parent.mkdir(parents=True)
    hidden_backup.write_text(
        yaml.safe_dump(
            {
                "task_id": "shadowed_task",
                "id_field": "old_record_id",
                "input": {"path": "raw.jsonl", "text_fields": ["old_title"]},
                "labels": {"primary": {"name": "old_label", "values": ["old", "new"]}},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    listed = pipeline.list_tasks(tasks_root)
    loaded = pipeline.load_task_by_id(tasks_root, "shadowed_task")

    assert [task["task_id"] for task in listed] == ["shadowed_task"]
    assert listed[0]["path"] == created["path"]
    assert loaded.path == Path(created["path"])
    assert loaded.id_field == "record_id"
    assert loaded.primary_label["name"] == "current_label"


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


def test_quality_control_profile_preset_is_available(tmp_path: Path):
    profile = profile_definition(QUALITY_CONTROL_PROFILE)

    assert profile["id"] == QUALITY_CONTROL_PROFILE
    assert profile["quality_controls"]["overlap_rate"] == 0.2
    assert profile["quality_controls"]["min_annotators_per_overlap_item"] == 2
    titles = [stage["title"] for stage in profile["stages"]]
    assert "试标/校准" in titles
    assert "一致性检查" in titles
    assert "主标注" in titles
    assert "复核裁决" in titles
    assert "模型训练" in titles
    assert "批量推理" in titles

    task = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "quality_profile_task",
            "profile": {"preset": QUALITY_CONTROL_PROFILE},
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    loaded = load_task(task["path"])
    assert loaded.profile == QUALITY_CONTROL_PROFILE
    assert task["profile"] == QUALITY_CONTROL_PROFILE


def test_profile_preset_catalog_discovers_multiple_cached_presets():
    presets = list_profile_presets()

    assert [preset["id"] for preset in presets][:2] == [DEFAULT_PROFILE, QUALITY_CONTROL_PROFILE]
    assert all("stages" not in preset for preset in presets)
    assert all(preset.get("name") for preset in presets)

    presets[0]["name"] = "mutated"
    assert list_profile_presets()[0]["name"] != "mutated"


def test_task_profile_status_can_switch_profile_without_rewriting_task(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "switch_profile_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    runs_root = tmp_path / "runs"

    default_status = pipeline.task_profile_status(runs_root, task)
    switched_status = pipeline.task_profile_status(runs_root, task, profile_id=QUALITY_CONTROL_PROFILE)

    assert default_status["task_profile_id"] == DEFAULT_PROFILE
    assert default_status["selected_profile_id"] == DEFAULT_PROFILE
    assert switched_status["task_profile_id"] == DEFAULT_PROFILE
    assert switched_status["selected_profile_id"] == QUALITY_CONTROL_PROFILE
    assert [preset["id"] for preset in switched_status["presets"]] == [DEFAULT_PROFILE, QUALITY_CONTROL_PROFILE]
    assert "argilla_dispatch" in [stage["id"] for stage in default_status["stages"]]
    assert "pilot_calibration" in [stage["id"] for stage in switched_status["stages"]]
    assert load_task(created["path"]).profile == DEFAULT_PROFILE


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


def test_task_asset_graph_returns_stage_fallback_and_asset_lineage(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "graph_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    runs_root = tmp_path / "runs"

    empty = pipeline.task_asset_graph(runs_root, task)
    empty_ids = {node["id"] for node in empty["nodes"]}
    assert "task" in empty_ids
    assert "stage:lake_import" in empty_ids
    assert "stage:sample" in empty_ids
    assert any(edge["source"] == "task" and edge["target"] == "stage:lake_import" for edge in empty["edges"])

    pipeline.save_import(
        runs_root,
        task,
        "seed_1",
        [{"record_id": "r1", "title": "A"}, {"record_id": "r2", "title": "B"}],
    )
    sample_dir = runs_root / task.task_id / "samples" / "sample_a"
    sample_dir.mkdir(parents=True)
    (sample_dir / "sample.jsonl").write_text('{"record_id":"r1","title":"A"}\n', encoding="utf-8")
    write_json(
        {"sample_id": "sample_a", "rows": 1, "source_import_id": "seed_1"},
        sample_dir / "manifest.json",
    )
    batch_plan_dir = sample_dir / "batches" / "qc_round_1"
    write_json(
        {
            "plan_id": "qc_round_1",
            "sample_id": "sample_a",
            "batch_count": 1,
            "batches": [{"batch": "batch_00001.jsonl", "rows": 1}],
        },
        batch_plan_dir / "manifest.json",
    )
    write_json(
        {
            "annotation_id": "argilla_qc_round",
            "source": "argilla",
            "sample_id": "sample_a",
            "batch_plan_id": "qc_round_1",
            "argilla_dataset": "argilla_qc_dataset",
        },
        runs_root / task.task_id / "annotation_jobs" / "argilla_qc_round" / "manifest.json",
    )
    write_json(
        {
            "task_id": task.task_id,
            "annotation_id": "argilla_qc_round",
            "suggestion_id": "codex_v001",
            "status": "generated",
            "provider": "codex_exec",
        },
        runs_root / task.task_id / "suggestions" / "argilla_qc_round" / "codex_v001" / "manifest.json",
    )
    decisions_dir = runs_root / task.task_id / "decisions" / "round_1"
    write_json(
        {
            "decision_id": "round_1",
            "sample_id": "sample_a",
            "path": str(decisions_dir / "decisions.jsonl"),
            "rows": 1,
        },
        decisions_dir / "manifest.json",
    )
    (decisions_dir / "decisions.jsonl").write_text(
        '{"record_id":"r1","human_label":{"label":"yes"}}\n',
        encoding="utf-8",
    )

    graph = pipeline.task_asset_graph(runs_root, task)
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["import:seed_1"]["type"] == "import"
    assert nodes["sample:sample_a"]["status"] == "completed"
    assert nodes["suggestions:argilla_qc_round:codex_v001"]["summary"] == "codex_exec"
    assert nodes["decision:round_1"]["route"].endswith("/annotations")
    assert {"source": "import:seed_1", "target": "sample:sample_a", "reason": "抽样来源"} in graph["edges"]
    assert {"source": "batch:sample_a:qc_round_1", "target": "annotation_job:argilla_qc_round", "reason": "批次分发"} in graph["edges"]
    assert {"source": "annotation_job:argilla_qc_round", "target": "suggestions:argilla_qc_round:codex_v001", "reason": "生成机器建议"} in graph["edges"]
    assert {"source": "sample:sample_a", "target": "decision:round_1", "reason": "结果来源样本"} in graph["edges"]


def test_quality_control_profile_status_tracks_batch_and_agreement_artifacts(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "quality_status_task",
            "profile": {"preset": QUALITY_CONTROL_PROFILE},
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])
    runs_root = tmp_path / "runs"
    pipeline.save_import(
        runs_root,
        task,
        "seed_1",
        [{"record_id": "r1", "title": "A"}, {"record_id": "r2", "title": "B"}],
    )

    sample_dir = runs_root / task.task_id / "samples" / "sample_a"
    sample_dir.mkdir(parents=True)
    (sample_dir / "sample.jsonl").write_text(
        '{"record_id":"r1","title":"A"}\n{"record_id":"r2","title":"B"}\n',
        encoding="utf-8",
    )
    write_json({"sample_id": "sample_a", "rows": 2}, sample_dir / "manifest.json")

    sampled = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(sampled, "sample") == "done"
    assert _stage_status(sampled, "pilot_calibration") == "ready"
    assert _stage_status(sampled, "consistency_check") == "not_started"
    assert _stage_status(sampled, "main_annotation") == "not_started"

    batch_dir = sample_dir / "batches" / "pilot_round_1"
    batch_dir.mkdir(parents=True)
    broken_batch = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(broken_batch, "pilot_calibration") == "blocked"
    assert _stage_status(broken_batch, "consistency_check") == "blocked"

    write_json(
        {"plan_id": "pilot_round_1", "sample_id": "sample_a", "batch_count": 1},
        batch_dir / "manifest.json",
    )
    batched = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(batched, "pilot_calibration") == "done"
    assert _stage_status(batched, "consistency_check") == "ready"
    assert _stage_status(batched, "main_annotation") == "not_started"

    write_json(
        {"audit_id": "audit_a", "passed": True},
        runs_root / task.task_id / "agreement_audits" / "audit_a" / "summary.json",
    )
    audited = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(audited, "consistency_check") == "done"
    assert _stage_status(audited, "main_annotation") == "ready"

    write_json(
        {"annotation_id": "job_a", "source": "argilla"},
        runs_root / task.task_id / "annotation_jobs" / "job_a" / "manifest.json",
    )
    annotated = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(annotated, "main_annotation") == "done"
    assert _stage_status(annotated, "argilla_pull") == "ready"

    decisions_dir = runs_root / task.task_id / "decisions" / "job_a"
    write_json({"decision_id": "job_a", "rows": 1}, decisions_dir / "manifest.json")
    (decisions_dir / "decisions.jsonl").write_text(
        '{"record_id":"r1","human_label":{"label":"yes"}}\n',
        encoding="utf-8",
    )
    pulled = pipeline.task_profile_status(runs_root, task)
    assert _stage_status(pulled, "argilla_pull") == "done"
    assert _stage_status(pulled, "review_adjudication") == "done"


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


def test_dry_run_data_lake_import_returns_plan_without_final_import_asset(tmp_path: Path):
    task, source = _create_local_data_lake_task(tmp_path)
    runs_root = tmp_path / "runs"

    with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
        dry_run = pipeline.dry_run_data_lake_import(runs_root, task)

    assert dry_run["dry_run"] is True
    assert dry_run["import_id"] == "lake_import"
    assert dry_run["task"]["task_id"] == task.task_id
    assert dry_run["source"]["source_object_sha256"] == _file_sha256(source)
    assert dry_run["manifest"]["selected_object"]["rows"] == 2
    assert dry_run["validation"]["ok"] is True
    assert dry_run["plan"]["action"] == "create"
    assert not (runs_root / task.task_id / "imports" / "lake_import").exists()

    with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
        pipeline.import_from_data_lake(runs_root, task)
        reused = pipeline.dry_run_data_lake_import(runs_root, task)

    assert reused["plan"]["action"] == "reuse"
    assert reused["plan"]["idempotent"] is True


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


def test_start_data_lake_import_reuses_idempotency_key(tmp_path: Path):
    task, _source = _create_local_data_lake_task(tmp_path)
    runs_root = tmp_path / "runs"

    with patch.dict("os.environ", {"LLS_ALLOW_LOCAL_DATA_LAKE_URIS": "1"}):
        first = pipeline.start_data_lake_import(runs_root, task, idempotency_key="lake-submit-1")
        second = pipeline.start_data_lake_import(runs_root, task, idempotency_key="lake-submit-1")
        current = _wait_for_job(runs_root, task.task_id, first["id"])

    assert second["id"] == first["id"]
    assert second["idempotent_submit"] is True
    assert current["status"] == "succeeded"
    assert current["result"]["import_id"] == "lake_import"

    try:
        pipeline.start_data_lake_import(runs_root, task, import_id="other_import", idempotency_key="lake-submit-1")
    except ValueError as exc:
        assert "幂等 key" in str(exc)
    else:
        raise AssertionError("same idempotency key with different request should fail")


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


def test_task_archive_plan_lists_assets_without_blocking_dependencies(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "archive_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    rows = [{"record_id": "r1", "title": "A"}, {"record_id": "r2", "title": "B"}]
    imported = pipeline.save_import(tmp_path / "runs", task, "seed_1", rows)
    sample_records(task, 1, "sample_a", "head", source_path=imported["path"], source_import_id="seed_1")
    write_json(
        {"task_id": task.task_id, "annotation_id": "job_a", "sample_id": "sample_a"},
        tmp_path / "runs" / task.task_id / "annotation_jobs" / "job_a" / "manifest.json",
    )
    write_json(
        {"task_id": task.task_id, "decision_id": "round_1", "sample_id": "sample_a", "rows": 1},
        tmp_path / "runs" / task.task_id / "decisions" / "round_1" / "manifest.json",
    )

    plan = pipeline.task_archive_plan(tmp_path / "tasks", tmp_path / "runs", task)

    assert plan["can_archive"] is True
    assert not plan["blocked"]
    asset_types = [item["asset_type"] for item in plan["active_assets"]]
    assert "import" in asset_types
    assert "sample" in asset_types
    assert "annotation_job" in asset_types
    assert "decision" in asset_types
    assert [step["asset_type"] for step in plan["archive_order"][:3]] == ["inference", "model", "gold"]
    assert plan["cleanup"]["r2_protected"] is True
    assert plan["cleanup"]["files"]


def test_execute_task_archive_moves_run_assets_then_task_config(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "archive_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    imported = pipeline.save_import(
        tmp_path / "runs",
        task,
        "seed_1",
        [{"record_id": "r1", "title": "A"}, {"record_id": "r2", "title": "B"}],
    )
    sample_path = sample_records(task, 1, "sample_a", "head", source_path=imported["path"], source_import_id="seed_1")
    write_json(
        {"task_id": task.task_id, "annotation_id": "job_a", "sample_id": "sample_a", "sample_path": str(sample_path)},
        tmp_path / "runs" / task.task_id / "annotation_jobs" / "job_a" / "manifest.json",
    )
    write_json(
        {"task_id": task.task_id, "decision_id": "round_1", "sample_id": "sample_a", "path": "decisions.jsonl"},
        tmp_path / "runs" / task.task_id / "decisions" / "round_1" / "manifest.json",
    )
    write_json(
        {"task_id": task.task_id, "audit_id": "audit_1", "passed": True},
        tmp_path / "runs" / task.task_id / "agreement_audits" / "audit_1" / "summary.json",
    )
    write_jsonl(
        [{"record_id": "r1", "title": "A", "label": "yes"}],
        tmp_path / "runs" / task.task_id / "gold" / "gold_v001.jsonl",
    )
    write_json(
        {"task_id": task.task_id, "version": "v001", "path": str(tmp_path / "runs" / task.task_id / "gold" / "gold_v001.jsonl")},
        tmp_path / "runs" / task.task_id / "gold" / "gold_v001.manifest.json",
    )
    write_json(
        {"task_id": task.task_id, "model_id": "model_a", "model_path": str(tmp_path / "runs" / task.task_id / "models" / "model_a" / "model.joblib")},
        tmp_path / "runs" / task.task_id / "models" / "model_a" / "manifest.json",
    )
    (tmp_path / "runs" / task.task_id / "models" / "model_a" / "model.joblib").write_text("model", encoding="utf-8")
    write_jsonl(
        [{"record_id": "r1", "prediction": "yes"}],
        tmp_path / "runs" / task.task_id / "inference" / "batch_a" / "predictions.jsonl",
    )

    result = pipeline.execute_task_archive(tmp_path / "tasks", tmp_path / "runs", task.task_id, reason="done")

    archive_root = Path(result["archive_root"])
    assert (archive_root / "imports" / "seed_1" / "raw.jsonl").exists()
    assert (archive_root / "samples" / "sample_a" / "sample.jsonl").exists()
    assert (archive_root / "annotation_jobs" / "job_a" / "manifest.json").exists()
    assert (archive_root / "decisions" / "round_1" / "manifest.json").exists()
    assert (archive_root / "agreement_audits" / "audit_1" / "summary.json").exists()
    assert (archive_root / "gold" / "gold_v001.jsonl").exists()
    assert (archive_root / "models" / "model_a" / "model.joblib").exists()
    assert (archive_root / "inference" / "batch_a" / "predictions.jsonl").exists()
    assert not (tmp_path / "tasks" / task.task_id).exists()
    assert list((tmp_path / "tasks" / "_archive").glob("archive_task__*/task.yaml"))
    events = read_jsonl(tmp_path / "runs" / task.task_id / "_audit" / "events.jsonl")
    assert any(event["event"] == "task_archive.complete" for event in events)


def test_task_cache_cleanup_deletes_only_local_run_files(tmp_path: Path):
    runs_root = tmp_path / "runs"
    task_dir = runs_root / "cleanup_task"
    write_json({"uri": "r2:bucket/path/object.jsonl"}, task_dir / "imports" / "seed_1" / "manifest.json")
    (task_dir / "imports" / "seed_1" / "raw.jsonl").write_text('{"record_id":"r1"}\n', encoding="utf-8")
    write_json({"event": "keep"}, task_dir / "_audit" / "keep.json")

    result = pipeline.execute_task_cache_cleanup(runs_root, "cleanup_task")

    assert result["ok"] is True
    assert result["r2_deleted"] is False
    assert not (task_dir / "imports" / "seed_1" / "raw.jsonl").exists()
    assert (task_dir / "_audit" / "keep.json").exists()
    events = read_jsonl(task_dir / "_audit" / "events.jsonl")
    assert events[-1]["event"] == "task.cache_cleanup"
    assert events[-1]["details"]["r2_deleted"] is False


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


def test_annotation_job_archive_blocks_decisions_and_prevents_id_reuse(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "annotation_archive_task",
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
    annotation_manifest = {
        "task_id": task.task_id,
        "annotation_id": "job_a",
        "source": "argilla",
        "argilla_dataset": "dataset_a",
        "sample_id": "sample_a",
        "sample_path": str(sample_path),
    }
    write_json(
        annotation_manifest,
        tmp_path / "runs" / task.task_id / "annotation_jobs" / "job_a" / "manifest.json",
    )
    write_json(
        {
            "task_id": task.task_id,
            "decision_id": "decision_a",
            "annotation_id": "job_a",
            "argilla_dataset": "dataset_a",
        },
        tmp_path / "runs" / task.task_id / "decisions" / "decision_a" / "manifest.json",
    )

    with pytest.raises(ValueError, match="下游资产"):
        pipeline.archive_annotation_job(tmp_path / "runs", task.task_id, "job_a")

    (tmp_path / "runs" / task.task_id / "decisions" / "decision_a" / "manifest.json").unlink()
    write_json(
        {
            "task_id": task.task_id,
            "decision_id": "decision_source",
            "annotation_id": "job_other",
            "source_annotation_id": "job_a",
        },
        tmp_path / "runs" / task.task_id / "decisions" / "decision_source" / "manifest.json",
    )

    with pytest.raises(ValueError, match="下游资产"):
        pipeline.archive_annotation_job(tmp_path / "runs", task.task_id, "job_a")

    (tmp_path / "runs" / task.task_id / "decisions" / "decision_source" / "manifest.json").unlink()
    archived = pipeline.archive_annotation_job(tmp_path / "runs", task.task_id, "job_a", reason="done")

    archive_path = Path(archived["archive_path"])
    assert archived["archived"] is True
    assert not (tmp_path / "runs" / task.task_id / "annotation_jobs" / "job_a").exists()
    assert (archive_path / "manifest.json").exists()
    assert read_json(archive_path / "manifest.json")["state"] == "archived"
    events = read_jsonl(tmp_path / "runs" / task.task_id / "_audit" / "events.jsonl")
    assert any(event["event"] == "annotation_job.archive" and event["status"] == "succeeded" for event in events)
    assert any(event["event"] == "annotation_job.archive" and event["status"] == "failed" for event in events)

    job = pipeline.start_action(
        tmp_path / "runs",
        created["path"],
        "argilla_push",
        {
            "sample": str(sample_path),
            "annotation_id": "job_a",
            "dataset": "dataset_a",
        },
    )
    current = _wait_for_job(tmp_path / "runs", task.task_id, job["id"])
    assert current["status"] == "failed"
    assert "已归档" in current["error"]


def test_argilla_push_uses_annotation_job_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "annotation_push_lock_task",
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
    locked_assets: list[str] = []

    class RecordingLock:
        def __init__(self, asset_name: str):
            self.asset_name = asset_name

        def __enter__(self):
            locked_assets.append(self.asset_name)
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_asset_lock(runs_root: Path, task_id: str, asset_name: str) -> RecordingLock:
        return RecordingLock(asset_name)

    def fake_push_sample(task, dispatch_path: str, dataset: str, params: dict) -> dict:
        return {"records": 1}

    from llm_labeling_scaffold.integrations import argilla as argilla_module

    monkeypatch.setattr(pipeline, "_asset_lock", fake_asset_lock)
    monkeypatch.setattr(argilla_module, "push_sample", fake_push_sample)

    job = pipeline.start_action(
        tmp_path / "runs",
        created["path"],
        "argilla_push",
        {
            "sample": str(sample_path),
            "annotation_id": "job_a",
            "dataset": "dataset_a",
        },
    )
    current = _wait_for_job(tmp_path / "runs", task.task_id, job["id"])

    assert current["status"] == "succeeded"
    assert "annotation-job-job_a" in locked_assets


def test_annotation_job_archive_does_not_mark_active_manifest_when_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    task_id = "annotation_archive_failure_task"
    annotation_dir = tmp_path / "runs" / task_id / "annotation_jobs" / "job_a"
    write_json(
        {
            "task_id": task_id,
            "annotation_id": "job_a",
            "source": "argilla",
            "argilla_dataset": "dataset_a",
        },
        annotation_dir / "manifest.json",
    )

    def fail_move(source: Path, target: Path) -> None:
        raise RuntimeError("move failed")

    monkeypatch.setattr(pipeline, "_move_directory", fail_move)

    with pytest.raises(RuntimeError, match="move failed"):
        pipeline.archive_annotation_job(tmp_path / "runs", task_id, "job_a", reason="done")

    manifest = read_json(annotation_dir / "manifest.json")
    assert manifest.get("state") != "archived"
    assert manifest.get("archived_at") is None
    assert annotation_dir.exists()
    events = read_jsonl(tmp_path / "runs" / task_id / "_audit" / "events.jsonl")
    assert any(
        event["event"] == "annotation_job.archive"
        and event["status"] == "failed"
        and event["details"]["error"] == "move failed"
        for event in events
    )


def test_annotation_job_archive_rolls_back_when_archived_manifest_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    task_id = "annotation_archive_manifest_failure_task"
    annotation_dir = tmp_path / "runs" / task_id / "annotation_jobs" / "job_a"
    write_json(
        {
            "task_id": task_id,
            "annotation_id": "job_a",
            "source": "argilla",
            "argilla_dataset": "dataset_a",
        },
        annotation_dir / "manifest.json",
    )

    original_write_json = pipeline.write_json

    def fail_archived_manifest(payload: dict, path: Path) -> None:
        target = Path(path)
        if (
            target.name == "manifest.json"
            and "_archive" in target.parts
            and "annotation_jobs" in target.parts
        ):
            raise RuntimeError("manifest write failed")
        original_write_json(payload, target)

    monkeypatch.setattr(pipeline, "write_json", fail_archived_manifest)

    with pytest.raises(RuntimeError, match="manifest write failed"):
        pipeline.archive_annotation_job(tmp_path / "runs", task_id, "job_a", reason="done")

    manifest = read_json(annotation_dir / "manifest.json")
    assert manifest.get("state") != "archived"
    assert manifest.get("archived_at") is None
    assert annotation_dir.exists()
    archive_root = tmp_path / "runs" / task_id / "_archive" / "annotation_jobs"
    assert not archive_root.exists() or not any(archive_root.iterdir())
    events = read_jsonl(tmp_path / "runs" / task_id / "_audit" / "events.jsonl")
    assert any(
        event["event"] == "annotation_job.archive"
        and event["status"] == "failed"
        and event["details"]["error"] == "manifest write failed"
        for event in events
    )


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


def test_pipeline_batch_run_action_accepts_overlap_plan_params(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "qc_batch_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source = tmp_path / "source.jsonl"
    source.write_text(
        "".join(f'{{"record_id":"r{idx}","title":"Title {idx}"}}\n' for idx in range(1, 7)),
        encoding="utf-8",
    )
    sample_path = sample_records(task, 6, "sample_a", "head", source_path=source)

    result = pipeline.run_action(
        tmp_path / "runs",
        created["path"],
        "batch",
        {
            "sample": str(sample_path),
            "batch_size": 2,
            "overlap_rate": 0.34,
            "min_annotators_per_overlap_item": 2,
            "gold_rate": 0.05,
            "strategy_id": "pipeline_qc_overlap",
            "plan_id": "qc_round_1",
        },
    )

    manifest_path = tmp_path / "runs" / task.task_id / "samples" / "sample_a" / "batches" / "qc_round_1" / "manifest.json"
    assert result["manifest_path"] == str(manifest_path)
    assert result["plan_id"] == "qc_round_1"
    assert result["strategy_id"] == "pipeline_qc_overlap"

    manifest = read_json(manifest_path)
    assert manifest["id_field"] == "record_id"
    assert manifest["overlap_item_count"] == 3
    assert manifest["overlap_assignment_count"] == 3
    assert manifest["gold_rate"] == 0.05
    assert len(result["artifacts"]) == manifest["batch_count"]

    batch_memberships: dict[str, set[str]] = {}
    for artifact in result["artifacts"]:
        for row in read_jsonl(artifact):
            batch_memberships.setdefault(row["record_id"], set()).add(Path(artifact).name)
    assert all(len(batch_memberships[item_id]) == 2 for item_id in manifest["overlap_item_ids"])


def test_list_samples_returns_latest_batch_manifest_after_overlap_batch(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "qc_list_samples_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source = tmp_path / "source.jsonl"
    source.write_text(
        "".join(f'{{"record_id":"r{idx}","title":"Title {idx}"}}\n' for idx in range(1, 7)),
        encoding="utf-8",
    )
    sample_path = sample_records(task, 6, "sample_a", "head", source_path=source)

    pipeline.run_action(
        tmp_path / "runs",
        created["path"],
        "batch",
        {
            "sample": str(sample_path),
            "batch_size": 2,
            "overlap_rate": 0.34,
            "min_annotators_per_overlap_item": 2,
            "strategy_id": "pipeline_qc_overlap",
            "plan_id": "qc_round_1",
        },
    )

    samples = pipeline.list_samples(tmp_path / "runs", task.task_id)
    sample = next(item for item in samples if item["sample_id"] == "sample_a")
    latest = sample["latest_batch_manifest"]
    manifest_path = tmp_path / "runs" / task.task_id / "samples" / "sample_a" / "batches" / "qc_round_1" / "manifest.json"

    assert sample["batch_manifests"] == [latest]
    assert sample["batch_manifest"] == latest
    assert sample["batches"] == sample["batch_manifests"]
    assert sample["batch_count"] == latest["batch_count"]
    assert latest["manifest_path"] == str(manifest_path)
    assert latest["plan_dir"] == str(manifest_path.parent)
    assert latest["plan_id"] == "qc_round_1"
    assert latest["overlap_item_count"] == 3
    assert latest["batch_count"] == 3


def test_argilla_push_batch_plan_dispatch_writes_dispatch_file_and_lineage(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "qc_argilla_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source = tmp_path / "source.jsonl"
    source.write_text(
        "".join(f'{{"record_id":"r{idx}","title":"Title {idx}"}}\n' for idx in range(1, 7)),
        encoding="utf-8",
    )
    sample_path = sample_records(task, 6, "sample_a", "head", source_path=source)
    pipeline.run_action(
        tmp_path / "runs",
        created["path"],
        "batch",
        {
            "sample": str(sample_path),
            "batch_size": 2,
            "overlap_rate": 0.34,
            "min_annotators_per_overlap_item": 2,
            "strategy_id": "pipeline_qc_overlap",
            "plan_id": "qc_round_1",
        },
    )
    batch_manifest_path = tmp_path / "runs" / task.task_id / "samples" / "sample_a" / "batches" / "qc_round_1" / "manifest.json"
    batch_manifest = read_json(batch_manifest_path)
    captured: dict[str, object] = {}

    def fake_push_sample(task_arg, path, dataset, argilla_params):
        rows = read_jsonl(path)
        captured["path"] = str(path)
        captured["dataset"] = dataset
        captured["argilla_params"] = dict(argilla_params)
        captured["rows"] = rows
        return {
            "records": len(rows),
            "record_id_policy": {
                "strategy": argilla_params.get("record_id_strategy"),
                "batch_id_field": "__lls_batch_id",
                "format": "{original_id}__{batch_id}",
            },
            "duplicate_record_ids": {
                "original_ids": ["r1"],
                "record_ids": [],
                "same_batch_original_ids": [],
            },
        }

    with patch("llm_labeling_scaffold.integrations.argilla.push_sample", side_effect=fake_push_sample):
        job = pipeline.start_action(
            tmp_path / "runs",
            created["path"],
            "argilla_push",
            {
                "sample": str(sample_path),
                "dispatch_mode": "batch_plan",
                "batch_plan_id": "qc_round_1",
                "annotation_id": "argilla_qc_round",
                "dataset": "argilla_qc_dataset",
            },
        )
        current = _wait_for_job(tmp_path / "runs", task.task_id, job["id"])

    assert current["status"] == "succeeded"
    dispatch_path = tmp_path / "runs" / task.task_id / "annotation_jobs" / "argilla_qc_round" / "dispatch.jsonl"
    assert captured["path"] == str(dispatch_path)
    assert captured["path"] != str(sample_path)
    assert captured["argilla_params"]["record_id_strategy"] == "batch_scoped"
    dispatch_rows = read_jsonl(dispatch_path)
    original_ids = [row["record_id"] for row in dispatch_rows]
    argilla_record_ids = [row["__lls_argilla_record_id"] for row in dispatch_rows]
    assert len(set(original_ids)) < len(original_ids)
    assert len(set(argilla_record_ids)) == len(argilla_record_ids)
    assert all(row["__lls_batch_plan_id"] == "qc_round_1" for row in dispatch_rows)
    assert all(row["__lls_original_id"] == row["record_id"] for row in dispatch_rows)
    assert {row["__lls_overlap_role"] for row in dispatch_rows} >= {"regular", "overlap"}

    manifest = read_json(tmp_path / "runs" / task.task_id / "annotation_jobs" / "argilla_qc_round" / "manifest.json")
    expected_batch_ids = [entry["batch"] for entry in batch_manifest["batches"]]
    expected_batch_files = [
        str(batch_manifest_path.parent / "batches" / entry["batch"])
        for entry in batch_manifest["batches"]
    ]
    assert manifest["dispatch_mode"] == "batch_plan"
    assert manifest["sample_id"] == "sample_a"
    assert manifest["sample_path"] == str(sample_path)
    assert manifest["batch_plan_id"] == "qc_round_1"
    assert manifest["batch_manifest_path"] == str(batch_manifest_path)
    assert manifest["batch_ids"] == expected_batch_ids
    assert manifest["batch_files"] == expected_batch_files
    assert manifest["overlap_item_ids"] == batch_manifest["overlap_item_ids"]
    assert manifest["argilla_dataset"] == "argilla_qc_dataset"
    assert manifest["rows"] == len(dispatch_rows)
    assert manifest["record_id_policy"]["strategy"] == "batch_scoped"


def test_annotation_decision_gold_status_contract_links_manifests(tmp_path: Path):
    runs_root = tmp_path / "runs"
    task_id = "smoke_status_task"
    annotation_dir = runs_root / task_id / "annotation_jobs" / "argilla_round_1"
    dispatch_path = annotation_dir / "dispatch.jsonl"
    write_jsonl([{"record_id": "r1", "title": "A"}], dispatch_path)
    write_json(
        {
            "task_id": task_id,
            "annotation_id": "argilla_round_1",
            "source": "argilla",
            "argilla_dataset": "dataset_round_1",
            "dispatch_path": str(dispatch_path),
        },
        annotation_dir / "manifest.json",
    )

    dispatched = pipeline.annotation_job_detail(runs_root, task_id, "argilla_round_1")
    assert dispatched["local_dispatch_file"] == str(dispatch_path)
    assert dispatched["local_dispatch_file_exists"] is True
    assert dispatched["argilla_published"] is False
    assert dispatched["decisions_pulled"] is False
    assert dispatched["gold_generated"] is False
    assert dispatched["state"] == "dispatch_ready"

    manifest_path = annotation_dir / "manifest.json"
    manifest = read_json(manifest_path)
    manifest.update({
        "status": "已分发",
        "created_at": "2026-06-28T00:00:00+00:00",
        "result": {"records": 1, "record_id_policy": {"strategy": "original"}},
    })
    write_json(manifest, manifest_path)
    published = pipeline.annotation_job_detail(runs_root, task_id, "argilla_round_1")
    assert published["argilla_published"] is True
    assert published["state"] == "argilla_published"

    decision_dir = runs_root / task_id / "decisions" / "decision_round_1"
    decisions_path = decision_dir / "decisions.jsonl"
    write_json(
        {
            "task_id": task_id,
            "decision_id": "decision_round_1",
            "annotation_id": "argilla_round_1",
            "source_annotation_id": "argilla_round_1",
            "source": "argilla",
            "argilla_dataset": "dataset_round_1",
            "path": str(decisions_path),
            "rows": 0,
        },
        decision_dir / "manifest.json",
    )
    pulled = pipeline.annotation_job_detail(runs_root, task_id, "argilla_round_1")
    assert pulled["decisions_pulled"] is True
    assert pulled["linked_decision_ids"] == ["decision_round_1"]
    assert pulled["state"] == "decisions_pulled"

    gold_dir = runs_root / task_id / "gold"
    gold_path = gold_dir / "gold_v001.jsonl"
    write_json(
        {
            "task_id": task_id,
            "version": "v001",
            "path": str(gold_path),
            "decisions": str(decisions_path),
            "rows": 0,
            "source": "decision_artifact",
        },
        gold_dir / "gold_v001.manifest.json",
    )
    completed = pipeline.annotation_job_detail(runs_root, task_id, "argilla_round_1")
    assert completed["gold_generated"] is True
    assert completed["linked_gold_versions"] == ["v001"]
    assert completed["state"] == "gold_generated"

    decision = pipeline.decision_artifact_detail(runs_root, task_id, "decision_round_1")
    assert decision["argilla_published"] is True
    assert decision["decisions_pulled"] is True
    assert decision["gold_generated"] is True
    assert decision["linked_gold_versions"] == ["v001"]
    assert decision["local_dispatch_file_exists"] is True

    gold = pipeline.gold_version_detail(runs_root, task_id, "v001")
    assert gold["gold_generated"] is True
    assert gold["decisions_pulled"] is True
    assert gold["linked_decision_ids"] == ["decision_round_1"]
    assert gold["linked_gold_versions"] == ["v001"]


def test_decision_and_gold_status_choose_existing_dispatch_from_later_linked_annotation(tmp_path: Path):
    runs_root = tmp_path / "runs"
    task_id = "dispatch_selection_task"
    task_dir = runs_root / task_id
    dataset = "shared_argilla_dataset"

    missing_dir = task_dir / "annotation_jobs" / "round_a_missing"
    write_json(
        {
            "task_id": task_id,
            "annotation_id": "round_a_missing",
            "source": "argilla",
            "argilla_dataset": dataset,
        },
        missing_dir / "manifest.json",
    )

    present_dir = task_dir / "annotation_jobs" / "round_b_present"
    present_dispatch = present_dir / "dispatch.jsonl"
    write_jsonl([{"record_id": "r1", "title": "A"}], present_dispatch)
    write_json(
        {
            "task_id": task_id,
            "annotation_id": "round_b_present",
            "source": "argilla",
            "argilla_dataset": dataset,
            "dispatch_path": str(present_dispatch),
        },
        present_dir / "manifest.json",
    )

    decision_dir = task_dir / "decisions" / "decision_shared"
    decisions_path = decision_dir / "decisions.jsonl"
    write_json(
        {
            "task_id": task_id,
            "decision_id": "decision_shared",
            "source": "argilla",
            "argilla_dataset": dataset,
            "path": str(decisions_path),
            "rows": 0,
        },
        decision_dir / "manifest.json",
    )

    gold_dir = task_dir / "gold"
    write_json(
        {
            "task_id": task_id,
            "version": "v001",
            "path": str(gold_dir / "gold_v001.jsonl"),
            "decisions": str(decisions_path),
            "rows": 0,
            "source": "decision_artifact",
        },
        gold_dir / "gold_v001.manifest.json",
    )

    decision = pipeline.decision_artifact_detail(runs_root, task_id, "decision_shared")
    assert decision["linked_annotation_ids"] == ["round_a_missing", "round_b_present"]
    assert decision["local_dispatch_file"] == str(present_dispatch)
    assert decision["local_dispatch_file_exists"] is True

    gold = pipeline.gold_version_detail(runs_root, task_id, "v001")
    assert gold["linked_annotation_ids"] == ["round_a_missing", "round_b_present"]
    assert gold["local_dispatch_file"] == str(present_dispatch)
    assert gold["local_dispatch_file_exists"] is True


def test_prelabel_suggest_writes_local_suggestions_for_annotation_job(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "suggest_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    annotation_dir = tmp_path / "runs" / task.task_id / "annotation_jobs" / "argilla_round_1"
    dispatch_path = annotation_dir / "dispatch.jsonl"
    write_jsonl(
        [
            {
                "record_id": "r1",
                "title": "remote service platform",
                "__lls_batch_id": "batch_00001.jsonl",
                "__lls_argilla_record_id": "r1__batch_00001.jsonl",
                "__lls_batch_plan_id": "plan_1",
            }
        ],
        dispatch_path,
    )
    write_json(
        {
            "annotation_id": "argilla_round_1",
            "argilla_dataset": "argilla_dataset_1",
            "dispatch_path": str(dispatch_path),
            "dispatch_mode": "batch_plan",
            "batch_plan_id": "plan_1",
            "batch_ids": ["batch_00001.jsonl"],
        },
        annotation_dir / "manifest.json",
    )

    job = pipeline.start_action(
        tmp_path / "runs",
        created["path"],
        "prelabel_suggest",
        {
            "annotation_id": "argilla_round_1",
            "suggestion_id": "local_stub_v001",
            "provider": "local_stub",
            "prompt_version": "v001",
        },
    )
    current = _wait_for_job(tmp_path / "runs", task.task_id, job["id"])

    assert current["status"] == "succeeded"
    out_dir = tmp_path / "runs" / task.task_id / "suggestions" / "argilla_round_1" / "local_stub_v001"
    manifest = read_json(out_dir / "manifest.json")
    rows = read_jsonl(out_dir / "suggestions.jsonl")
    assert manifest["annotation_id"] == "argilla_round_1"
    assert manifest["argilla_dataset"] == "argilla_dataset_1"
    assert manifest["batch_plan_id"] == "plan_1"
    assert manifest["records"] == 1
    assert rows[0]["argilla_record_id"] == "r1__batch_00001.jsonl"
    assert rows[0]["batch_id"] == "batch_00001.jsonl"
    assert rows[0]["batch_plan_id"] == "plan_1"
    assert rows[0]["agent"] == "local_stub:v001"
    assert rows[0]["suggestions"]["label"] in {"yes", "no"}
    suggestions = pipeline.list_suggestions(tmp_path / "runs", task.task_id, annotation_id="argilla_round_1")
    jobs = pipeline.list_annotation_jobs(tmp_path / "runs", task.task_id)
    assert suggestions[0]["suggestion_id"] == "local_stub_v001"
    assert suggestions[0]["records"] == 1
    assert jobs[0]["suggestion_summary"]["records"] == 1
    assert jobs[0]["suggestion_summary"]["latest_suggestion_id"] == "local_stub_v001"
    assert pipeline.list_runs(tmp_path / "runs", task.task_id) == []


def test_prelabel_reuse_persists_publish_metadata(tmp_path: Path, monkeypatch):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "suggest_publish_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    annotation_dir = tmp_path / "runs" / task.task_id / "annotation_jobs" / "argilla_round_1"
    dispatch_path = annotation_dir / "dispatch.jsonl"
    write_jsonl([{"record_id": "r1", "title": "remote service platform"}], dispatch_path)
    write_json(
        {
            "annotation_id": "argilla_round_1",
            "argilla_dataset": "argilla_dataset_1",
            "dispatch_path": str(dispatch_path),
        },
        annotation_dir / "manifest.json",
    )
    suggestions_module.generate_suggestions_for_annotation_job(
        tmp_path / "runs",
        task,
        "argilla_round_1",
        "local_stub_v001",
        provider="local_stub",
        prompt_version="v001",
    )
    monkeypatch.setattr(
        suggestions_module,
        "push_suggestions",
        lambda *args, **kwargs: {"status": "published", "records": 1},
    )

    result = suggestions_module.generate_suggestions_for_annotation_job(
        tmp_path / "runs",
        task,
        "argilla_round_1",
        "local_stub_v001",
        provider="local_stub",
        prompt_version="v001",
        publish=True,
    )

    manifest = read_json(tmp_path / "runs" / task.task_id / "suggestions" / "argilla_round_1" / "local_stub_v001" / "manifest.json")
    assert result["action"] == "reused"
    assert result["publish"]["status"] == "published"
    assert manifest["status"] == "published"
    assert manifest["publish"]["records"] == 1


def test_prelabel_provider_payload_must_be_dict(tmp_path: Path, monkeypatch):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "bad_provider_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = load_task(created["path"])

    class BadProvider:
        def annotate_batch(self, rows, task):
            return ["not", "a", "dict"]

    monkeypatch.setattr(suggestions_module, "get_provider", lambda name: BadProvider())

    with pytest.raises(ValueError, match="必须返回 dict payload"):
        suggestions_module._provider_results(task, [{"record_id": "r1", "title": "A"}], "bad_provider")


def test_argilla_push_sample_dispatch_still_uses_sample_file_without_batch_plan(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "argilla_sample_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source = tmp_path / "source.jsonl"
    source.write_text('{"record_id":"r1","title":"A"}\n{"record_id":"r2","title":"B"}\n', encoding="utf-8")
    sample_path = sample_records(task, 2, "sample_a", "head", source_path=source)
    captured: dict[str, object] = {}

    def fake_push_sample(task_arg, path, dataset, argilla_params):
        captured["path"] = str(path)
        captured["dataset"] = dataset
        captured["argilla_params"] = dict(argilla_params)
        return {"records": len(read_jsonl(path)), "record_id_policy": {"strategy": "original"}}

    with patch("llm_labeling_scaffold.integrations.argilla.push_sample", side_effect=fake_push_sample):
        job = pipeline.start_action(
            tmp_path / "runs",
            created["path"],
            "argilla_push",
            {
                "sample": str(sample_path),
                "annotation_id": "argilla_sample_round",
                "dataset": "argilla_sample_dataset",
            },
        )
        current = _wait_for_job(tmp_path / "runs", task.task_id, job["id"])

    assert current["status"] == "succeeded"
    assert captured["path"] == str(sample_path)
    assert captured["argilla_params"] == {}
    manifest = read_json(tmp_path / "runs" / task.task_id / "annotation_jobs" / "argilla_sample_round" / "manifest.json")
    assert manifest["dispatch_mode"] == "sample"
    assert manifest["sample_path"] == str(sample_path)
    assert manifest["batch_plan_id"] is None
    assert manifest["batch_manifest_path"] is None
    assert manifest["batch_ids"] == []
    assert manifest["rows"] == 2


def test_argilla_push_batch_plan_fails_on_same_batch_duplicate_original_id(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "qc_argilla_duplicate_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    task = pipeline.with_runs_root(load_task(created["path"]), tmp_path / "runs")
    source = tmp_path / "source.jsonl"
    source.write_text(
        "".join(f'{{"record_id":"r{idx}","title":"Title {idx}"}}\n' for idx in range(1, 5)),
        encoding="utf-8",
    )
    sample_path = sample_records(task, 4, "sample_a", "head", source_path=source)
    pipeline.run_action(
        tmp_path / "runs",
        created["path"],
        "batch",
        {"sample": str(sample_path), "batch_size": 2, "plan_id": "round_1"},
    )
    batch_file = tmp_path / "runs" / task.task_id / "samples" / "sample_a" / "batches" / "round_1" / "batches" / "batch_00001.jsonl"
    write_jsonl(
        [
            {"record_id": "r1", "title": "Title 1"},
            {"record_id": "r1", "title": "Duplicate in same batch"},
        ],
        batch_file,
    )

    with patch("llm_labeling_scaffold.integrations.argilla.push_sample") as mock_push:
        job = pipeline.start_action(
            tmp_path / "runs",
            created["path"],
            "argilla_push",
            {
                "sample": str(sample_path),
                "dispatch_mode": "batch_plan",
                "batch_plan_id": "round_1",
                "annotation_id": "argilla_bad_round",
                "dataset": "argilla_bad_dataset",
            },
        )
        current = _wait_for_job(tmp_path / "runs", task.task_id, job["id"])

    assert current["status"] == "failed"
    assert "同一 batch 内重复原始 ID" in current["error"]
    mock_push.assert_not_called()
