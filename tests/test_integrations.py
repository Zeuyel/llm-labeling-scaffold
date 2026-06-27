from pathlib import Path
import sys
import tempfile
import types

import pytest

from llm_labeling_scaffold.config import TaskConfig, load_task
from llm_labeling_scaffold.integrations import argilla
from llm_labeling_scaffold.integrations.argilla import (
    _argilla_text_fields,
    _guidelines_for_task,
    _human_label_from_values,
    _prepare_dataset,
    _prepare_records_for_push,
    _questions_for_task,
)
from llm_labeling_scaffold.integrations.mlflow import log_training_result
from llm_labeling_scaffold.io import read_json, write_json, write_jsonl


class _RunInfo:
    run_id = "run_123"
    artifact_uri = "file:///tmp/mlruns/run_123"


class _Run:
    info = _RunInfo()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_mlflow_result_updates_model_manifest():
    fake = types.SimpleNamespace(
        set_tracking_uri=lambda uri: None,
        set_experiment=lambda name: None,
        start_run=lambda run_name: _Run(),
        set_tag=lambda key, value: None,
        log_param=lambda key, value: None,
        log_metric=lambda key, value: None,
        log_artifacts=lambda path: None,
    )
    old = sys.modules.get("mlflow")
    sys.modules["mlflow"] = fake
    tmp = Path(tempfile.mkdtemp())
    try:
        model_dir = tmp / "model"
        model_dir.mkdir()
        write_json({"model_id": "m1"}, model_dir / "manifest.json")

        result = log_training_result(
            "toy_multiclass_v1",
            "m1",
            {
                "trainer": "dummy",
                "model_dir": str(model_dir),
                "metrics": {"train_rows": 8, "test_rows": 2, "classification_report": {"macro avg": {"f1-score": 0.5}}},
            },
            {"experiment": "toy"},
        )

        assert result["mlflow"]["run_id"] == "run_123"
        assert read_json(model_dir / "manifest.json")["mlflow"]["run_id"] == "run_123"
    finally:
        if old is None:
            sys.modules.pop("mlflow", None)
        else:
            sys.modules["mlflow"] = old
        import shutil
        shutil.rmtree(tmp)


class _Question:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_argilla_questions_cover_all_task_label_fields():
    base = load_task(Path("examples/toy_text_classification/task.yaml"))
    task = TaskConfig(
        path=base.path,
        raw={
            **base.raw,
            "labels": {
                "primary": {
                    "name": "innovation_boundary_label",
                    "type": "categorical",
                    "values": ["new_product_or_application", "unclear_or_insufficient"],
                },
                "auxiliary": [
                    {"name": "new_product_application_flag", "type": "integer", "values": [0, 1]},
                    {"name": "process_improvement_only", "type": "integer", "values": [0, 1]},
                    {"name": "service_solution_digital_flag", "type": "integer", "values": [0, 1]},
                    {"name": "service_solution_digital_type", "type": "categorical", "title": "数字化服务类型", "values": ["none", "remote_monitoring"]},
                    {"name": "technical_distance_hint", "type": "categorical", "values": ["same_domain_incremental", "cannot_judge_from_text"]},
                    {"name": "reason", "type": "string"},
                    {"name": "confidence", "type": "integer", "min": 0, "max": 100},
                    {"name": "evidence_product_application", "type": "string", "required": False},
                    {"name": "evidence_process", "type": "string", "required": False},
                    {"name": "evidence_service_solution", "type": "string", "required": False},
                    {"name": "evidence_distance", "type": "string", "required": False},
                ],
            },
        },
    )
    fake_rg = types.SimpleNamespace(LabelQuestion=_Question, TextQuestion=_Question)

    questions = _questions_for_task(fake_rg, task)
    names = [question.kwargs["name"] for question in questions]

    assert names == [
        "innovation_boundary_label",
        "new_product_application_flag",
        "process_improvement_only",
        "service_solution_digital_flag",
        "service_solution_digital_type",
        "technical_distance_hint",
        "reason",
        "confidence",
        "evidence_product_application",
        "evidence_process",
        "evidence_service_solution",
        "evidence_distance",
    ]
    optional = {q.kwargs["name"]: q.kwargs.get("required") for q in questions}
    assert optional["evidence_product_application"] is False
    titles = {q.kwargs["name"]: q.kwargs.get("title") for q in questions}
    assert titles["service_solution_digital_type"] == "数字化服务类型"


def test_argilla_questions_use_value_labels_for_display_text():
    base = load_task(Path("examples/toy_text_classification/task.yaml"))
    task = TaskConfig(
        path=base.path,
        raw={
            **base.raw,
            "labels": {
                "primary": {
                    "name": "label",
                    "type": "categorical",
                    "values": ["yes", "no"],
                    "value_labels": {
                        "yes": {"label": "是", "description": "属于目标类"},
                        "no": {"label": "否", "description": "不属于目标类"},
                    },
                },
            },
        },
    )
    fake_rg = types.SimpleNamespace(LabelQuestion=_Question)

    question = _questions_for_task(fake_rg, task)[0]

    assert question.kwargs["labels"] == {"yes": "是", "no": "否"}


def test_argilla_pull_expands_all_response_fields():
    base = load_task(Path("examples/toy_text_classification/task.yaml"))
    task = TaskConfig(
        path=base.path,
        raw={
            **base.raw,
            "labels": {
                "primary": {
                    "name": "innovation_boundary_label",
                    "type": "categorical",
                    "values": ["new_product_or_application", "unclear_or_insufficient"],
                },
                "auxiliary": [
                    {"name": "new_product_application_flag", "type": "integer", "values": [0, 1]},
                    {"name": "process_improvement_only", "type": "integer", "values": [0, 1]},
                    {"name": "service_solution_digital_flag", "type": "integer", "values": [0, 1]},
                    {"name": "service_solution_digital_type", "type": "categorical", "values": ["none", "remote_monitoring"]},
                    {"name": "technical_distance_hint", "type": "categorical", "values": ["same_domain_incremental", "cannot_judge_from_text"]},
                    {"name": "reason", "type": "string"},
                    {"name": "confidence", "type": "integer", "min": 0, "max": 100},
                    {"name": "evidence_product_application", "type": "string", "required": False},
                ],
            },
        },
    )

    values = {
        "innovation_boundary_label": {"value": "new_product_or_application"},
        "new_product_application_flag": {"value": "1"},
        "process_improvement_only": {"value": "0"},
        "service_solution_digital_flag": {"value": "1"},
        "service_solution_digital_type": {"value": "remote_monitoring"},
        "technical_distance_hint": {"value": "same_domain_incremental"},
        "reason": {"value": "claims describe a new product application"},
        "confidence": {"value": "88"},
        "evidence_product_application": {"value": "new remote monitoring product"},
    }

    human_label = _human_label_from_values(task, values)

    assert human_label["innovation_boundary_label"] == "new_product_or_application"
    assert human_label["new_product_application_flag"] == 1
    assert human_label["process_improvement_only"] == 0
    assert human_label["service_solution_digital_flag"] == 1
    assert human_label["service_solution_digital_type"] == "remote_monitoring"
    assert human_label["technical_distance_hint"] == "same_domain_incremental"
    assert human_label["reason"] == "claims describe a new product application"
    assert human_label["confidence"] == 88
    assert human_label["evidence_product_application"] == "new remote monitoring product"


def test_argilla_guidelines_use_task_annotation_by_default():
    base = load_task(Path("examples/toy_text_classification/task.yaml"))
    task = TaskConfig(
        path=base.path,
        raw={
            **base.raw,
            "annotation": {"guidelines": "请先判断是否属于目标创新，再填写证据。"},
        },
    )

    assert _guidelines_for_task(task, {}) == "请先判断是否属于目标创新，再填写证据。"
    assert _guidelines_for_task(task, {"guidelines": "临时说明"}) == "临时说明"


class _Record:
    def __init__(self, **kwargs):
        self.id = kwargs["id"]
        self.fields = kwargs["fields"]
        self.metadata = kwargs["metadata"]
        self.suggestions = kwargs.get("suggestions", [])


class _Suggestion:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _argilla_push_task() -> TaskConfig:
    return TaskConfig(
        path=Path("task.yaml"),
        raw={
            "task_id": "argilla_push_task",
            "id_field": "record_id",
            "input": {
                "path": "input.jsonl",
                "text_fields": ["title"],
                "metadata_fields": ["source"],
            },
            "labels": {
                "primary": {
                    "name": "label",
                    "type": "categorical",
                    "values": ["yes", "no"],
                },
            },
        },
    )


def test_argilla_push_fails_fast_on_duplicate_record_ids(tmp_path: Path):
    task = _argilla_push_task()
    sample = tmp_path / "sample.jsonl"
    write_jsonl(
        [
            {"record_id": "r1", "title": "one"},
            {"record_id": "r1", "title": "one overlap"},
        ],
        sample,
    )
    fake_rg = types.SimpleNamespace(Record=_Record)

    with pytest.raises(ValueError, match="overlap"):
        _prepare_records_for_push(fake_rg, task, sample, "text", {})


def test_argilla_push_passes_batch_context_metadata(tmp_path: Path):
    task = _argilla_push_task()
    sample = tmp_path / "sample.jsonl"
    write_jsonl([{"record_id": "r1", "title": "one", "source": "seed"}], sample)
    fake_rg = types.SimpleNamespace(Record=_Record)

    records, policy, duplicate_record_ids = _prepare_records_for_push(
        fake_rg,
        task,
        sample,
        "text",
        {
            "dispatch_mode": "batch_plan",
            "batch_plan_id": "plan_1",
            "batch_id": "batch_00001.jsonl",
            "batch_manifest_path": "/tmp/manifest.json",
            "overlap_role": "regular",
        },
    )

    assert policy["strategy"] == "original"
    assert duplicate_record_ids["record_ids"] == []
    assert records[0].id == "r1"
    assert records[0].metadata["record_id"] == "r1"
    assert records[0].metadata["source"] == "seed"
    assert records[0].metadata["dispatch_mode"] == "batch_plan"
    assert records[0].metadata["batch_plan_id"] == "plan_1"
    assert records[0].metadata["batch_id"] == "batch_00001.jsonl"
    assert records[0].metadata["batch_manifest_path"] == "/tmp/manifest.json"
    assert records[0].metadata["overlap_role"] == "regular"
    assert records[0].fields["text"] == "one"
    assert "context" in records[0].fields
    assert "record_id: r1" in records[0].fields["context"]
    assert "source: seed" in records[0].fields["context"]
    assert "batch_id: batch_00001.jsonl" in records[0].fields["context"]
    assert "overlap_role: regular" in records[0].fields["context"]


def test_argilla_visible_context_can_be_configured_or_disabled(tmp_path: Path):
    task = TaskConfig(
        path=Path("task.yaml"),
        raw={
            **_argilla_push_task().raw,
            "annotation": {
                "context_field": "标注上下文",
                "context_title": "标注上下文",
                "context_fields": ["record_id", "source"],
            },
        },
    )
    sample = tmp_path / "sample.jsonl"
    write_jsonl([{"record_id": "r1", "title": "one", "source": "seed", "ignored": "hidden"}], sample)
    fake_rg = types.SimpleNamespace(Record=_Record)

    records, _, _ = _prepare_records_for_push(fake_rg, task, sample, "text", {})

    assert records[0].fields["标注上下文"] == "record_id: r1\nsource: seed"

    disabled_task = TaskConfig(
        path=Path("task.yaml"),
        raw={
            **_argilla_push_task().raw,
            "annotation": {"context_fields": False},
        },
    )
    disabled_records, _, _ = _prepare_records_for_push(fake_rg, disabled_task, sample, "text", {})
    assert disabled_records[0].fields == {"text": "one"}


def test_argilla_settings_include_visible_context_field():
    task = _argilla_push_task()
    fake_rg = types.SimpleNamespace(TextField=_Question)

    fields = _argilla_text_fields(fake_rg, task, "text", {})

    assert [field.kwargs["name"] for field in fields] == ["text", "context"]
    assert fields[1].kwargs["title"] == "Context"


def test_argilla_push_batch_scoped_record_ids_keep_original_metadata_id(tmp_path: Path):
    task = _argilla_push_task()
    sample = tmp_path / "merged_batches.jsonl"
    write_jsonl(
        [
            {"record_id": "r1", "title": "regular", "__lls_batch_id": "batch_00001.jsonl"},
            {"record_id": "r1", "title": "overlap", "__lls_batch_id": "batch_00002.jsonl"},
        ],
        sample,
    )
    fake_rg = types.SimpleNamespace(Record=_Record)

    records, policy, duplicate_record_ids = _prepare_records_for_push(
        fake_rg,
        task,
        sample,
        "text",
        {"record_id_strategy": "batch_scoped", "batch_plan_id": "plan_1"},
    )

    assert policy["strategy"] == "batch_scoped"
    assert policy["batch_id_field"] == "__lls_batch_id"
    assert duplicate_record_ids["original_ids"] == ["r1"]
    assert duplicate_record_ids["record_ids"] == []
    assert [record.id for record in records] == ["r1__batch_00001.jsonl", "r1__batch_00002.jsonl"]
    assert [record.metadata["record_id"] for record in records] == ["r1", "r1"]
    assert [record.metadata["batch_id"] for record in records] == ["batch_00001.jsonl", "batch_00002.jsonl"]
    assert [record.metadata["batch_plan_id"] for record in records] == ["plan_1", "plan_1"]


def test_argilla_push_attaches_suggestions_without_responses(tmp_path: Path):
    task = _argilla_push_task()
    sample = tmp_path / "merged_batches.jsonl"
    write_jsonl(
        [
            {"record_id": "r1", "title": "regular", "__lls_batch_id": "batch_00001.jsonl", "__lls_argilla_record_id": "r1__batch_00001.jsonl"},
            {"record_id": "r1", "title": "overlap", "__lls_batch_id": "batch_00002.jsonl", "__lls_argilla_record_id": "r1__batch_00002.jsonl"},
        ],
        sample,
    )
    suggestions = tmp_path / "suggestions.jsonl"
    write_jsonl(
        [
            {
                "argilla_record_id": "r1__batch_00002.jsonl",
                "suggestions": {"label": "yes"},
                "scores": {"label": 0.82},
                "agent": "codex_exec:v001",
            }
        ],
        suggestions,
    )
    fake_rg = types.SimpleNamespace(Record=_Record, Suggestion=_Suggestion)

    records, _, _ = _prepare_records_for_push(
        fake_rg,
        task,
        sample,
        "text",
        {
            "record_id_strategy": "batch_scoped",
            "suggestions_path": str(suggestions),
        },
    )

    assert records[0].suggestions == []
    assert len(records[1].suggestions) == 1
    assert records[1].suggestions[0].kwargs == {
        "question_name": "label",
        "value": "yes",
        "score": 0.82,
        "agent": "codex_exec:v001",
    }
    assert not hasattr(records[1], "responses")


def test_argilla_push_batch_scoped_fails_on_same_batch_duplicate_original_id(tmp_path: Path):
    task = _argilla_push_task()
    sample = tmp_path / "bad_batch.jsonl"
    write_jsonl(
        [
            {"record_id": "r1", "title": "one", "__lls_batch_id": "batch_00001.jsonl"},
            {"record_id": "r1", "title": "duplicate", "__lls_batch_id": "batch_00001.jsonl"},
        ],
        sample,
    )
    fake_rg = types.SimpleNamespace(Record=_Record)

    with pytest.raises(ValueError, match="同一 batch"):
        _prepare_records_for_push(
            fake_rg,
            task,
            sample,
            "text",
            {"record_id_strategy": "batch_scoped"},
        )


class _Workspace:
    name = "argilla"


class _Dataset:
    def __init__(self, name="dataset_a"):
        self.name = name
        self.workspace = _Workspace()
        self.created = 0
        self.deleted = 0

    def create(self):
        self.created += 1
        return self

    def delete(self):
        self.deleted += 1


class _Datasets:
    def __init__(self, items):
        self._items = items

    def list(self):
        return self._items


class _Client:
    def __init__(self, datasets):
        self.datasets = _Datasets(datasets)


def test_argilla_dataset_existing_policy_fail_append_replace():
    existing = _Dataset("dataset_a")
    created = _Dataset("dataset_a")
    client = _Client([existing])

    try:
        _prepare_dataset(client, created, "dataset_a", "argilla", "fail")
    except ValueError as exc:
        assert "已存在" in str(exc)
    else:
        raise AssertionError("existing dataset should fail by default")

    dataset, action = _prepare_dataset(client, created, "dataset_a", "argilla", "append")
    assert dataset is existing
    assert action == "appended"

    dataset, action = _prepare_dataset(client, created, "dataset_a", "argilla", "replace")
    assert dataset is created
    assert action == "replaced"
    assert existing.deleted == 1
    assert created.created == 1


def test_argilla_connection_status_uses_client_me_and_workspaces(monkeypatch):
    class _FakeClient:
        me = types.SimpleNamespace(username="argilla", role=types.SimpleNamespace(value="owner"))
        workspaces = _Datasets([_Workspace()])

    monkeypatch.setattr(argilla, "_client", lambda api_url=None, api_key=None: _FakeClient())

    status = argilla.test_connection({"workspace": "argilla"})

    assert status["ok"] is True
    assert status["user"]["username"] == "argilla"
    assert status["workspace_exists"] is True
