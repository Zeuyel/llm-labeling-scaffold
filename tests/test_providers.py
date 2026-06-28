from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from llm_labeling_scaffold import pipeline
from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.providers.codex_exec import CodexExecProvider


def _task(tmp_path: Path):
    created = pipeline.create_task(
        tmp_path / "tasks",
        {
            "task_id": "codex_provider_task",
            "id_field": "record_id",
            "text_fields": ["title"],
            "primary_label_name": "label",
            "primary_label_values": ["yes", "no"],
        },
    )
    return load_task(created["path"])


def test_codex_exec_provider_parses_results_from_output_file(tmp_path: Path):
    task = _task(tmp_path)
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    def runner(command, **kwargs):
        assert command[:2] == [str(fake_bin), "exec"]
        assert "--output-schema" in command
        assert kwargs["cwd"] == str(tmp_path)
        assert "codex_provider_task" in kwargs["input"]
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text(json.dumps({"results": [{"record_id": "r1", "label": "yes"}]}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    provider = CodexExecProvider(codex_bin=str(fake_bin), cwd=tmp_path, runner=runner)
    payload = provider.annotate_batch([{"record_id": "r1", "title": "remote platform"}], task)

    assert payload == {"results": [{"record_id": "r1", "label": "yes"}]}


def test_codex_exec_provider_requires_codex_cli(tmp_path: Path):
    task = _task(tmp_path)
    provider = CodexExecProvider(codex_bin="lls-codex-exec-missing")

    with pytest.raises(RuntimeError, match="requires Codex CLI"):
        provider.annotate_batch([{"record_id": "r1", "title": "remote platform"}], task)
