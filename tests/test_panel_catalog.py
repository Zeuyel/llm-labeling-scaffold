from __future__ import annotations

from llm_labeling_scaffold import data_lake, panel


def test_data_lake_catalog_lists_registered_datasets(monkeypatch):
    registry = {
        "version": "2026-07-17",
        "datasets": {
            "patent_inputs": {
                "name": "专利输入",
                "layer": "labels",
                "domain": "patent",
                "manifest": "r2:lake/manifests/patent.json",
            },
        },
    }
    manifest = {
        "dataset_id": "patent_inputs",
        "revision": 3,
        "objects": [{
            "path": "inputs/v1/raw.jsonl",
            "storage_uri": "r2:lake/inputs/v1/raw.jsonl",
            "asset_type": "label_import_jsonl",
            "rows": 2,
            "bytes": 100,
            "sha256": "a" * 64,
            "id_field": "patent_id",
        }],
    }
    monkeypatch.setattr(data_lake, "default_registry_uri", lambda: "r2:lake/registry.yaml")
    monkeypatch.setattr(data_lake, "read_yaml_uri", lambda uri: registry)
    monkeypatch.setattr(data_lake, "read_json_uri", lambda uri: manifest)

    summary = panel._data_lake_catalog_payload()
    assert summary["backend"] == "r2"
    assert summary["registry_uri"] == "r2:lake/registry.yaml"
    assert summary["datasets"][0]["dataset_id"] == "patent_inputs"
    assert "manifest" not in summary["datasets"][0]

    detail = panel._data_lake_catalog_payload(detail_dataset_id="patent_inputs")
    assert detail["datasets"][0]["manifest"]["version"] == "3"
    assert detail["datasets"][0]["manifest"]["objects"][0]["rows"] == 2
