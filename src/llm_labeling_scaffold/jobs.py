from __future__ import annotations

import json
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_LOCK = threading.Lock()
_REGISTRY: dict[str, "Job"] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Job:
    def __init__(self, kind: str, params: dict[str, Any], jobs_dir: Path):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.params = params
        self.status = "pending"
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self.logs: list[str] = []
        self.jobs_dir = jobs_dir

    def log(self, line: str) -> None:
        self.logs.append(f"[{_now()}] {line}")
        self._persist()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "logs": self.logs,
        }

    def _persist(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        path = self.jobs_dir / f"{self.id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def create_job(kind: str, params: dict, jobs_dir: Path) -> Job:
    job = Job(kind, params, Path(jobs_dir))
    with _LOCK:
        _REGISTRY[job.id] = job
    job._persist()
    return job


def get_job(job_id: str, jobs_dir: Path | None = None) -> dict | None:
    with _LOCK:
        job = _REGISTRY.get(job_id)
    if job is not None:
        return job.to_dict()
    if jobs_dir is not None:
        path = Path(jobs_dir) / f"{job_id}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def list_jobs(jobs_dir: Path) -> list[dict]:
    out: dict[str, dict] = {}
    jd = Path(jobs_dir)
    if jd.is_dir():
        for p in jd.glob("*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out[d["id"]] = d
            except Exception:
                pass
    with _LOCK:
        for jid, job in _REGISTRY.items():
            out[jid] = job.to_dict()
    return sorted(out.values(), key=lambda d: d.get("created_at", ""), reverse=True)


def run_job(job: Job, target: Callable[[Job], dict]) -> None:
    def _runner() -> None:
        job.status = "running"
        job.started_at = _now()
        job._persist()
        try:
            result = target(job)
            job.result = result if isinstance(result, dict) else {"value": result}
            job.status = "succeeded"
        except Exception as exc:  # noqa: BLE001
            job.error = f"{type(exc).__name__}: {exc}"
            job.logs.append(traceback.format_exc())
            job.status = "failed"
        finally:
            job.finished_at = _now()
            job._persist()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
