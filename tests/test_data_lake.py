from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from llm_labeling_scaffold import data_lake
from llm_labeling_scaffold.config import TaskConfig
from llm_labeling_scaffold.data_lake import (
    DataLakeError,
    copy_path_to_uri,
    export_artifact,
    resolve_source,
    set_allowed_r2_prefix_override,
    set_default_registry_uri_override,
)
from llm_labeling_scaffold.io import write_json, write_jsonl


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task(tmp_path: Path, source_manifest_uri: str | None = None) -> TaskConfig:
    data_lake = {"output_base_uri": str(tmp_path / "published")}
    if source_manifest_uri is not None:
        data_lake["source_manifest_uri"] = source_manifest_uri
    return TaskConfig(
        path=tmp_path / "task.yaml",
        raw={
            "task_id": "task-1",
            "id_field": "record_id",
            "input": {"path": "input.jsonl"},
            "labels": {"primary": {"name": "label", "values": ["yes", "no"]}},
            "data_lake": data_lake,
        },
    )


@pytest.fixture(autouse=True)
def reset_data_lake_overrides() -> None:
    set_default_registry_uri_override(None)
    set_allowed_r2_prefix_override(None)
    yield
    set_default_registry_uri_override(None)
    set_allowed_r2_prefix_override(None)


def test_export_artifact_publishes_verifiable_source_and_output_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset", "manifest_version": "1.0"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl(
        [{"record_id": "r1", "label": "yes"}, {"record_id": "r2", "label": "no"}],
        output,
    )

    result = export_artifact(
        _task(tmp_path),
        output,
        source_manifest_uri=str(source_manifest),
        target_path="decisions/v001.jsonl",
        idempotency_key="annotation-job-1",
        version="v001",
    )

    manifest = json.loads(Path(result["manifest_uri"]).read_text(encoding="utf-8"))
    assert result["action"] == "published"
    assert result["idempotent"] is False
    assert manifest["source_manifest_uri"] == str(source_manifest)
    assert manifest["source_manifest_sha256"] == _sha256(source_manifest)
    assert manifest["idempotency_key"] == "annotation-job-1"
    assert manifest["version"] == "v001"
    assert manifest["output"] == {
        "path": "decisions/v001.jsonl",
        "storage_uri": str(tmp_path / "published" / "decisions" / "v001.jsonl"),
        "manifest_uri": str(tmp_path / "published" / "decisions" / "v001.manifest.json"),
        "bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "rows": 2,
    }
    assert manifest["bytes"] == manifest["output"]["bytes"]
    assert manifest["sha256"] == manifest["output"]["sha256"]
    assert manifest["rows"] == manifest["output"]["rows"]


def test_export_artifact_reuses_identical_publication_and_auto_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)
    task = _task(tmp_path)

    first = export_artifact(task, output, source_manifest_uri=str(source_manifest), target_path="decisions.jsonl")
    second = export_artifact(task, output, source_manifest_uri=str(source_manifest), target_path="decisions.jsonl")

    assert first["action"] == "published"
    assert second["action"] == "reused"
    assert second["idempotent"] is True
    assert second["manifest"]["idempotency_key"].startswith("auto-")


def test_export_artifact_rejects_same_version_with_different_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    first_output = tmp_path / "first.jsonl"
    second_output = tmp_path / "second.jsonl"
    write_jsonl([{"record_id": "r1", "label": "a"}], first_output)
    write_jsonl([{"record_id": "r1", "label": "b"}], second_output)
    task = _task(tmp_path)

    export_artifact(
        task,
        first_output,
        source_manifest_uri=str(source_manifest),
        target_path="decisions.jsonl",
        idempotency_key="annotation-job-1",
        version="v001",
    )

    with pytest.raises(DataLakeError, match="同版本不同 hash"):
        export_artifact(
            task,
            second_output,
            source_manifest_uri=str(source_manifest),
            target_path="decisions.jsonl",
            idempotency_key="annotation-job-1",
            version="v001",
        )


def test_export_artifact_rejects_stale_expected_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)
    task = _task(tmp_path)
    kwargs = {
        "source_manifest_uri": str(source_manifest),
        "target_path": "decisions.jsonl",
        "idempotency_key": "annotation-job-1",
        "version": "v001",
    }

    export_artifact(task, output, **kwargs)

    with pytest.raises(DataLakeError, match="expected_version 冲突"):
        export_artifact(task, output, expected_version="v000", **kwargs)


def test_export_artifact_accepts_expected_version_for_first_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)

    result = export_artifact(
        _task(tmp_path),
        output,
        source_manifest_uri=str(source_manifest),
        target_path="decisions.jsonl",
        version="v001",
        expected_version="v001",
    )

    assert result["action"] == "published"
    assert result["manifest"]["expected_version"] == "v001"


def test_export_artifact_rejects_expected_version_mismatch_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)

    with pytest.raises(DataLakeError, match="expected_version 与待发布版本不一致"):
        export_artifact(
            _task(tmp_path),
            output,
            source_manifest_uri=str(source_manifest),
            target_path="decisions.jsonl",
            version="v001",
            expected_version="v000",
        )


def test_export_artifact_rejects_orphaned_object_or_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)
    task = _task(tmp_path)
    target = tmp_path / "published" / "orphan.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(output.read_bytes())

    with pytest.raises(DataLakeError, match="产物对象已存在但 manifest 缺失"):
        export_artifact(task, output, source_manifest_uri=str(source_manifest), target_path="orphan.jsonl")

    result = export_artifact(task, output, source_manifest_uri=str(source_manifest), target_path="decisions.jsonl")
    Path(result["target_uri"]).unlink()

    with pytest.raises(DataLakeError, match="发布 manifest 已存在但产物对象缺失"):
        export_artifact(task, output, source_manifest_uri=str(source_manifest), target_path="decisions.jsonl")


def test_export_artifact_rejects_source_manifest_override_and_invalid_inline_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    configured = tmp_path / "configured-source.json"
    explicit = tmp_path / "explicit-source.json"
    write_json({"dataset_id": "configured"}, configured)
    write_json({"dataset_id": "explicit"}, explicit)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)
    task = _task(tmp_path, source_manifest_uri=str(configured))

    with pytest.raises(DataLakeError, match="source_manifest_uri 与任务配置不一致"):
        export_artifact(task, output, source_manifest_uri=str(explicit), target_path="decisions.jsonl")

    with pytest.raises(DataLakeError, match="source_manifest 必须是 JSON 对象"):
        export_artifact(task, output, source_manifest=[], target_path="other.jsonl")


def test_export_artifact_rejects_inline_manifest_for_r2_publication(tmp_path: Path):
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)

    with pytest.raises(DataLakeError, match="R2 发布必须提供 source_manifest_uri"):
        export_artifact(
            _task(tmp_path),
            output,
            target_uri="r2:ai-innovation-data-lake/labels/task-1/decisions.jsonl",
            source_manifest={"dataset_id": "input-dataset"},
        )


def test_export_artifact_requires_r2_source_manifest_for_r2_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)

    with pytest.raises(DataLakeError, match="R2 发布的 source_manifest_uri 必须是 R2 URI"):
        export_artifact(
            _task(tmp_path, source_manifest_uri=str(source_manifest)),
            output,
            target_uri="r2:ai-innovation-data-lake/labels/task-1/decisions.jsonl",
            source_manifest_uri=str(source_manifest),
        )


def test_export_artifact_rejects_request_level_r2_source_manifest_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)

    with pytest.raises(DataLakeError, match="必须来自任务配置"):
        export_artifact(
            _task(tmp_path),
            output,
            target_uri="r2:ai-innovation-data-lake/labels/task-1/decisions.jsonl",
            source_manifest_uri="r2:ai-innovation-data-lake/labels/task-1/source.manifest.json",
        )


def test_export_artifact_rejects_local_publication_in_production_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", raising=False)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)

    with pytest.raises(DataLakeError, match="生产模式不允许本地数据湖 URI"):
        export_artifact(_task(tmp_path), output, target_path="decisions.jsonl")


def test_resolve_source_requires_registry_and_manifest_metadata_to_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source = tmp_path / "source.jsonl"
    write_jsonl([{"record_id": "r1", "title": "A"}], source)
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "dataset_id": "input-dataset",
        "layer": "labels",
        "domain": "patent",
        "objects": [
            {
                "path": "inputs/seed/raw.jsonl",
                "storage_uri": str(source),
                "asset_type": "label_import_jsonl",
                "rows": 1,
                "id_field": "record_id",
                "unique_ids": 1,
                "bytes": source.stat().st_size,
                "sha256": _sha256(source),
                "created_by": "tests",
                "upstream_uri": ["r2:ai-innovation-data-lake/source.jsonl"],
                "sampling_strategy": "unit_test",
            }
        ],
    }
    write_json(manifest, manifest_path)
    registry_path = tmp_path / "data_lake.yaml"
    registry_path.write_text(
        "datasets:\n"
        "  input-dataset:\n"
        f"    manifest: {manifest_path}\n"
        "    layer: labels\n"
        "    domain: patent\n",
        encoding="utf-8",
    )
    task = TaskConfig(
        path=tmp_path / "task.yaml",
        raw={
            "task_id": "task-1",
            "id_field": "record_id",
            "input": {"path": "input.jsonl"},
            "labels": {"primary": {"name": "label", "values": ["yes", "no"]}},
            "data_lake": {
                "lake_registry_uri": str(registry_path),
                "source_dataset_id": "input-dataset",
                "source_object_path": "inputs/seed/raw.jsonl",
            },
        },
    )

    assert resolve_source(task)["selected_object"]["path"] == "inputs/seed/raw.jsonl"

    manifest["layer"] = "other"
    write_json(manifest, manifest_path)
    with pytest.raises(DataLakeError, match="manifest.layer 与 registry 不一致"):
        resolve_source(task)


def test_export_artifact_does_not_retry_orphaned_artifact_after_manifest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    source_manifest = tmp_path / "source-manifest.json"
    write_json({"dataset_id": "input-dataset"}, source_manifest)
    output = tmp_path / "decisions.jsonl"
    write_jsonl([{"record_id": "r1", "label": "yes"}], output)
    task = _task(tmp_path)
    original_copy = data_lake.copy_path_to_uri

    def fail_manifest_upload(source: str | Path, uri: str, *, immutable: bool = False) -> str:
        if uri.endswith(".manifest.json"):
            raise DataLakeError("模拟 manifest 上传失败")
        return original_copy(source, uri, immutable=immutable)

    monkeypatch.setattr(data_lake, "copy_path_to_uri", fail_manifest_upload)
    with pytest.raises(DataLakeError, match="模拟 manifest 上传失败"):
        export_artifact(task, output, source_manifest_uri=str(source_manifest), target_path="orphan.jsonl")

    monkeypatch.setattr(data_lake, "copy_path_to_uri", original_copy)
    with pytest.raises(DataLakeError, match="产物对象已存在但 manifest 缺失"):
        export_artifact(task, output, source_manifest_uri=str(source_manifest), target_path="orphan.jsonl")


def test_copy_path_to_uri_uses_rclone_immutable_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "artifact.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    target = "r2:ai-innovation-data-lake/labels/task/artifact.jsonl"
    monkeypatch.setenv("LLS_RCLONE_BIN", "rclone")

    with patch("llm_labeling_scaffold.data_lake.subprocess.run") as run:
        copy_path_to_uri(source, target, immutable=True)

    run.assert_called_once_with(
        ["rclone", "copyto", "--immutable", str(source), target],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
    )
