from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .base import AnnotationProvider
from ..config import TaskConfig, build_text


Runner = Callable[..., subprocess.CompletedProcess]


def _resolve_codex_bin(codex_bin: str) -> str:
    if "/" in codex_bin or "\\" in codex_bin:
        path = Path(codex_bin)
        if path.exists():
            return str(path)
        raise RuntimeError(f"codex_exec provider 找不到 Codex CLI: {codex_bin}")
    resolved = shutil.which(codex_bin)
    if not resolved:
        raise RuntimeError("codex_exec provider requires Codex CLI (`codex exec`) on PATH")
    return resolved


def _json_schema(row_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "minItems": row_count,
                "maxItems": row_count,
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
        },
    }


def _label_contract(task: TaskConfig) -> dict[str, Any]:
    return {
        "primary": task.primary_label,
        "auxiliary": task.auxiliary_labels,
    }


def _prompt(task: TaskConfig, rows: list[dict]) -> str:
    payload = {
        "task_id": task.task_id,
        "id_field": task.id_field,
        "text_fields": task.text_fields,
        "label_contract": _label_contract(task),
        "records": [
            {
                task.id_field: row.get(task.id_field),
                "text": build_text(row, task),
                "row": row,
            }
            for row in rows
        ],
    }
    return (
        "You generate machine prelabel suggestions for an Argilla annotation job.\n"
        "Return only a JSON object with key `results`.\n"
        "The results array must have exactly the same length and order as records.\n"
        "Each result must include the task id field and predicted label fields from the label_contract.\n"
        "Do not emit human_label, responses, markdown, or explanatory text.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError("codex_exec provider 没有返回合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("codex_exec provider 必须返回 JSON object")
    return payload


class CodexExecProvider(AnnotationProvider):
    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        timeout_seconds: int | None = None,
        cwd: str | Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.codex_bin = codex_bin or os.environ.get("LLS_CODEX_EXEC_BIN") or "codex"
        self.model = model or os.environ.get("LLS_CODEX_EXEC_MODEL")
        self.profile = profile or os.environ.get("LLS_CODEX_EXEC_PROFILE")
        self.timeout_seconds = timeout_seconds or int(os.environ.get("LLS_CODEX_EXEC_TIMEOUT_SECONDS", "600"))
        self.cwd = str(cwd or os.environ.get("LLS_CODEX_EXEC_CWD") or Path.cwd())
        self.runner = runner or subprocess.run

    def annotate_batch(self, rows: list[dict], task: TaskConfig) -> dict:
        codex_bin = _resolve_codex_bin(self.codex_bin)
        with tempfile.TemporaryDirectory(prefix="lls-codex-exec-") as tmp:
            tmp_dir = Path(tmp)
            schema_path = tmp_dir / "schema.json"
            output_path = tmp_dir / "last_message.json"
            schema_path.write_text(json.dumps(_json_schema(len(rows)), ensure_ascii=False), encoding="utf-8")
            command = [
                codex_bin,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "-o",
                str(output_path),
                "-",
            ]
            if self.model:
                command[2:2] = ["--model", self.model]
            if self.profile:
                command[2:2] = ["--profile", self.profile]
            try:
                completed = self.runner(
                    command,
                    input=_prompt(task, rows),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    cwd=self.cwd,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"codex_exec provider 超时: {self.timeout_seconds}s") from exc
            if completed.returncode != 0:
                stderr = str(completed.stderr or "").strip()
                stdout = str(completed.stdout or "").strip()
                detail = stderr or stdout or f"exit code {completed.returncode}"
                raise RuntimeError(f"codex_exec provider 执行失败: {detail[:500]}")
            if output_path.exists() and output_path.read_text(encoding="utf-8").strip():
                payload = _parse_json_object(output_path.read_text(encoding="utf-8"))
            else:
                payload = _parse_json_object(str(completed.stdout or ""))
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("codex_exec provider 返回 JSON 缺少 results 列表")
        if len(results) != len(rows):
            raise ValueError(f"codex_exec provider 返回 {len(results)} 条结果, 但输入有 {len(rows)} 条")
        return payload
