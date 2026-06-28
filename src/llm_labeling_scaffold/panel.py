from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from . import __version__
from .io import read_json, read_jsonl, write_jsonl
from . import pipeline
from . import panel_settings

API_CONTRACT_VERSION = "2026-06-28"

POOL_FILES = {
    "merged": ("merged", "merged_clean.jsonl"),
    "missing": ("merged", "missing_pool.jsonl"),
    "duplicate": ("merged", "duplicate_pool.jsonl"),
    "conflict": ("merged", "conflict_pool.jsonl"),
}

_TASK_REGISTRY_SYNC_CACHE: dict[tuple[str, str, str, str], tuple[float, object]] = {}
_TASK_REGISTRY_SYNC_CACHE_LOCK = threading.Lock()
_DEFAULT_TASK_REGISTRY_SYNC_TTL_SECONDS = 5.0


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _valid_idempotency_key(value: str) -> bool:
    return bool(value) and len(value) <= 200 and not any(ord(ch) < 32 for ch in value)


def _allow_data_lake_overrides() -> bool:
    return panel_settings.allow_data_lake_overrides()


def _allow_manual_imports() -> bool:
    return panel_settings.allow_manual_imports()


def _task_source_mode() -> str:
    return panel_settings.task_source_mode()


def _r2_task_source_enabled() -> bool:
    return _task_source_mode() == "r2"


def _task_registry_sync_ttl_seconds() -> float:
    raw = os.environ.get("LLS_TASK_REGISTRY_SYNC_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_TASK_REGISTRY_SYNC_TTL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_TASK_REGISTRY_SYNC_TTL_SECONDS


def _task_registry_sync_cache_key(tasks_root: str | Path, settings: dict) -> tuple[str, str, str, str]:
    return (
        str(Path(tasks_root).expanduser().resolve()),
        str(settings.get("task_registry_uri") or ""),
        str(settings.get("data_lake_r2_prefix") or ""),
        str(settings.get("rclone_config_path") or ""),
    )


def _invalidate_task_registry_sync_cache() -> None:
    with _TASK_REGISTRY_SYNC_CACHE_LOCK:
        _TASK_REGISTRY_SYNC_CACHE.clear()


def _apply_runtime_settings(runs_root: str | Path, settings: dict | None = None) -> dict:
    effective = settings or panel_settings.effective_settings(runs_root)
    from .data_lake import set_allowed_r2_prefix_override, set_default_registry_uri_override

    set_default_registry_uri_override(effective["task_registry_uri"])
    set_allowed_r2_prefix_override(effective["data_lake_r2_prefix"])
    return effective


def _settings_response(settings: dict, *, include_legacy: bool = False) -> dict:
    payload = {"settings": settings}
    if include_legacy:
        payload.update({
            "allow_data_lake_overrides": settings["allow_data_lake_overrides"],
            "task_source": settings["task_source"],
            "task_registry_uri": settings["task_registry_uri"] if settings["task_source"] == "r2" else None,
        })
    return payload


def _public_settings_response(settings: dict) -> dict:
    return {
        "settings": {
            "task_source": settings["task_source"],
            "allow_manual_imports": settings["allow_manual_imports"],
            "allow_data_lake_overrides": settings["allow_data_lake_overrides"],
            "task_registry_configured": bool(settings["task_registry_uri"]),
            "data_lake_r2_prefix_configured": bool(settings["data_lake_r2_prefix"]),
            "rclone_configured": bool(settings["rclone_config_path"]),
        }
    }


def _contract_capabilities() -> dict[str, Any]:
    return {
        "service": "llm-labeling-scaffold",
        "api_contract_version": API_CONTRACT_VERSION,
        "auth": {"type": "basic"},
        "endpoints": [
            {
                "method": "GET",
                "path": "/api/health",
                "action": "health",
                "side_effects": False,
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "status", "service"],
                },
            },
            {
                "method": "GET",
                "path": "/api/version",
                "action": "version",
                "side_effects": False,
                "response_schema": {
                    "type": "object",
                    "required": ["service", "version", "api_contract_version"],
                },
            },
            {
                "method": "GET",
                "path": "/api/capabilities",
                "action": "capabilities",
                "side_effects": False,
                "response_schema": {"type": "object", "required": ["endpoints"]},
            },
            {
                "method": "GET",
                "path": "/api/settings/public",
                "action": "settings_public",
                "side_effects": False,
                "response_schema": {
                    "type": "object",
                    "required": ["settings"],
                    "properties": {
                        "settings": {
                            "type": "object",
                            "required": [
                                "task_source",
                                "allow_manual_imports",
                                "allow_data_lake_overrides",
                                "task_registry_configured",
                                "data_lake_r2_prefix_configured",
                                "rclone_configured",
                            ],
                        }
                    },
                },
            },
            {
                "method": "GET",
                "path": "/api/tasks/{task_id}",
                "action": "task_detail",
                "side_effects": False,
                "path_params": {"task_id": {"type": "string"}},
                "response_schema": {
                    "type": "object",
                    "required": ["task"],
                    "properties": {"task": {"type": "object", "required": ["task_id", "profile", "data_lake"]}},
                },
            },
            {
                "method": "POST",
                "path": "/api/tasks/{task_id}/check",
                "action": "task_check",
                "side_effects": False,
                "path_params": {"task_id": {"type": "string"}},
                "request_schema": {"type": "object", "additionalProperties": False},
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "task_id", "checks", "warnings", "errors"],
                },
            },
            {
                "method": "GET",
                "path": "/api/task/annotation_jobs",
                "action": "annotation_jobs_list",
                "side_effects": False,
                "query_params": {"task_id": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["annotation_jobs"]},
            },
            {
                "method": "GET",
                "path": "/api/annotation_job/detail",
                "action": "annotation_job_detail",
                "side_effects": False,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "annotation_id": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["annotation_job"]},
            },
            {
                "method": "GET",
                "path": "/api/task/decision_artifacts",
                "action": "decision_artifacts_list",
                "side_effects": False,
                "query_params": {"task_id": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["decision_artifacts"]},
            },
            {
                "method": "GET",
                "path": "/api/decision_artifact/detail",
                "action": "decision_artifact_detail",
                "side_effects": False,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "decision_id": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["decision_artifact"]},
            },
            {
                "method": "GET",
                "path": "/api/task/gold_versions",
                "action": "gold_versions_list",
                "side_effects": False,
                "query_params": {"task_id": {"type": "string", "required": True}},
                "response_schema": {"type": "object", "required": ["gold_versions"]},
            },
            {
                "method": "GET",
                "path": "/api/gold_version/detail",
                "action": "gold_version_detail",
                "side_effects": False,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "version": {"type": "string", "required": True},
                },
                "response_schema": {"type": "object", "required": ["gold_version"]},
            },
            {
                "method": "POST",
                "path": "/api/action",
                "action": "operator_action",
                "side_effects": True,
                "operator_gated": True,
                "allowed_actions": [
                    "argilla_push",
                    "argilla_pull",
                    "gold",
                    "prelabel_export",
                    "prelabel_publish",
                ],
                "request_schema": {
                    "type": "object",
                    "required": ["task", "action"],
                    "properties": {
                        "task": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": [
                                "argilla_push",
                                "argilla_pull",
                                "gold",
                                "prelabel_export",
                                "prelabel_publish",
                            ],
                        },
                        "params": {"type": "object"},
                    },
                },
            },
            {
                "method": "POST",
                "path": "/api/suggestions/import",
                "action": "suggestions_import",
                "side_effects": True,
                "operator_gated": True,
                "query_params": {
                    "task_id": {"type": "string", "required": True},
                    "annotation_id": {"type": "string", "required": True},
                    "suggestion_id": {"type": "string", "required": True},
                    "provider": {"type": "string", "required": False},
                    "prompt_version": {"type": "string", "required": False},
                    "publish": {"type": "boolean", "required": False},
                },
                "request_schema": {"content_type": "application/x-ndjson"},
                "response_schema": {"type": "object", "required": ["ok", "suggestions"]},
            },
            {
                "method": "POST",
                "path": "/api/import/data_lake",
                "action": "data_lake_import_dry_run",
                "side_effects": False,
                "request_schema": {
                    "type": "object",
                    "required": ["task_id", "dry_run"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "import_id": {"type": "string"},
                        "dry_run": {"const": True},
                    },
                    "additionalProperties": True,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "dry_run", "result"],
                    "properties": {
                        "ok": {"type": "boolean"},
                        "dry_run": {"const": True},
                        "result": {"type": "object"},
                    },
                },
            },
            {
                "method": "POST",
                "path": "/api/import/data_lake",
                "action": "data_lake_import_submit",
                "side_effects": True,
                "request_schema": {
                    "type": "object",
                    "required": ["task_id", "confirm", "idempotency_key"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "import_id": {"type": "string"},
                        "confirm": {"const": True},
                        "idempotency_key": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "response_schema": {
                    "type": "object",
                    "required": ["ok", "job"],
                },
            },
        ],
    }


def _contract_task_path(path: str, *, suffix: str = "") -> str | None:
    prefix = "/api/tasks/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if suffix:
        marker = f"/{suffix}"
        if not rest.endswith(marker):
            return None
        rest = rest[: -len(marker)]
    elif "/" in rest:
        return None
    task_id = rest.strip()
    return task_id if _safe_segment(task_id) else None


def _safe_data_lake_summary(data_lake: dict[str, Any]) -> dict[str, Any]:
    return {
        "configured": bool(data_lake),
        "source_dataset_id": data_lake.get("source_dataset_id"),
        "source_object_path": data_lake.get("source_object_path"),
        "lake_registry_uri_configured": bool(data_lake.get("lake_registry_uri")),
        "source_manifest_uri_configured": bool(data_lake.get("source_manifest_uri")),
        "output_base_uri_configured": bool(data_lake.get("output_base_uri")),
    }


def _task_detail_payload(task) -> dict[str, Any]:
    try:
        from .profiles import profile_definition

        profile = profile_definition(task.profile)
        profile_summary = {
            "id": task.profile,
            "valid": True,
            "name": profile.get("name"),
            "stage_count": len(profile.get("stages", [])),
        }
    except Exception as exc:  # noqa: BLE001
        profile_summary = {"id": task.profile, "valid": False, "error": str(exc)}
    return {
        "task": {
            "task_id": task.task_id,
            "path": str(task.path),
            "profile": profile_summary,
            "id_field": task.id_field,
            "text_fields": task.text_fields,
            "metadata_fields": task.metadata_fields,
            "primary_label": task.primary_label,
            "auxiliary_labels": task.auxiliary_labels,
            "data_lake": _safe_data_lake_summary(task.data_lake),
        }
    }


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def discover_runs(runs_root: Path) -> list[dict]:
    out: list[dict] = []
    if not runs_root.exists():
        return out
    for task_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        for run_dir in sorted(p for p in task_dir.iterdir() if p.is_dir()):
            audit = run_dir / "audit" / "run_summary.json"
            merged = run_dir / "merged" / "merge_summary.json"
            if not audit.exists() and not merged.exists():
                continue
            out.append({
                "task_id": task_dir.name,
                "run_id": run_dir.name,
                "path": str(run_dir),
                "audit": read_json(audit) if audit.exists() else None,
                "merge": read_json(merged) if merged.exists() else None,
            })
    return out


def run_detail(run_dir: Path) -> dict:
    audit_dir = run_dir / "audit"
    batches = []
    if audit_dir.is_dir():
        for sp in sorted(audit_dir.glob("batch_*.summary.json")):
            batches.append(read_json(sp))
    merge = run_dir / "merged" / "merge_summary.json"
    return {
        "batches": batches,
        "merge": read_json(merge) if merge.exists() else None,
        "pools": {k: _count_lines(run_dir / d / f) for k, (d, f) in POOL_FILES.items()},
        "has_decisions": (run_dir / "adjudication" / "decisions.jsonl").exists(),
    }


def pool_rows(run_dir: Path, kind: str, limit: int = 200) -> list[dict]:
    if kind not in POOL_FILES:
        return []
    d, f = POOL_FILES[kind]
    path = run_dir / d / f
    if not path.exists():
        return []
    return read_jsonl(path)[:limit]


def append_decision(run_dir: Path, decision: dict) -> int:
    out = run_dir / "adjudication" / "decisions.jsonl"
    existing = read_jsonl(out) if out.exists() else []
    existing.append(decision)
    write_jsonl(existing, out)
    return len(existing)


def parse_import_rows(text: str) -> list[dict]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("上传内容为空")
    if stripped[0] in "[{":
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            if not all(isinstance(row, dict) for row in data):
                raise ValueError("JSON 数组中的每一项都必须是对象")
            if not data:
                raise ValueError("上传内容为空")
            return data

    rows: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_no} 行不是合法 JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"第 {line_no} 行必须是 JSON 对象")
        rows.append(row)
    if not rows:
        raise ValueError("上传内容为空")
    return rows


def list_gold(runs_root: Path, task: str) -> list[dict]:
    gold_dir = runs_root / task / "gold"
    out = []
    if not gold_dir.exists():
        return out
    for mp in sorted(gold_dir.glob("gold_*.manifest.json")):
        out.append(read_json(mp))
    return out


# Backwards-compatible aliases used by tests.
_discover_runs = discover_runs


def _sample_rows(run_dir: Path, limit: int = 50) -> list[dict]:
    return pool_rows(run_dir, "merged", limit)


class _Handler(BaseHTTPRequestHandler):
    server_version = "LLSPanel/0.2"
    runs_root: Path = Path("runs")
    tasks_root: Path = Path("tasks")
    static_dir: Path | None = None
    auth_user: str = "admin"
    auth_pass: str = ""

    def log_message(self, *args) -> None:
        pass

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        user, _, pw = raw.partition(":")
        return hmac.compare_digest(user, self.auth_user) and hmac.compare_digest(pw, self.auth_pass)

    def _require_auth(self) -> bool:
        if self._authed():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="lls-panel"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"authentication required\n")
        return False

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _resolve_run(self, params) -> Path | None:
        task = params.get("task", [""])[0]
        run = params.get("run", [""])[0]
        if not _safe_segment(task) or not _safe_segment(run):
            return None
        run_dir = self.runs_root / task / run
        return run_dir if run_dir.is_dir() else None

    def _sync_tasks_if_needed(self, *, force: bool = False):
        if not _r2_task_source_enabled():
            return None
        from .task_registry import sync_tasks_from_registry

        settings = _apply_runtime_settings(self.runs_root)
        ttl_seconds = _task_registry_sync_ttl_seconds()
        cache_key = _task_registry_sync_cache_key(self.tasks_root, settings)
        now = time.monotonic()

        with _TASK_REGISTRY_SYNC_CACHE_LOCK:
            cached = _TASK_REGISTRY_SYNC_CACHE.get(cache_key)
            if not force and cached is not None and ttl_seconds > 0 and now - cached[0] < ttl_seconds:
                return cached[1]

            synced = sync_tasks_from_registry(self.tasks_root, registry_uri=settings["task_registry_uri"])
            if ttl_seconds > 0:
                _TASK_REGISTRY_SYNC_CACHE[cache_key] = (time.monotonic(), synced)
            return synced

    def _list_tasks(self) -> list[dict]:
        tasks = pipeline.list_tasks(self.tasks_root)
        if not _r2_task_source_enabled():
            return tasks
        out: list[dict] = []
        for task in tasks:
            task_id = task.get("task_id")
            if not task_id:
                continue
            item = dict(task)
            item["source"] = "R2 数据湖"
            item["deletable"] = False
            out.append(item)
        return out

    def _load_task_by_id(self, task_id: str):
        return pipeline.load_task_by_id(self.tasks_root, task_id)

    def _resolve_action_task_path(self, task_path: str) -> str:
        if not _r2_task_source_enabled():
            return task_path
        try:
            submitted = Path(task_path).expanduser().resolve()
        except Exception as exc:
            raise ValueError(f"任务路径无效: {task_path}") from exc
        for task in pipeline.list_tasks(self.tasks_root):
            local_path = task.get("path")
            if not local_path:
                continue
            try:
                resolved = Path(local_path).expanduser().resolve()
            except Exception:
                continue
            if submitted == resolved:
                return str(resolved)
        raise ValueError("生产模式只能执行已同步到本地缓存的 R2 任务配置；请先同步任务配置")

    def _sync_tasks_from_registry(self) -> None:
        if not _r2_task_source_enabled():
            self._json({"error": "当前不是 R2 任务来源模式，不需要同步任务配置"}, status=400)
            return
        try:
            synced = self._sync_tasks_if_needed(force=True)
            tasks = self._list_tasks()
            self._json({
                "ok": True,
                "sync": {
                    "registry_uri": synced.registry_uri if synced is not None else None,
                    "tasks": synced.tasks if synced is not None else {},
                },
                "tasks": tasks,
            })
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _task_detail(self, task_id: str) -> None:
        if not _safe_segment(task_id):
            self._json({"error": "bad task"}, status=400)
            return
        try:
            self._json(_task_detail_payload(self._load_task_by_id(task_id)))
        except Exception as exc:
            self._json({"error": str(exc)}, status=404)

    def _task_check(self, task_id: str) -> None:
        if not _safe_segment(task_id):
            self._json({"ok": False, "task_id": task_id, "checks": [], "warnings": [], "errors": [
                {"check": "task_id", "message": "bad task"}
            ]}, status=400)
            return

        checks: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def add_check(name: str, status: str, message: str, details: dict[str, Any] | None = None) -> None:
            item = {"name": name, "status": status, "message": message}
            if details:
                item["details"] = details
            checks.append(item)
            if status == "warning":
                warnings.append({"check": name, "message": message, "details": details or {}})
            elif status == "error":
                errors.append({"check": name, "message": message, "details": details or {}})

        try:
            task = self._load_task_by_id(task_id)
            add_check("task_load", "ok", "task loaded", {"path": str(task.path)})
        except Exception as exc:
            add_check("task_load", "error", str(exc), {"error_class": exc.__class__.__name__})
            self._json({
                "ok": False,
                "task_id": task_id,
                "checks": checks,
                "warnings": warnings,
                "errors": errors,
            }, status=404)
            return

        try:
            from .profiles import profile_definition

            profile = profile_definition(task.profile)
            add_check("profile", "ok", "profile is valid", {
                "profile_id": task.profile,
                "stage_count": len(profile.get("stages", [])),
            })
        except Exception as exc:  # noqa: BLE001
            add_check("profile", "error", str(exc), {
                "profile_id": task.profile,
                "error_class": exc.__class__.__name__,
            })

        if not task.data_lake:
            add_check("data_lake_config", "error", "task has no data_lake configuration")
        else:
            add_check("data_lake_config", "ok", "task has data_lake configuration", _safe_data_lake_summary(task.data_lake))
            try:
                from .data_lake import preview_source

                _apply_runtime_settings(self.runs_root)
                preview = preview_source(task)
                add_check("data_lake_preview", "ok", "data_lake source can be previewed", {"preview": preview})
            except Exception as exc:  # noqa: BLE001
                add_check("data_lake_preview", "error", str(exc), {"error_class": exc.__class__.__name__})

        self._json({
            "ok": not errors,
            "task_id": task_id,
            "checks": checks,
            "warnings": warnings,
            "errors": errors,
        })

    def _serve_static(self, path: str) -> bool:
        if not self.static_dir:
            return False
        rel = path.lstrip("/") or "index.html"
        target = (self.static_dir / rel).resolve()
        try:
            target.relative_to(self.static_dir.resolve())
        except ValueError:
            return False
        if not target.is_file():
            target = self.static_dir / "index.html"
            if not target.is_file():
                return False
        ctype = "text/html; charset=utf-8"
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif target.suffix == ".json":
            ctype = "application/json; charset=utf-8"
        elif target.suffix == ".svg":
            ctype = "image/svg+xml"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        contract_task_id = _contract_task_path(path)
        if path == "/api/health":
            self._json({"ok": True, "status": "ok", "service": "llm-labeling-scaffold"})
        elif path == "/api/version":
            self._json({
                "service": "llm-labeling-scaffold",
                "version": __version__,
                "api_contract_version": API_CONTRACT_VERSION,
            })
        elif path == "/api/capabilities":
            self._json(_contract_capabilities())
        elif path == "/api/settings/public":
            try:
                settings = _apply_runtime_settings(self.runs_root)
                self._json(_public_settings_response(settings))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif contract_task_id is not None:
            self._task_detail(contract_task_id)
        elif path.startswith("/api/tasks/"):
            self._json({"error": "not found"}, status=404)
        elif path == "/api/runs":
            self._json({"runs": discover_runs(self.runs_root)})
        elif path == "/api/run":
            run_dir = self._resolve_run(params)
            if not run_dir:
                self._json({"error": "unknown run"}, status=404)
                return
            self._json(run_detail(run_dir))
        elif path == "/api/rows":
            run_dir = self._resolve_run(params)
            if not run_dir:
                self._json({"error": "unknown run"}, status=404)
                return
            kind = params.get("kind", ["merged"])[0]
            self._json({"kind": kind, "rows": pool_rows(run_dir, kind)})
        elif path == "/api/gold":
            task = params.get("task", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"gold": list_gold(self.runs_root, task)})
        elif path == "/api/tasks":
            try:
                self._json({"tasks": self._list_tasks()})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/profile":
            task = params.get("task_id", [""])[0]
            preset = params.get("preset", [""])[0].strip() or None
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg = self._load_task_by_id(task)
                self._json(pipeline.task_profile_status(self.runs_root, task_cfg, profile_id=preset))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/graph":
            task = params.get("task_id", [""])[0]
            preset = params.get("preset", [""])[0].strip() or None
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg = self._load_task_by_id(task)
                self._json(pipeline.task_asset_graph(self.runs_root, task_cfg, profile_id=preset))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/archive_plan":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg = self._load_task_by_id(task)
                self._json(pipeline.task_archive_plan(
                    self.tasks_root,
                    self.runs_root,
                    task_cfg,
                    r2_task_source=_r2_task_source_enabled(),
                ))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/profile/presets":
            try:
                from .profiles import DEFAULT_PROFILE, list_profile_presets

                self._json({"default_profile_id": DEFAULT_PROFILE, "presets": list_profile_presets()})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/settings":
            try:
                settings = _apply_runtime_settings(self.runs_root)
                self._json(_settings_response(settings))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/config":
            try:
                settings = _apply_runtime_settings(self.runs_root)
                self._json(_settings_response(settings, include_legacy=True))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/runs":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"runs": pipeline.list_runs(self.runs_root, task)})
        elif path == "/api/task/samples":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"samples": pipeline.list_samples(self.runs_root, task)})
        elif path == "/api/task/imports":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                task_cfg = self._load_task_by_id(task)
                self._json({"imports": pipeline.list_imports(self.runs_root, task, id_field=task_cfg.id_field)})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import/detail":
            task = params.get("task_id", [""])[0]
            import_id = params.get("import_id", [""])[0]
            try:
                task_cfg = self._load_task_by_id(task)
                self._json({"import": pipeline.import_detail(self.runs_root, task, import_id, id_field=task_cfg.id_field)})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import/rows":
            task = params.get("task_id", [""])[0]
            import_id = params.get("import_id", [""])[0]
            try:
                self._json(pipeline.import_rows(
                    self.runs_root,
                    task,
                    import_id,
                    offset=int(params.get("offset", ["0"])[0] or 0),
                    limit=int(params.get("limit", ["50"])[0] or 50),
                    query=params.get("q", [""])[0],
                ))
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import/download":
            self._download_import(params)
        elif path == "/api/task/annotation_jobs":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"annotation_jobs": pipeline.list_annotation_jobs(self.runs_root, task)})
        elif path == "/api/annotation_job/detail":
            task = params.get("task_id", [""])[0]
            annotation_id = params.get("annotation_id", [""])[0]
            if not _safe_segment(task) or not _safe_segment(annotation_id):
                self._json({"error": "bad task/annotation_id"}, status=400)
                return
            try:
                self._json({"annotation_job": pipeline.annotation_job_detail(self.runs_root, task, annotation_id)})
            except ValueError as exc:
                self._json({"error": str(exc), "found": False}, status=404)
        elif path == "/api/task/agreement_audits":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"agreement_audits": pipeline.list_agreement_audits(self.runs_root, task)})
        elif path == "/api/task/models":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"models": pipeline.list_models(self.runs_root, task)})
        elif path == "/api/task/gold_versions":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"gold_versions": pipeline.list_gold_versions(self.runs_root, task)})
        elif path == "/api/gold_version/detail":
            task = params.get("task_id", [""])[0]
            version = params.get("version", [""])[0]
            if not _safe_segment(task) or not _safe_segment(version):
                self._json({"error": "bad task/version"}, status=400)
                return
            try:
                self._json({"gold_version": pipeline.gold_version_detail(self.runs_root, task, version)})
            except ValueError as exc:
                self._json({"error": str(exc), "found": False}, status=404)
        elif path == "/api/task/decision_artifacts":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"decision_artifacts": pipeline.list_decision_artifacts(self.runs_root, task)})
        elif path == "/api/decision_artifact/detail":
            task = params.get("task_id", [""])[0]
            decision_id = params.get("decision_id", [""])[0]
            if not _safe_segment(task) or not _safe_segment(decision_id):
                self._json({"error": "bad task/decision_id"}, status=400)
                return
            try:
                self._json({"decision_artifact": pipeline.decision_artifact_detail(self.runs_root, task, decision_id)})
            except ValueError as exc:
                self._json({"error": str(exc), "found": False}, status=404)
        elif path == "/api/task/decisions":
            task = params.get("task_id", [""])[0]
            run = params.get("run", [""])[0]
            if not _safe_segment(task) or not _safe_segment(run):
                self._json({"error": "bad task/run"}, status=400)
                return
            self._json({"decisions": pipeline.list_decisions(self.runs_root, task, run)})
        elif path == "/api/jobs":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"jobs": pipeline.jobs_for_task(self.runs_root, task)})
        elif path == "/api/task/audit":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"events": pipeline.list_audit_events(self.runs_root, task)})
        elif path == "/api/task/data_lake":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                from .data_lake import preview_source

                _apply_runtime_settings(self.runs_root)
                task_cfg = self._load_task_by_id(task)
                self._json({
                    "enabled": bool(task_cfg.data_lake),
                    "data_lake": task_cfg.data_lake,
                    "preview": preview_source(task_cfg) if task_cfg.data_lake else None,
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/argilla/status":
            try:
                from .integrations.argilla import test_connection

                self._json(test_connection({
                    "workspace": params.get("workspace", [""])[0],
                }))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=400)
        elif path == "/api/export":
            self._export(params)
        elif path.startswith("/api/"):
            self._json({"error": "not found"}, status=404)
        else:
            if not self._serve_static(path):
                self._json({"error": "not found"}, status=404)

    def _export(self, params) -> None:
        run_dir = self._resolve_run(params)
        kind = params.get("kind", ["merged"])[0]
        if not run_dir or kind not in POOL_FILES:
            self._json({"error": "unknown export"}, status=404)
            return
        d, f = POOL_FILES[kind]
        path = run_dir / d / f
        if not path.exists():
            self._json({"error": "no data"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{kind}.jsonl"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        contract_check_task_id = _contract_task_path(path, suffix="check")
        if contract_check_task_id is not None:
            self._task_check(contract_check_task_id)
        elif path == "/api/tasks/sync":
            self._sync_tasks_from_registry()
        elif path.startswith("/api/tasks/"):
            self._json({"error": "not found"}, status=404)
        elif path == "/api/adjudicate":
            run_dir = self._resolve_run(params)
            if not run_dir:
                self._json({"error": "unknown run"}, status=404)
                return
            body = self._read_body()
            rid = body.get("id")
            human_label = body.get("human_label")
            if not rid or not isinstance(human_label, dict):
                self._json({"error": "id and human_label required"}, status=400)
                return
            id_field = body.get("id_field", "record_id")
            count = append_decision(run_dir, {
                id_field: rid,
                "human_label": human_label,
                "note": body.get("note", ""),
            })
            self._json({"ok": True, "decisions": count})
        elif path == "/api/action":
            body = self._read_body()
            task_path = body.get("task")
            action = body.get("action")
            if not task_path or not action:
                self._json({"error": "task and action required"}, status=400)
                return
            try:
                job = pipeline.start_action(self.runs_root, self._resolve_action_task_path(task_path), action, body.get("params", {}))
                self._json({"ok": True, "job": job})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/suggestions/import":
            self._import_suggestions(params)
        elif path == "/api/tasks":
            body = self._read_body()
            try:
                if _r2_task_source_enabled():
                    self._json({"error": "生产模式任务必须从 R2 数据湖登记表拉取，不能在面板中新建本地任务"}, status=400)
                    return
                if not _allow_data_lake_overrides() and "data_lake" in body:
                    body = dict(body)
                    body.pop("data_lake", None)
                task = pipeline.create_task(self.tasks_root, body)
                self._json({"ok": True, "task": task})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import":
            self._import(params)
        elif path == "/api/import/data_lake":
            self._import_data_lake(params)
        elif path == "/api/task/archive":
            body = self._read_body()
            task_id = str(body.get("task_id") or params.get("task_id", [""])[0])
            reason = str(body.get("reason") or "")
            if not _safe_segment(task_id):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                result = pipeline.execute_task_archive(
                    self.tasks_root,
                    self.runs_root,
                    task_id,
                    reason=reason,
                    actor="panel",
                    r2_task_source=_r2_task_source_enabled(),
                )
                self._json({"ok": True, "archive": result})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/task/cache_cleanup":
            body = self._read_body()
            task_id = str(body.get("task_id") or params.get("task_id", [""])[0])
            if not _safe_segment(task_id):
                self._json({"error": "bad task"}, status=400)
                return
            try:
                result = pipeline.execute_task_cache_cleanup(self.runs_root, task_id, actor="panel")
                self._json({"ok": result.get("ok", False), "cleanup": result})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/settings":
            body = self._read_body()
            if not isinstance(body, dict):
                self._json({"error": "settings payload must be a JSON object"}, status=400)
                return
            try:
                settings = panel_settings.update_settings(self.runs_root, body)
                _invalidate_task_registry_sync_cache()
                _apply_runtime_settings(self.runs_root, settings)
                self._json({"ok": True, **_settings_response(settings)})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        else:
            self._json({"error": "not found"}, status=404)

    def do_DELETE(self) -> None:
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/api/tasks":
            task_id = params.get("task_id", [""])[0]
            delete_runs = params.get("delete_runs", ["0"])[0] in {"1", "true", "yes"}
            try:
                if _r2_task_source_enabled():
                    self._json({"error": "生产模式任务来自 R2 数据湖登记表，不能在面板中归档本地缓存；请在登记表中标记为非启用状态"}, status=400)
                    return
                self._json({
                    "ok": True,
                    "task": pipeline.delete_task(
                        self.tasks_root,
                        task_id,
                        runs_root=self.runs_root,
                        delete_runs=delete_runs,
                    ),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import":
            task_id = params.get("task_id", [""])[0]
            import_id = params.get("import_id", [""])[0]
            reason = params.get("reason", [""])[0]
            try:
                self._json({
                    "ok": True,
                    "import": pipeline.archive_import(self.runs_root, task_id, import_id, reason=reason),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/sample":
            task_id = params.get("task_id", [""])[0]
            sample_id = params.get("sample_id", [""])[0]
            reason = params.get("reason", [""])[0]
            try:
                self._json({
                    "ok": True,
                    "sample": pipeline.archive_sample(self.runs_root, task_id, sample_id, reason=reason),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/annotation_job":
            task_id = params.get("task_id", [""])[0]
            annotation_id = params.get("annotation_id", [""])[0]
            reason = params.get("reason", [""])[0]
            try:
                self._json({
                    "ok": True,
                    "annotation_job": pipeline.archive_annotation_job(self.runs_root, task_id, annotation_id, reason=reason),
                })
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        else:
            self._json({"error": "not found"}, status=404)

    def _import(self, params) -> None:
        task = params.get("task_id", params.get("task", [""]))[0]
        name = params.get("name", ["imported"])[0]
        if not _safe_segment(task) or not _safe_segment(name):
            self._json({"error": "bad task/name"}, status=400)
            return
        if not _allow_manual_imports():
            self._json({"error": "生产模式不允许手动上传或粘贴导入；请使用 task.yaml 中的 data_lake 配置从数据湖导入"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        max_bytes = int(os.environ.get("LLS_MAX_IMPORT_BYTES", str(100 * 1024 * 1024)))
        if length <= 0:
            self._json({"error": "上传内容为空"}, status=400)
            return
        if length > max_bytes:
            self._json({"error": f"上传内容过大，当前上限为 {max_bytes} bytes"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            rows = parse_import_rows(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return
        try:
            task_cfg = self._load_task_by_id(task)
            result = pipeline.save_import(self.runs_root, task_cfg, name, rows, source="upload")
            self._json({"ok": True, "import": result})
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _import_data_lake(self, params) -> None:
        body = self._read_body()
        if not isinstance(body, dict):
            body = {}
        task_id = str(body.get("task_id") or params.get("task_id", [""])[0])
        if not _safe_segment(task_id):
            self._json({"error": "bad task"}, status=400)
            return
        import_id = str(body.get("import_id") or params.get("import_id", [""])[0] or "").strip()
        dry_run_value = body.get("dry_run", body.get("dryRun", params.get("dry_run", [""])[0]))
        dry_run = _truthy_value(dry_run_value)
        override_keys = (
            "lake_registry_uri",
            "source_dataset_id",
            "source_manifest_uri",
            "source_object_path",
        )
        provided_overrides = {key: body.get(key) for key in override_keys if body.get(key) not in (None, "")}
        if provided_overrides and not _allow_data_lake_overrides():
            self._json({"error": "生产模式不允许覆盖数据湖来源；请在 task.yaml 中使用治理登记表配置"}, status=400)
            return
        overrides = provided_overrides if _allow_data_lake_overrides() else {}
        idempotency_key = str(body.get("idempotency_key") or self.headers.get("Idempotency-Key") or "").strip()
        if not dry_run:
            if not _truthy_value(body.get("confirm")):
                self._json({"error": "数据湖导入提交必须显式设置 confirm=true"}, status=400)
                return
            if not _valid_idempotency_key(idempotency_key):
                self._json({"error": "数据湖导入提交必须提供有效的 idempotency_key 或 Idempotency-Key header"}, status=400)
                return
        max_bytes = int(os.environ.get("LLS_MAX_IMPORT_BYTES", str(100 * 1024 * 1024)))
        try:
            _apply_runtime_settings(self.runs_root)
            task_cfg = self._load_task_by_id(task_id)
            if dry_run:
                dry_run_result = pipeline.dry_run_data_lake_import(
                    self.runs_root,
                    task_cfg,
                    import_id=import_id or None,
                    overrides=overrides,
                    max_bytes=max_bytes,
                )
                self._json({"ok": bool(dry_run_result["validation"]["ok"]), "dry_run": True, "result": dry_run_result})
                return
            job = pipeline.start_data_lake_import(
                self.runs_root,
                task_cfg,
                import_id=import_id or None,
                overrides=overrides,
                max_bytes=max_bytes,
                idempotency_key=idempotency_key,
            )
            self._json({"ok": True, "job": job})
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _import_suggestions(self, params) -> None:
        task_id = params.get("task_id", [""])[0]
        annotation_id = params.get("annotation_id", [""])[0]
        suggestion_id = params.get("suggestion_id", [""])[0]
        provider = params.get("provider", ["external"])[0] or "external"
        prompt_version = params.get("prompt_version", ["v001"])[0] or "v001"
        publish = _truthy_value(params.get("publish", [""])[0])
        if not _safe_segment(task_id) or not _safe_segment(annotation_id) or not _safe_segment(suggestion_id):
            self._json({"error": "bad task/annotation/suggestion"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        max_bytes = int(os.environ.get("LLS_MAX_SUGGESTIONS_BYTES", str(50 * 1024 * 1024)))
        if length <= 0:
            self._json({"error": "上传内容为空"}, status=400)
            return
        if length > max_bytes:
            self._json({"error": f"上传内容过大，当前上限为 {max_bytes} bytes"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        raw = self.rfile.read(length)
        try:
            rows = parse_import_rows(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return
        try:
            from .suggestions import import_external_suggestions

            task_cfg = self._load_task_by_id(task_id)
            result = import_external_suggestions(
                self.runs_root,
                task_cfg,
                annotation_id,
                suggestion_id,
                rows,
                provider=provider,
                prompt_version=prompt_version,
                publish=publish,
            )
            self._json({"ok": True, "suggestions": result})
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _download_import(self, params) -> None:
        task = params.get("task_id", [""])[0]
        import_id = params.get("import_id", [""])[0]
        if not _safe_segment(task) or not _safe_segment(import_id):
            self._json({"error": "bad task/import"}, status=400)
            return
        path = self.runs_root / task / "imports" / import_id / "raw.jsonl"
        if not path.exists():
            self._json({"error": "no data"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{import_id}.jsonl"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_panel(runs_root: str | Path = "runs", host: str = "127.0.0.1",
                port: int = 8765, user: str = "admin", password: str | None = None,
                static_dir: str | Path | None = None,
                tasks_root: str | Path = "tasks") -> None:
    password = password or os.environ.get("LLS_PANEL_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(12)
        print(f"[lls panel] generated password for user '{user}': {password}")
    _Handler.runs_root = Path(runs_root)
    _Handler.tasks_root = Path(tasks_root)
    _Handler.auth_user = user
    _Handler.auth_pass = password
    if static_dir is None:
        guess = Path("frontend/dist")
        static_dir = guess if guess.is_dir() else None
    _Handler.static_dir = Path(static_dir) if static_dir else None
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"[lls panel] serving on http://{host}:{port} (basic auth user='{user}')")
    if _Handler.static_dir:
        print(f"[lls panel] serving frontend from {_Handler.static_dir}")
    else:
        print("[lls panel] no frontend build found; run 'npm run build' in frontend/ or use the Vite dev server")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
