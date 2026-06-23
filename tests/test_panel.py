from __future__ import annotations

import base64
import hmac
from pathlib import Path

from llm_labeling_scaffold import panel


def _decode_basic(header: str) -> tuple[str, str]:
    raw = base64.b64decode(header[6:]).decode("utf-8")
    user, _, pw = raw.partition(":")
    return user, pw


def test_discover_runs_finds_demo():
    runs = panel._discover_runs(Path("runs"))
    ids = {(r["task_id"], r["run_id"]) for r in runs}
    assert ("toy_multiclass_v1", "demo") in ids
    demo = next(r for r in runs if r["run_id"] == "demo")
    assert demo["merge"] and demo["merge"]["merged_rows"] == 12


def test_sample_rows_returns_merged():
    runs = panel._discover_runs(Path("runs"))
    demo = next(r for r in runs if r["run_id"] == "demo")
    rows = panel._sample_rows(Path(demo["path"]))
    assert rows and "record_id" in rows[0]


def test_basic_auth_roundtrip():
    header = "Basic " + base64.b64encode(b"admin:secret123").decode()
    user, pw = _decode_basic(header)
    assert hmac.compare_digest(user, "admin")
    assert hmac.compare_digest(pw, "secret123")
    assert not hmac.compare_digest(pw, "wrong")


def test_run_detail_and_pools():
    runs = panel.discover_runs(Path("runs"))
    demo = next(r for r in runs if r["run_id"] == "demo")
    detail = panel.run_detail(Path(demo["path"]))
    assert "pools" in detail and "merged" in detail["pools"]
    assert detail["pools"]["merged"] == 12
    for kind in ("merged", "missing", "duplicate", "conflict"):
        assert isinstance(panel.pool_rows(Path(demo["path"]), kind), list)


def test_list_gold():
    gold = panel.list_gold(Path("runs"), "toy_multiclass_v1")
    assert gold and gold[0]["task_id"] == "toy_multiclass_v1"


def test_append_decision_roundtrip(tmp_path=None):
    import shutil
    import tempfile
    runs = panel.discover_runs(Path("runs"))
    demo = next(r for r in runs if r["run_id"] == "demo")
    tmp = Path(tempfile.mkdtemp())
    try:
        dst = tmp / "toy_multiclass_v1" / "demo"
        shutil.copytree(Path(demo["path"]), dst)
        n = panel.append_decision(dst, {"record_id": "r001", "human_label": {"class_label": "non_target"}, "note": "t"})
        assert n == 1
        assert (dst / "adjudication" / "decisions.jsonl").exists()
    finally:
        shutil.rmtree(tmp)


def test_safe_segment_blocks_traversal():
    assert panel._safe_segment("demo")
    assert not panel._safe_segment("../etc")
    assert not panel._safe_segment("a/b")
