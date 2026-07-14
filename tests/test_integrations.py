from pathlib import Path
import io
import sys
import tempfile
import types
from uuid import UUID

import pytest

from llm_labeling_scaffold.config import TaskConfig, load_task
from llm_labeling_scaffold.integrations import argilla
from llm_labeling_scaffold.integrations.argilla import (
    _ARGILLA_PUSH_FINGERPRINT_FIELD,
    _argilla_text_fields,
    _build_contract,
    _guidelines_for_task,
    _human_label_from_values,
    _make_suggestion,
    _prepare_dataset,
    _prepare_records_for_push,
    _push_fingerprints,
    _questions_for_task,
    _record_response_groups,
    _settings_fingerprint,
    _server_version,
    _task_fingerprint,
)
from llm_labeling_scaffold.integrations.mlflow import log_training_result
from llm_labeling_scaffold.io import read_json, read_jsonl, write_json, write_jsonl


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


def test_argilla_2_8_public_response_iterable_groups_by_user_and_status():
    rg = pytest.importorskip("argilla")
    user_id = UUID("10000000-0000-0000-0000-000000000001")
    record = rg.Record(
        id="r1",
        fields={"text": "one"},
        responses=[
            rg.Response(question_name="label", value="yes", user_id=user_id, status=rg.ResponseStatus.submitted),
            rg.Response(question_name="reason", value="evidence", user_id=user_id, status=rg.ResponseStatus.submitted),
        ],
    )

    groups = _record_response_groups(record)

    assert groups == [
        {
            "user_id": str(user_id),
            "values": {"label": {"value": "yes"}, "reason": {"value": "evidence"}},
            "status": "submitted",
            "observed_statuses": ["submitted"],
        }
    ]
    assert "status" not in record.responses.to_dict()["label"][0]


def test_argilla_response_groups_quarantine_mixed_and_keep_draft_discarded():
    user_mixed = UUID("10000000-0000-0000-0000-000000000001")
    user_draft = UUID("10000000-0000-0000-0000-000000000002")
    user_discarded = UUID("10000000-0000-0000-0000-000000000003")
    record = types.SimpleNamespace(
        responses=[
            types.SimpleNamespace(question_name="label", value="yes", user_id=user_mixed, status="submitted"),
            types.SimpleNamespace(question_name="reason", value="mixed", user_id=user_mixed, status="draft"),
            types.SimpleNamespace(question_name="label", value="no", user_id=user_draft, status="draft"),
            types.SimpleNamespace(question_name="label", value="no", user_id=user_discarded, status="discarded"),
        ]
    )

    groups = {group["user_id"]: group for group in _record_response_groups(record)}

    assert groups[str(user_mixed)]["status"] == "mixed"
    assert groups[str(user_mixed)]["observed_statuses"] == ["draft", "submitted"]
    assert groups[str(user_draft)]["status"] == "draft"
    assert groups[str(user_discarded)]["status"] == "discarded"


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


def test_argilla_push_ignores_non_numeric_suggestion_scores(tmp_path: Path):
    task = _argilla_push_task()
    sample = tmp_path / "sample.jsonl"
    write_jsonl(
        [{"record_id": "r1", "title": "regular"}],
        sample,
    )
    suggestions = tmp_path / "suggestions.jsonl"
    write_jsonl(
        [
            {
                "record_id": "r1",
                "suggestions": {"label": "yes"},
                "scores": {"label": "not-a-number"},
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
        {"suggestions_path": str(suggestions)},
    )

    assert len(records[0].suggestions) == 1
    assert records[0].suggestions[0].kwargs == {
        "question_name": "label",
        "value": "yes",
        "agent": "codex_exec:v001",
    }


def test_argilla_suggestions_encode_integer_label_values_as_strings(tmp_path: Path):
    task = TaskConfig(
        path=Path("task.yaml"),
        raw={
            "task_id": "argilla_suggestion_value_task",
            "id_field": "record_id",
            "input": {"path": "input.jsonl", "text_fields": ["title"]},
            "labels": {
                "primary": {"name": "label", "type": "categorical", "values": ["yes", "no"]},
                "auxiliary": [
                    {"name": "flag", "type": "integer", "values": [0, 1]},
                    {"name": "bool_flag", "type": "boolean"},
                ],
            },
        },
    )
    sample = tmp_path / "sample.jsonl"
    write_jsonl([{"record_id": "r1", "title": "one"}], sample)
    suggestions = tmp_path / "suggestions.jsonl"
    write_jsonl(
        [
            {
                "record_id": "r1",
                "suggestions": {"label": "yes", "flag": 0, "bool_flag": False},
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
        {"suggestions_path": str(suggestions)},
    )

    values = {item.kwargs["question_name"]: item.kwargs["value"] for item in records[0].suggestions}
    assert values == {"label": "yes", "flag": "0", "bool_flag": "false"}


def test_argilla_suggestions_skip_wrapped_null_values(tmp_path: Path):
    task = TaskConfig(
        path=Path("task.yaml"),
        raw={
            "task_id": "argilla_suggestion_null_value_task",
            "id_field": "record_id",
            "input": {"path": "input.jsonl", "text_fields": ["title"]},
            "labels": {
                "primary": {"name": "label", "type": "categorical", "values": ["yes", "no"]},
                "auxiliary": [
                    {"name": "flag", "type": "integer", "values": [0, 1]},
                    {"name": "bool_flag", "type": "boolean"},
                ],
            },
        },
    )
    sample = tmp_path / "sample.jsonl"
    write_jsonl([{"record_id": "r1", "title": "one"}], sample)
    suggestions = tmp_path / "suggestions.jsonl"
    write_jsonl(
        [
            {
                "record_id": "r1",
                "suggestions": {
                    "label": "yes",
                    "flag": {"value": None},
                    "bool_flag": {"value": None},
                },
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
        {"suggestions_path": str(suggestions)},
    )

    values = {item.kwargs["question_name"]: item.kwargs["value"] for item in records[0].suggestions}
    assert values == {"label": "yes"}


def test_argilla_suggestion_fallback_drops_agent_before_score():
    class ScoreOnlySuggestion:
        def __init__(self, **kwargs):
            if "agent" in kwargs:
                raise TypeError("agent not supported")
            self.kwargs = kwargs

    fake_rg = types.SimpleNamespace(Suggestion=ScoreOnlySuggestion)

    suggestion = _make_suggestion(fake_rg, question_name="label", value="yes", score=0.7, agent="local_stub:v001")

    assert suggestion.kwargs == {"question_name": "label", "value": "yes", "score": 0.7}


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
    def __init__(self, name="argilla", resource_id="00000000-0000-0000-0000-000000000001"):
        self.name = name
        self.id = resource_id


class _Settings:
    def __init__(self, guidelines="guidelines"):
        self.guidelines = guidelines

    def serialize(self):
        return {
            "guidelines": self.guidelines,
            "fields": [{"name": "text", "type": "text"}],
            "questions": [{"name": "label", "type": "label_selection", "labels": ["yes", "no"]}],
            "vectors": [],
            "metadata": [],
            "allow_extra_metadata": True,
            "distribution": {"strategy": "overlap", "min_submitted": 1},
            "mapping": {},
        }


class _Dataset:
    def __init__(
        self,
        name="dataset_a",
        resource_id="00000000-0000-0000-0000-000000000002",
        records=None,
        min_submitted=1,
    ):
        self.name = name
        self.workspace = _Workspace()
        self.id = resource_id
        self.records = list(records or [])
        self.distribution = types.SimpleNamespace(min_submitted=min_submitted)
        self.settings = _Settings()
        self.created = 0
        self.deleted = 0

    def create(self):
        self.created += 1
        return self

    def delete(self):
        self.deleted += 1

    def get(self):
        return self


class _Datasets:
    def __init__(self, items):
        self._items = items

    def list(self):
        return self._items


class _Lookup:
    def __init__(self, *, by_id=None, by_name=None):
        self.by_id = dict(by_id or {})
        self.by_name = dict(by_name or {})

    def __call__(self, name=None, id=None, workspace=None):
        if id is not None:
            return self.by_id.get(str(id))
        return self.by_name.get(str(name))


class _Users:
    def __init__(self, users):
        self._users = list(users)

    def list(self, workspace=None):
        return list(self._users)


class _PullClient:
    def __init__(self, workspace, dataset, users):
        self.workspaces = _Lookup(by_id={str(workspace.id): workspace}, by_name={workspace.name: workspace})
        self.datasets = _Lookup(by_id={str(dataset.id): dataset}, by_name={dataset.name: dataset})
        self.users = _Users(users)


def _pull_contract(task: TaskConfig, dataset: _Dataset, *, push_fingerprint="f" * 64) -> dict:
    return {
        "schema_version": 1,
        "server_version": "2.8.0",
        "sdk_version": "2.8.0",
        "workspace": {"uuid": str(dataset.workspace.id), "name": dataset.workspace.name},
        "dataset": {"uuid": str(dataset.id), "name": dataset.name},
        "min_submitted": int(dataset.distribution.min_submitted),
        "fingerprints": {
            "task": _task_fingerprint(task),
            "sample": "2" * 64,
            "batch": "3" * 64,
            "plan": "4" * 64,
            "settings": _settings_fingerprint(dataset.settings),
            "push": push_fingerprint,
        },
    }


def _remote_record(record_id: str, fingerprint: str, responses=None):
    return types.SimpleNamespace(
        id=record_id,
        metadata={_ARGILLA_PUSH_FINGERPRINT_FIELD: fingerprint},
        responses=list(responses or []),
    )


def test_argilla_dataset_existing_policy_fail_and_idempotent_resume():
    fingerprint = "a" * 64
    submitted = types.SimpleNamespace(status="submitted")
    existing = _Dataset("dataset_a", records=[_remote_record("r1", fingerprint, [submitted])])
    created = _Dataset("dataset_a")

    with pytest.raises(ValueError, match="已存在"):
        _prepare_dataset(
            created,
            existing,
            "fail",
            push_fingerprint=fingerprint,
            settings_fingerprint=_settings_fingerprint(existing.settings),
            desired_record_ids={"r1"},
            min_submitted=1,
        )

    dataset, action = _prepare_dataset(
        created,
        existing,
        "resume",
        push_fingerprint=fingerprint,
        settings_fingerprint=_settings_fingerprint(existing.settings),
        desired_record_ids={"r1", "r2"},
        min_submitted=1,
    )
    assert dataset is existing
    assert action == "resumed"
    assert existing.records[0].responses == [submitted]
    assert existing.deleted == 0


def test_argilla_dataset_resume_rejects_empty_unmarked_dataset_with_recovery_path():
    existing = _Dataset("dataset_a", records=[])
    created = _Dataset("dataset_a")

    with pytest.raises(ValueError, match="为空.*if_exists='replace'"):
        _prepare_dataset(
            created,
            existing,
            "resume",
            push_fingerprint="a" * 64,
            settings_fingerprint=_settings_fingerprint(existing.settings),
            desired_record_ids={"r1"},
            min_submitted=1,
        )


def test_argilla_dataset_resume_rejects_live_settings_drift():
    fingerprint = "a" * 64
    existing = _Dataset("dataset_a", records=[_remote_record("r1", fingerprint)])
    existing.settings = _Settings("changed guidelines")
    created = _Dataset("dataset_a")

    with pytest.raises(ValueError, match="live settings/schema"):
        _prepare_dataset(
            created,
            existing,
            "resume",
            push_fingerprint=fingerprint,
            settings_fingerprint=_settings_fingerprint(_Settings("expected guidelines")),
            desired_record_ids={"r1"},
            min_submitted=1,
        )


def test_argilla_dataset_append_rejects_different_plan_fingerprint():
    existing = _Dataset("dataset_a", records=[_remote_record("r1", "a" * 64)])
    created = _Dataset("dataset_a")

    with pytest.raises(ValueError, match="fingerprint"):
        _prepare_dataset(
            created,
            existing,
            "append",
            push_fingerprint="b" * 64,
            settings_fingerprint=_settings_fingerprint(existing.settings),
            desired_record_ids={"r1"},
            min_submitted=1,
        )


def test_argilla_dataset_replace_is_blocked_after_any_response():
    existing = _Dataset(
        "dataset_a",
        records=[_remote_record("r1", "a" * 64, [types.SimpleNamespace(status="draft")])],
    )
    created = _Dataset("dataset_a")

    with pytest.raises(ValueError, match="已有回答"):
        _prepare_dataset(
            created,
            existing,
            "replace",
            push_fingerprint="b" * 64,
            settings_fingerprint=_settings_fingerprint(existing.settings),
            desired_record_ids={"r1"},
            min_submitted=1,
        )
    assert existing.deleted == 0


def test_argilla_dataset_replace_without_responses_creates_new_dataset():
    existing = _Dataset("dataset_a", records=[_remote_record("r1", "a" * 64)])
    created = _Dataset("dataset_a")

    dataset, action = _prepare_dataset(
        created,
        existing,
        "replace",
        push_fingerprint="b" * 64,
        settings_fingerprint=_settings_fingerprint(existing.settings),
        desired_record_ids={"r1"},
        min_submitted=1,
    )
    assert dataset is created
    assert action == "replaced"
    assert existing.deleted == 1
    assert created.created == 1


def test_argilla_push_fingerprints_are_stable_and_plan_sensitive(tmp_path: Path):
    task = _argilla_push_task()
    sample = tmp_path / "sample.jsonl"
    batch = tmp_path / "batch_00001.jsonl"
    plan = tmp_path / "manifest.json"
    write_jsonl([{"record_id": "r1", "title": "one"}], sample)
    write_jsonl([{"record_id": "r1", "title": "one"}], batch)
    write_json({"schema_version": 2, "sample": str(sample), "plan_id": "plan_a"}, plan)
    params = {
        "dispatch_mode": "batch_plan",
        "sample_path": str(sample),
        "batch_files": [str(batch)],
        "batch_ids": [batch.name],
        "batch_manifest_path": str(plan),
    }

    first = _push_fingerprints(task, batch, params)
    second = _push_fingerprints(task, batch, params)
    write_json({"schema_version": 2, "sample": str(sample), "plan_id": "plan_b"}, plan)
    changed = _push_fingerprints(task, batch, params)

    assert first == second
    assert set(first) == {"task", "sample", "batch", "plan"}
    assert all(len(value) == 64 for value in first.values())
    assert changed["task"] == first["task"]
    assert changed["sample"] == first["sample"]
    assert changed["batch"] == first["batch"]
    assert changed["plan"] != first["plan"]


def test_argilla_contract_contains_versions_uuids_and_all_fingerprints():
    workspace = _Workspace()
    dataset = _Dataset()
    fingerprints = {
        name: str(index) * 64
        for index, name in enumerate(("task", "sample", "batch", "plan", "settings"), start=1)
    }

    contract = _build_contract(
        versions={"server": "2.8.0", "sdk": "2.8.0"},
        workspace=workspace,
        dataset=dataset,
        min_submitted=2,
        fingerprints=fingerprints,
        push_fingerprint="f" * 64,
    )

    assert contract["server_version"] == "2.8.0"
    assert contract["sdk_version"] == "2.8.0"
    assert contract["workspace"] == {"uuid": str(workspace.id), "name": "argilla"}
    assert contract["dataset"] == {"uuid": str(dataset.id), "name": "dataset_a"}
    assert contract["min_submitted"] == 2
    assert contract["fingerprints"] == {**fingerprints, "push": "f" * 64}


def test_argilla_settings_fingerprint_ignores_server_ids_but_detects_schema_drift():
    def settings(payload):
        return types.SimpleNamespace(serialize=lambda: payload)

    first = {
        "guidelines": "label carefully",
        "fields": [{"id": "10000000-0000-0000-0000-000000000001", "name": "text", "type": "text"}],
        "questions": [{"id": "20000000-0000-0000-0000-000000000001", "name": "label", "type": "label"}],
        "distribution": {"strategy": "overlap", "min_submitted": 1},
    }
    same_schema_new_ids = {
        **first,
        "fields": [{"id": "10000000-0000-0000-0000-000000000099", "name": "text", "type": "text"}],
        "questions": [{"id": "20000000-0000-0000-0000-000000000099", "name": "label", "type": "label"}],
    }
    drifted = {**same_schema_new_ids, "guidelines": "changed"}

    assert _settings_fingerprint(settings(first)) == _settings_fingerprint(settings(same_schema_new_ids))
    assert _settings_fingerprint(settings(first)) != _settings_fingerprint(settings(drifted))


def test_argilla_server_2_8_version_route_contract(monkeypatch):
    observed = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        return io.BytesIO(b'{"version":"2.8.0"}')

    monkeypatch.setattr(argilla.urllib.request, "urlopen", fake_urlopen)

    assert _server_version("https://argilla.example/", timeout=3.0) == "2.8.0"
    assert observed == {"url": "https://argilla.example/api/v1/version", "timeout": 3.0}


def test_argilla_pull_accepts_only_submitted_groups_and_preserves_user_identity(tmp_path: Path, monkeypatch):
    task = _argilla_push_task()
    push_fingerprint = "f" * 64
    submitted_user = UUID("10000000-0000-0000-0000-000000000001")
    draft_user = UUID("10000000-0000-0000-0000-000000000002")
    discarded_user = UUID("10000000-0000-0000-0000-000000000003")
    mixed_user = UUID("10000000-0000-0000-0000-000000000004")
    unknown_user = UUID("10000000-0000-0000-0000-000000000005")
    record = types.SimpleNamespace(
        id="r1",
        status="completed",
        metadata={"record_id": "r1", _ARGILLA_PUSH_FINGERPRINT_FIELD: push_fingerprint},
        suggestions=[types.SimpleNamespace(value="yes")],
        responses=[
            types.SimpleNamespace(question_name="label", value="yes", user_id=submitted_user, status="submitted"),
            types.SimpleNamespace(question_name="label", value="no", user_id=draft_user, status="draft"),
            types.SimpleNamespace(question_name="label", value="no", user_id=discarded_user, status="discarded"),
            types.SimpleNamespace(question_name="label", value="yes", user_id=mixed_user, status="submitted"),
            types.SimpleNamespace(question_name="reason", value="mixed", user_id=mixed_user, status="draft"),
            types.SimpleNamespace(question_name="label", value="yes", user_id=unknown_user, status="completed"),
        ],
    )
    dataset = _Dataset("dataset", records=[record])
    users = [
        types.SimpleNamespace(id=user_id, username=f"user_{index}", role=types.SimpleNamespace(value="annotator"))
        for index, user_id in enumerate((submitted_user, draft_user, discarded_user, mixed_user, unknown_user), start=1)
    ]
    client = _PullClient(dataset.workspace, dataset, users)
    contract = _pull_contract(task, dataset, push_fingerprint=push_fingerprint)
    manifest = {"task_id": task.task_id, "argilla_dataset": dataset.name, "argilla_contract": contract}
    output = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(argilla, "_runtime_versions", lambda rg, api_url: {"sdk": "2.8.0", "server": "2.8.0"})
    monkeypatch.setattr(argilla, "_client", lambda api_url=None, api_key=None: client)

    result = argilla.pull_responses(
        task,
        dataset.name,
        output,
        {"manifest": manifest, "api_key": "do-not-leak-this-key"},
    )

    rows = read_jsonl(output)
    assert rows == [
        {
            "record_id": "r1",
            "human_label": {"label": "yes"},
            "source": "argilla",
            "user_id": str(submitted_user),
            "user_username": "user_1",
            "user_role": "annotator",
            "workspace_uuid": str(dataset.workspace.id),
            "status": "submitted",
            "response_status": "submitted",
        }
    ]
    assert result["responses"] == 1
    assert result["skipped_response_groups"] == 4
    assert result["quarantined_response_groups"] == 2
    assert result["users"] == [
        {
            "uuid": str(submitted_user),
            "username": "user_1",
            "role": "annotator",
            "workspace_uuid": str(dataset.workspace.id),
        }
    ]
    assert "do-not-leak-this-key" not in str(result)
    assert "do-not-leak-this-key" not in output.read_text(encoding="utf-8")


def test_argilla_pull_requires_complete_manifest_without_leaking_api_key(tmp_path: Path):
    task = _argilla_push_task()
    secret = "sensitive-api-key"

    with pytest.raises(ValueError) as exc_info:
        argilla.pull_responses(task, "dataset", tmp_path / "out.jsonl", {"api_key": secret})

    assert "manifest" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_argilla_pull_rejects_manifest_missing_required_fingerprint(tmp_path: Path):
    task = _argilla_push_task()
    dataset = _Dataset("dataset", records=[_remote_record("r1", "f" * 64)])
    contract = _pull_contract(task, dataset)
    contract["fingerprints"].pop("settings")

    with pytest.raises(ValueError, match="settings fingerprint"):
        argilla.pull_responses(
            task,
            dataset.name,
            tmp_path / "out.jsonl",
            {"manifest": {"task_id": task.task_id, "argilla_dataset": dataset.name, "argilla_contract": contract}},
        )


def test_argilla_pull_rejects_same_name_workspace_environment_drift(tmp_path: Path, monkeypatch):
    task = _argilla_push_task()
    record = _remote_record("r1", "f" * 64)
    dataset = _Dataset("dataset", records=[record])
    contract = _pull_contract(task, dataset)
    drift_workspace = _Workspace(resource_id="00000000-0000-0000-0000-000000000099")
    client = _PullClient(drift_workspace, dataset, [])
    client.workspaces = _Lookup(by_name={drift_workspace.name: drift_workspace})
    monkeypatch.setattr(argilla, "_runtime_versions", lambda rg, api_url: {"sdk": "2.8.0", "server": "2.8.0"})
    monkeypatch.setattr(argilla, "_client", lambda api_url=None, api_key=None: client)

    with pytest.raises(ValueError, match="workspace UUID.*拒绝按同名"):
        argilla.pull_responses(
            task,
            dataset.name,
            tmp_path / "out.jsonl",
            {"manifest": {"task_id": task.task_id, "argilla_dataset": dataset.name, "argilla_contract": contract}},
        )


def test_argilla_pull_rejects_dataset_uuid_mismatch(tmp_path: Path, monkeypatch):
    task = _argilla_push_task()
    dataset = _Dataset("dataset", records=[_remote_record("r1", "f" * 64)])
    contract = _pull_contract(task, dataset)
    wrong_dataset = _Dataset(
        "dataset",
        resource_id="00000000-0000-0000-0000-000000000099",
        records=dataset.records,
    )
    client = _PullClient(dataset.workspace, wrong_dataset, [])
    client.datasets = _Lookup(by_id={contract["dataset"]["uuid"]: wrong_dataset})
    monkeypatch.setattr(argilla, "_runtime_versions", lambda rg, api_url: {"sdk": "2.8.0", "server": "2.8.0"})
    monkeypatch.setattr(argilla, "_client", lambda api_url=None, api_key=None: client)

    with pytest.raises(ValueError, match="dataset 身份"):
        argilla.pull_responses(
            task,
            dataset.name,
            tmp_path / "out.jsonl",
            {"manifest": {"task_id": task.task_id, "argilla_dataset": dataset.name, "argilla_contract": contract}},
        )


def test_argilla_pull_rejects_live_settings_drift(tmp_path: Path, monkeypatch):
    task = _argilla_push_task()
    dataset = _Dataset("dataset", records=[_remote_record("r1", "f" * 64)])
    contract = _pull_contract(task, dataset)
    dataset.settings = _Settings("changed after push")
    client = _PullClient(dataset.workspace, dataset, [])
    monkeypatch.setattr(argilla, "_runtime_versions", lambda rg, api_url: {"sdk": "2.8.0", "server": "2.8.0"})
    monkeypatch.setattr(argilla, "_client", lambda api_url=None, api_key=None: client)

    with pytest.raises(ValueError, match="live settings/schema"):
        argilla.pull_responses(
            task,
            dataset.name,
            tmp_path / "out.jsonl",
            {"manifest": {"task_id": task.task_id, "argilla_dataset": dataset.name, "argilla_contract": contract}},
        )


def test_argilla_connection_status_uses_client_me_and_workspaces(monkeypatch):
    class _FakeClient:
        me = types.SimpleNamespace(username="argilla", role=types.SimpleNamespace(value="owner"))
        workspaces = _Datasets([_Workspace()])

    monkeypatch.setattr(argilla, "_client", lambda api_url=None, api_key=None: _FakeClient())

    status = argilla.test_connection({"workspace": "argilla"})

    assert status["ok"] is True
    assert status["user"]["username"] == "argilla"
    assert status["workspace_exists"] is True
