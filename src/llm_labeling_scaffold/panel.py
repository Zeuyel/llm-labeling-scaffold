from __future__ import annotations

import base64
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .io import read_json, read_jsonl, write_jsonl
from . import pipeline

POOL_FILES = {
    "merged": ("merged", "merged_clean.jsonl"),
    "missing": ("merged", "missing_pool.jsonl"),
    "duplicate": ("merged", "duplicate_pool.jsonl"),
    "conflict": ("merged", "conflict_pool.jsonl"),
}


def _safe_segment(value: str) -> bool:
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


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
    tasks_root: Path = Path("examples,tasks")
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
            self._json({"tasks": pipeline.list_tasks(self.tasks_root)})
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
            self._json({"imports": pipeline.list_imports(self.runs_root, task)})
        elif path == "/api/task/annotation_jobs":
            task = params.get("task_id", [""])[0]
            if not _safe_segment(task):
                self._json({"error": "bad task"}, status=400)
                return
            self._json({"annotation_jobs": pipeline.list_annotation_jobs(self.runs_root, task)})
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
                job = pipeline.start_action(self.runs_root, task_path, action, body.get("params", {}))
                self._json({"ok": True, "job": job})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/tasks":
            body = self._read_body()
            try:
                task = pipeline.create_task(self.tasks_root, body)
                self._json({"ok": True, "task": task})
            except Exception as exc:
                self._json({"error": str(exc)}, status=400)
        elif path == "/api/import":
            self._import(params)
        else:
            self._json({"error": "not found"}, status=404)

    def _import(self, params) -> None:
        task = params.get("task", [""])[0]
        name = params.get("name", ["imported"])[0]
        if not _safe_segment(task) or not _safe_segment(name):
            self._json({"error": "bad task/name"}, status=400)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            rows = parse_import_rows(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
            return
        dest = self.runs_root / task / "imports" / name / "raw.jsonl"
        write_jsonl(rows, dest)
        manifest = {
            "task_id": task,
            "import_id": name,
            "path": str(dest),
            "rows": len(rows),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "upload",
        }
        manifest_path = self.runs_root / task / "imports" / name / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self._json({"ok": True, "rows": len(rows), "path": str(dest)})


def serve_panel(runs_root: str | Path = "runs", host: str = "127.0.0.1",
                port: int = 8765, user: str = "admin", password: str | None = None,
                static_dir: str | Path | None = None,
                tasks_root: str | Path = "examples,tasks") -> None:
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
