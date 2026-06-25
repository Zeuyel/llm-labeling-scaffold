from __future__ import annotations

import base64
import hmac
from pathlib import Path

from llm_labeling_scaffold import panel
from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.gold import build_gold_from_decisions
from llm_labeling_scaffold.io import read_json, read_jsonl


def _decode_basic(header: str) -> tuple[str, str]:
    raw = base64.b64decode(header[6:]).decode("utf-8")
    user, _, pw = raw.partition(":")
    return user, pw


def test_discover_runs_finds_demo(panel_workspace):
    runs = panel._discover_runs(panel_workspace["runs_root"])
    ids = {(r["task_id"], r["run_id"]) for r in runs}
    assert ("toy_multiclass_v1", "demo") in ids
    demo = next(r for r in runs if r["run_id"] == "demo")
    assert demo["merge"] and demo["merge"]["merged_rows"] == 2


def test_sample_rows_returns_merged(panel_workspace):
    runs = panel._discover_runs(panel_workspace["runs_root"])
    demo = next(r for r in runs if r["run_id"] == "demo")
    rows = panel._sample_rows(Path(demo["path"]))
    assert rows and "record_id" in rows[0]


def test_basic_auth_roundtrip():
    header = "Basic " + base64.b64encode(b"admin:secret123").decode()
    user, pw = _decode_basic(header)
    assert hmac.compare_digest(user, "admin")
    assert hmac.compare_digest(pw, "secret123")
    assert not hmac.compare_digest(pw, "wrong")


def test_run_detail_and_pools(panel_workspace):
    runs = panel.discover_runs(panel_workspace["runs_root"])
    demo = next(r for r in runs if r["run_id"] == "demo")
    detail = panel.run_detail(Path(demo["path"]))
    assert "pools" in detail and "merged" in detail["pools"]
    assert detail["pools"]["merged"] == 2
    for kind in ("merged", "missing", "duplicate", "conflict"):
        assert isinstance(panel.pool_rows(Path(demo["path"]), kind), list)


def test_list_gold(panel_workspace):
    gold = panel.list_gold(panel_workspace["runs_root"], "toy_multiclass_v1")
    assert gold and gold[0]["task_id"] == "toy_multiclass_v1"


def test_append_decision_roundtrip(panel_workspace):
    run_dir = panel_workspace["run_dir"]
    n = panel.append_decision(
        run_dir,
        {"record_id": "r001", "human_label": {"class_label": "non_target"}, "note": "t"},
    )
    assert n == 1
    assert (run_dir / "adjudication" / "decisions.jsonl").exists()


def test_safe_segment_blocks_traversal():
    assert panel._safe_segment("demo")
    assert not panel._safe_segment("../etc")
    assert not panel._safe_segment("a/b")
    assert not panel._safe_segment("a\\b")


def test_parse_import_rows_accepts_jsonl_and_json_array():
    rows = panel.parse_import_rows('{"record_id":"r001"}\n{"record_id":"r002"}\n')
    assert [row["record_id"] for row in rows] == ["r001", "r002"]

    rows = panel.parse_import_rows('[{"record_id":"r003"}]')
    assert rows == [{"record_id": "r003"}]


def test_parse_import_rows_rejects_bad_lines():
    try:
        panel.parse_import_rows('{"record_id":"r001"}\nnot-json\n')
    except ValueError as exc:
        assert "第 2 行" in str(exc)
    else:
        raise AssertionError("bad JSONL line should fail")


def test_list_decision_artifacts_reads_manifest(panel_workspace):
    artifacts = pipeline.list_decision_artifacts(panel_workspace["runs_root"], "toy_multiclass_v1")
    assert artifacts == [
        {
            "task_id": "toy_multiclass_v1",
            "decision_id": "argilla_round_1",
            "source": "argilla",
            "argilla_dataset": "toy_argilla_round_1",
            "sample_id": "sample_a",
            "sample_path": str(panel_workspace["sample_path"]),
            "path": str(panel_workspace["decisions_path"]),
            "rows": 2,
        }
    ]


def test_build_gold_from_sample_and_decisions(panel_workspace):
    task = panel_workspace["task"]
    gold_path = build_gold_from_decisions(
        task,
        panel_workspace["sample_path"],
        panel_workspace["decisions_path"],
        "from_decisions_v001",
    )

    rows = read_jsonl(gold_path)
    assert [row["record_id"] for row in rows] == ["r001", "r002"]
    assert rows[1]["title"] == "Service upgrade"
    assert rows[1]["class_label"] == "service_upgrade"
    assert rows[1]["gold_source"] == "argilla"

    manifest = read_json(gold_path.parent / "gold_from_decisions_v001.manifest.json")
    assert manifest["source"] == "decision_artifact"
    assert manifest["rows"] == 2
    assert manifest["sample_path"] == str(panel_workspace["sample_path"])
