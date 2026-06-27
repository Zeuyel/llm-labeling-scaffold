from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .config import load_task
from .io import read_json, read_jsonl, write_jsonl
from . import pipeline
from . import panel_settings

POOL_FILES = {
    "merged": ("merged", "merged_clean.jsonl"),
    "missing": ("merged", "missing_pool.jsonl"),
    "duplicate": ("merged", "duplicate_pool.jsonl"),
    "conflict": ("merged", "conflict_pool.jsonl"),
}


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _truthy_env(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _allow_data_lake_overrides() -> bool:
    return panel_settings.allow_data_lake_overrides()


def _allow_manual_imports() -> bool:
    return panel_settings.allow_manual_imports()


def _task_source_mode() -> str:
    return panel_settings.task_source_mode()


def _r2_task_source_enabled() -> bool:
    return _task_source_mode() == "r2"


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

    def _sync_tasks_if_needed(self):
        if not _r2_task_source_enabled():
            return None
        from .task_registry import sync_tasks_from_registry

        settings = _apply_runtime_settings(self.runs_root)
        return sync_tasks_from_registry(self.tasks_root, registry_uri=settings["task_registry_uri"])

    def _list_tasks(self) -> list[dict]:
        synced = self._sync_tasks_if_needed()
        tasks = pipeline.list_tasks(self.tasks_root)
        if synced is None:
            return tasks
        active = synced.tasks
        out: list[dict] = []
        for task in tasks:
            task_id = task.get("task_id")
            if task_id not in active:
                continue
            item = dict(task)
            item["source"] = "R2 数据湖"
            item["deletable"] = False
            item["task_uri"] = active[task_id]["task_uri"]
            item["registry_uri"] = synced.registry_uri
            out.append(item)
        return out

    def _load_task_by_id(self, task_id: str):
        synced = self._sync_tasks_if_needed()
        if synced is not None and task_id not in synced.tasks:
            raise ValueError(f"任务未在 R2 数据湖登记表中登记为启用状态: {task_id}")
        return pipeline.load_task_by_id(self.tasks_root, task_id)

    def _resolve_action_task_path(self, task_path: str) -> str:
        synced = self._sync_tasks_if_needed()
        if synced is None:
            return task_path
        task = load_task(task_path)
        meta = synced.tasks.get(task.task_id)
        if not meta:
            raise ValueError(f"任务未在 R2 数据湖登记表中登记为启用状态: {task.task_id}")
        return str(meta["path"])

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
        if path == "/api/runs":
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
        elif path == "/api/task/decision_artifacts":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"decision_artifacts": pipeline.list_decision_artifacts(self.runs_root, task)})
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
        if path == "/api/adjudicate":
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
        elif path == "/api/settings":
            body = self._read_body()
            if not isinstance(body, dict):
                self._json({"error": "settings payload must be a JSON object"}, status=400)
                return
            try:
                settings = panel_settings.update_settings(self.runs_root, body)
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
        task_id = str(body.get("task_id") or params.get("task_id", [""])[0])
        if not _safe_segment(task_id):
            self._json({"error": "bad task"}, status=400)
            return
        import_id = str(body.get("import_id") or params.get("import_id", [""])[0] or "").strip()
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
        max_bytes = int(os.environ.get("LLS_MAX_IMPORT_BYTES", str(100 * 1024 * 1024)))
        try:
            _apply_runtime_settings(self.runs_root)
            task_cfg = self._load_task_by_id(task_id)
            job = pipeline.start_data_lake_import(
                self.runs_root,
                task_cfg,
                import_id=import_id or None,
                overrides=overrides,
                max_bytes=max_bytes,
            )
            self._json({"ok": True, "job": job})
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
