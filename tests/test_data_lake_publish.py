from pathlib import Path
import hashlib
import json

import pytest
import yaml

from llm_labeling_scaffold import data_lake
from llm_labeling_scaffold.cli import main as cli_main
from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.io import read_json, write_json, write_jsonl


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_task(tmp_path: Path, *, output_base_uri: str = "r2:test-lake/labels/demo_task/"):
    task_dir = tmp_path / "tasks" / "demo_task"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "task.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "task_id": "demo_task",
                "id_field": "record_id",
                "input": {"path": "input.jsonl", "text_fields": ["text"]},
                "labels": {"primary": {"name": "label", "values": ["yes", "no"]}},
                "data_lake": {
                    "output_base_uri": output_base_uri,
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return load_task(task_path), tmp_path / "runs"


def _write_artifacts(runs_root: Path, task_id: str) -> None:
    task_dir = runs_root / task_id
    decisions_dir = task_dir / "decisions" / "round_1"
    write_jsonl([{"record_id": "r1", "human_label": {"label": "yes"}}], decisions_dir / "decisions.jsonl")
    write_json({"task_id": task_id, "decision_id": "round_1", "rows": 1}, decisions_dir / "manifest.json")

    gold_dir = task_dir / "gold"
    write_jsonl([{"record_id": "r1", "label": "yes"}], gold_dir / "gold_v001.jsonl")
    write_json({"task_id": task_id, "version": "v001", "rows": 1}, gold_dir / "gold_v001.manifest.json")

    inference_dir = task_dir / "inference" / "batch_1"
    write_jsonl([{"record_id": "r1", "pred_label": "yes"}], inference_dir / "predictions.jsonl")
    write_json({"rows": 1, "model_path": "models/model_a/model.joblib"}, inference_dir / "inference_summary.json")

    model_dir = task_dir / "models" / "model_a"
    write_json(
        {"model_id": "model_a", "trainer": "unit", "model_path": str(model_dir / "model.joblib")},
        model_dir / "manifest.json",
    )


@pytest.mark.parametrize(
    ("kind", "artifact_id", "expected_target"),
    [
        ("decisions", "round_1", "decisions/round_1/decisions.jsonl"),
        ("gold", "v001", "gold/v001/gold_v001.jsonl"),
        ("predictions", "batch_1", "predictions/batch_1/predictions.jsonl"),
        ("model_metadata", "model_a", "model_metadata/model_a/manifest.json"),
    ],
)
def test_publish_plan_parses_supported_artifacts_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    artifact_id: str,
    expected_target: str,
):
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:test-lake/")
    task, runs_root = _write_task(tmp_path)
    _write_artifacts(runs_root, task.task_id)
    writes: list[tuple] = []
    monkeypatch.setattr(data_lake, "copy_path_to_uri", lambda *args, **kwargs: writes.append(args))

    plan = data_lake.plan_artifact_publish(task, runs_root, kind, artifact_id)

    assert writes == []
    assert plan["dry_run"] is True
    artifact = plan["artifacts"][0]
    local_path = Path(artifact["local_path"])
    assert artifact["target_uri"] == f"r2:test-lake/labels/demo_task/{expected_target}"
    assert artifact["manifest_uri"].endswith(".manifest.json")
    assert artifact["bytes"] == local_path.stat().st_size
    assert artifact["sha256"] == _sha256(local_path)
    assert artifact["manifest"]["dry_run"] is True
    assert artifact["manifest"]["storage_uri"] == artifact["target_uri"]


def test_publish_submit_requires_confirm_and_idempotency_key_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    task, runs_root = _write_task(tmp_path)
    writes: list[tuple] = []
    monkeypatch.setattr(data_lake, "copy_path_to_uri", lambda *args, **kwargs: writes.append(args))

    with pytest.raises(data_lake.DataLakeError, match="confirm"):
        data_lake.submit_artifact_publish(task, runs_root, "decisions", "round_1", idempotency_key="submit-1")

    with pytest.raises(data_lake.DataLakeError, match="idempotency key"):
        data_lake.submit_artifact_publish(task, runs_root, "decisions", "round_1", confirm=True)

    assert writes == []


def test_publish_submit_uploads_manifest_and_verifies_hashes_to_local_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LLS_ALLOW_LOCAL_DATA_LAKE_URIS", "1")
    task, runs_root = _write_task(tmp_path, output_base_uri=str(tmp_path / "lake"))
    _write_artifacts(runs_root, task.task_id)
    source = runs_root / task.task_id / "decisions" / "round_1" / "decisions.jsonl"

    result = data_lake.submit_artifact_publish(
        task,
        runs_root,
        "decisions",
        "round_1",
        confirm=True,
        idempotency_key="submit-1",
    )

    target = tmp_path / "lake" / "decisions" / "round_1" / "decisions.jsonl"
    manifest_path = tmp_path / "lake" / "decisions" / "round_1" / "decisions.manifest.json"
    publish_manifest = read_json(manifest_path)
    artifact = result["artifacts"][0]
    assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert publish_manifest["idempotency_key"] == "submit-1"
    assert publish_manifest["sha256"] == _sha256(source)
    assert artifact["verified"] is True
    assert artifact["verification"]["artifact"]["sha256"] == _sha256(source)
    assert not (tmp_path / "lake" / "registry").exists()
    assert not (tmp_path / "lake" / "current").exists()


def test_publish_plan_rejects_r2_targets_outside_allowed_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:allowed/")
    task, runs_root = _write_task(tmp_path, output_base_uri="r2:other/labels/demo_task/")
    _write_artifacts(runs_root, task.task_id)

    with pytest.raises(data_lake.DataLakeError, match="必须位于 r2:allowed/"):
        data_lake.plan_artifact_publish(task, runs_root, "decisions", "round_1")


def test_publish_plan_rejects_registry_current_output_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:test-lake/")
    task, runs_root = _write_task(
        tmp_path,
        output_base_uri="r2:test-lake/governance/data_lake/v1/current/",
    )
    _write_artifacts(runs_root, task.task_id)

    with pytest.raises(data_lake.DataLakeError, match="registry/governance/current"):
        data_lake.plan_artifact_publish(task, runs_root, "decisions", "round_1")


def test_publish_plan_rejects_artifact_id_path_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:test-lake/")
    task, runs_root = _write_task(tmp_path)
    _write_artifacts(runs_root, task.task_id)

    with pytest.raises(data_lake.DataLakeError, match="安全单段"):
        data_lake.plan_artifact_publish(task, runs_root, "decisions", "../round_1")


def test_cli_publish_plan_outputs_dry_run_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setenv("LLS_DATA_LAKE_R2_PREFIX", "r2:test-lake/")
    task, runs_root = _write_task(tmp_path)
    _write_artifacts(runs_root, task.task_id)

    cli_main([
        "data-lake",
        "publish",
        "plan",
        "--task",
        str(task.path),
        "--runs-root",
        str(runs_root),
        "--kind",
        "decisions",
        "--artifact-id",
        "round_1",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["artifacts"][0]["target_uri"] == "r2:test-lake/labels/demo_task/decisions/round_1/decisions.jsonl"
