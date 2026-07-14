from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, aliased

from ..config import TaskConfig, load_task
from .audit import append_audit_event
from .database import create_database_engine, create_session_factory
from .enums import AuditChannel, TaskMaterializationState
from .models import Task, TaskRevision, TaskRevisionMaterialization, Workspace
from .service import task_definition_fingerprint

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None


logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_FILES = frozenset({"task.yaml", "definition.json", "manifest.json"})
CACHE_METADATA_FILE = ".task_source.json"


class MaterializationError(RuntimeError):
    pass


class SnapshotValidationError(MaterializationError):
    pass


class SnapshotConflictError(SnapshotValidationError):
    pass


class MaterializationLeaseLost(MaterializationError):
    pass


class MaterializationCrash(BaseException):
    """Fault-injection exception that models process death without cleanup."""


@dataclass(frozen=True)
class RevisionSnapshot:
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    task_key: str
    revision_id: uuid.UUID
    revision_number: int
    definition: dict[str, Any]
    rendered_task: str
    content_hash: str


@dataclass(frozen=True)
class MaterializationClaim:
    materialization_id: uuid.UUID
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime
    revision: RevisionSnapshot


@dataclass(frozen=True)
class ReadyMaterialization:
    materialization_id: uuid.UUID
    revision: RevisionSnapshot


@dataclass(frozen=True)
class SnapshotRef:
    path: Path
    task_path: Path
    definition_path: Path
    manifest_path: Path
    content_hash: str


@dataclass(frozen=True)
class ActivationResult:
    state: str
    changed: bool
    revision_id: uuid.UUID
    current_revision_id: uuid.UUID | None
    current_revision_number: int | None


@dataclass(frozen=True)
class WorkerRunResult:
    action: str
    materialization_id: uuid.UUID | None = None
    activation_state: str | None = None
    cache_refreshed: bool | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, uuid.UUID) else value
            for key, value in asdict(self).items()
            if value is not None
        }


@dataclass(frozen=True)
class TaskMaterializationStatus:
    operation_id: uuid.UUID
    workspace_id: uuid.UUID
    task_id: uuid.UUID
    task_key: str
    revision_id: uuid.UUID
    revision_number: int
    state: TaskMaterializationState
    activation_state: str
    attempt_count: int
    available_at: datetime
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    claimed_by: str | None
    last_error: str | None
    materialized_at: datetime | None
    current_revision_id: uuid.UUID | None
    current_revision_number: int | None
    snapshot_path: Path
    snapshot_exists: bool
    snapshot_valid: bool | None
    snapshot_error: str | None
    terminal: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "task_materialization_status_v1",
            "operation_id": str(self.operation_id),
            "workspace_id": str(self.workspace_id),
            "task_id": str(self.task_id),
            "task_key": self.task_key,
            "revision_id": str(self.revision_id),
            "revision_number": self.revision_number,
            "state": self.state.value,
            "activation_state": self.activation_state,
            "attempt_count": self.attempt_count,
            "available_at": _isoformat(self.available_at),
            "claimed_at": _isoformat(self.claimed_at),
            "lease_expires_at": _isoformat(self.lease_expires_at),
            "claimed_by": self.claimed_by,
            "last_error": self.last_error,
            "materialized_at": _isoformat(self.materialized_at),
            "current_revision_id": (
                str(self.current_revision_id) if self.current_revision_id is not None else None
            ),
            "current_revision_number": self.current_revision_number,
            "snapshot_path": str(self.snapshot_path),
            "snapshot_exists": self.snapshot_exists,
            "snapshot_valid": self.snapshot_valid,
            "snapshot_error": self.snapshot_error,
            "terminal": self.terminal,
        }


FaultInjector = Callable[[str, RevisionSnapshot], None]


class TaskSnapshotStore:
    def __init__(
        self,
        runs_root: str | Path,
        *,
        fault_injector: FaultInjector | None = None,
    ):
        self.runs_root = Path(runs_root)
        self._fault_injector = fault_injector

    @property
    def root(self) -> Path:
        return self.runs_root / "_system" / "task_control" / "task_snapshots"

    def snapshot_path(self, revision_id: uuid.UUID, content_hash: str) -> Path:
        _require_sha256(content_hash)
        return self.root / str(revision_id) / content_hash

    def materialize(self, revision: RevisionSnapshot) -> SnapshotRef:
        expected = self._expected_files(revision)
        target = self.snapshot_path(revision.revision_id, revision.content_hash)
        target.parent.mkdir(parents=True, exist_ok=True)

        with self._revision_lock(revision.revision_id):
            if target.exists():
                return self._validate_path(target, revision, expected)

            staging_root = self.root / ".staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            staging = staging_root / f"{revision.revision_id}.{os.getpid()}.{uuid.uuid4().hex}"
            staging.mkdir(parents=False, exist_ok=False)
            preserve_staging = False
            try:
                _write_bytes_fsync(staging / "task.yaml", expected["task.yaml"])
                self._inject("after_task_write", revision)
                _write_bytes_fsync(staging / "definition.json", expected["definition.json"])
                _write_bytes_fsync(staging / "manifest.json", expected["manifest.json"])
                self._validate_path(staging, revision, expected)
                _fsync_dir(staging)
                self._inject("before_snapshot_commit", revision)

                if target.exists():
                    return self._validate_path(target, revision, expected)
                os.rename(staging, target)
                _fsync_dir(target.parent)
                self._inject("after_snapshot_commit", revision)
                return self._validate_path(target, revision, expected)
            except MaterializationCrash:
                preserve_staging = True
                raise
            finally:
                if not preserve_staging and staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

    def validate(self, revision: RevisionSnapshot) -> SnapshotRef:
        expected = self._expected_files(revision)
        return self._validate_path(
            self.snapshot_path(revision.revision_id, revision.content_hash),
            revision,
            expected,
        )

    def load(self, revision: RevisionSnapshot) -> TaskConfig:
        snapshot = self.validate(revision)
        return load_task(snapshot.task_path)

    def _expected_files(self, revision: RevisionSnapshot) -> dict[str, bytes]:
        _validate_revision(revision)
        definition_bytes = (
            json.dumps(
                revision.definition,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        task_bytes = revision.rendered_task.encode("utf-8")
        manifest = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "workspace_id": str(revision.workspace_id),
            "task_id": str(revision.task_id),
            "task_key": revision.task_key,
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "definition_sha256": _sha256_bytes(definition_bytes),
            "rendered_task_sha256": _sha256_bytes(task_bytes),
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        return {
            "task.yaml": task_bytes,
            "definition.json": definition_bytes,
            "manifest.json": manifest_bytes,
        }

    def _validate_path(
        self,
        path: Path,
        revision: RevisionSnapshot,
        expected: dict[str, bytes],
    ) -> SnapshotRef:
        if not path.is_dir() or path.is_symlink():
            raise SnapshotConflictError(f"snapshot target is not an immutable directory: {path}")
        entries = {entry.name for entry in path.iterdir()}
        if entries != SNAPSHOT_FILES:
            raise SnapshotConflictError(
                f"snapshot files differ: expected={sorted(SNAPSHOT_FILES)}, actual={sorted(entries)}",
            )
        for name, expected_bytes in expected.items():
            file_path = path / name
            if not file_path.is_file() or file_path.is_symlink():
                raise SnapshotConflictError(f"snapshot file is missing or not regular: {file_path}")
            actual_bytes = file_path.read_bytes()
            if actual_bytes != expected_bytes:
                raise SnapshotConflictError(f"snapshot file content differs: {file_path}")

        try:
            definition = json.loads((path / "definition.json").read_text(encoding="utf-8"))
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotConflictError(f"snapshot JSON is invalid: {path}") from exc
        if definition != revision.definition:
            raise SnapshotConflictError(f"snapshot definition differs: {path}")
        if not isinstance(manifest, dict):
            raise SnapshotConflictError(f"snapshot manifest is not an object: {path}")

        task = load_task(path / "task.yaml")
        if task.task_id != revision.task_key:
            raise SnapshotConflictError(
                f"snapshot task_id differs: expected={revision.task_key}, actual={task.task_id}",
            )
        return SnapshotRef(
            path=path,
            task_path=path / "task.yaml",
            definition_path=path / "definition.json",
            manifest_path=path / "manifest.json",
            content_hash=revision.content_hash,
        )

    @contextmanager
    def _revision_lock(self, revision_id: uuid.UUID):
        lock_root = self.root / ".locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"{revision_id}.lock"
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _inject(self, point: str, revision: RevisionSnapshot) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point, revision)


class CompatibilityTaskCache:
    def __init__(
        self,
        session_factory,
        snapshot_store: TaskSnapshotStore,
        tasks_root: str | Path,
    ):
        self._session_factory = session_factory
        self._snapshot_store = snapshot_store
        self.root = _cache_root(tasks_root)

    def refresh(self, task_id: uuid.UUID) -> bool:
        with self._session_factory() as session, session.begin():
            task = session.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.current_revision_id is None:
                return False
            revision = session.get(TaskRevision, task.current_revision_id)
            materialization = session.scalar(
                select(TaskRevisionMaterialization).where(
                    TaskRevisionMaterialization.revision_id == task.current_revision_id,
                    TaskRevisionMaterialization.state == TaskMaterializationState.SUCCEEDED,
                ),
            )
            if revision is None or materialization is None:
                raise SnapshotValidationError("current revision is not backed by a ready materialization")
            descriptor = _revision_snapshot(task, revision)
            snapshot = self._snapshot_store.validate(descriptor)
            with self._task_lock(task.task_key):
                return self._write_cache(descriptor, snapshot)

    def recover_all(self) -> int:
        with self._session_factory() as session:
            task_ids = tuple(
                session.scalars(select(Task.id).where(Task.current_revision_id.is_not(None))).all(),
            )
        changed = 0
        for task_id in task_ids:
            changed += int(self.refresh(task_id))
        return changed

    def _write_cache(self, revision: RevisionSnapshot, snapshot: SnapshotRef) -> bool:
        target = self.root / revision.task_key
        metadata = {
            "source": "database_current_revision",
            "workspace_id": str(revision.workspace_id),
            "task_id": str(revision.task_id),
            "task_key": revision.task_key,
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "snapshot_path": str(snapshot.path),
            "task_sha256": _sha256_bytes(snapshot.task_path.read_bytes()),
        }
        if _cache_matches(target, metadata, snapshot.task_path):
            return False

        staging_root = self.root / "_staging" / "task_materialization"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / f"{revision.task_id}.{os.getpid()}.{uuid.uuid4().hex}"
        publish = staging / "publish"
        publish.mkdir(parents=True, exist_ok=False)
        try:
            _copy_file_fsync(snapshot.task_path, publish / "task.yaml")
            _write_bytes_fsync(
                publish / CACHE_METADATA_FILE,
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                    "utf-8",
                ),
            )
            cached = load_task(publish / "task.yaml")
            if cached.task_id != revision.task_key:
                raise SnapshotValidationError("compatibility cache task_id differs from current revision")
            _fsync_dir(publish)
            _replace_directory(publish, target)
            return True
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @contextmanager
    def _task_lock(self, task_key: str):
        _require_safe_segment(task_key)
        lock_root = self.root / "_locks" / "task_materialization"
        lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = lock_root / f"{task_key}.lock"
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ControlTaskSnapshotLoader:
    def __init__(self, session_factory, runs_root: str | Path, *, engine=None):
        self._session_factory = session_factory
        self._snapshot_store = TaskSnapshotStore(runs_root)
        self._engine = engine

    @classmethod
    def from_url(
        cls,
        database_url: str | None,
        runs_root: str | Path,
    ) -> ControlTaskSnapshotLoader:
        engine = create_database_engine(database_url)
        return cls(create_session_factory(engine), runs_root, engine=engine)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    def load(self, workspace_slug: str, task_key: str) -> TaskConfig:
        _require_safe_segment(task_key)
        with self._session_factory() as session:
            row = session.execute(
                select(Task, TaskRevision, TaskRevisionMaterialization)
                .join(Workspace, Workspace.id == Task.workspace_id)
                .join(TaskRevision, TaskRevision.id == Task.current_revision_id)
                .join(
                    TaskRevisionMaterialization,
                    TaskRevisionMaterialization.revision_id == TaskRevision.id,
                )
                .where(
                    Workspace.slug == workspace_slug,
                    Task.task_key == task_key,
                    TaskRevisionMaterialization.state == TaskMaterializationState.SUCCEEDED,
                ),
            ).one_or_none()
        if row is None:
            raise SnapshotValidationError(f"task has no ready current revision: {workspace_slug}/{task_key}")
        task, revision, _materialization = row
        return self._snapshot_store.load(_revision_snapshot(task, revision))


class TaskMaterializationWorker:
    def __init__(
        self,
        session_factory,
        *,
        runs_root: str | Path,
        tasks_root: str | Path | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 30.0,
        retry_delay_seconds: float = 1.0,
        max_attempts: int = 5,
        poll_seconds: float = 1.0,
        fault_injector: FaultInjector | None = None,
        engine=None,
    ):
        if lease_seconds <= 0 or retry_delay_seconds < 0 or poll_seconds <= 0:
            raise ValueError("worker timing values are invalid")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._session_factory = session_factory
        self.worker_id = worker_id or _default_worker_id()
        if not self.worker_id.strip() or len(self.worker_id) > 255:
            raise ValueError("worker_id must be 1-255 characters")
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts
        self.poll_seconds = poll_seconds
        self._fault_injector = fault_injector
        self._snapshot_store = TaskSnapshotStore(runs_root, fault_injector=fault_injector)
        self._cache = (
            CompatibilityTaskCache(session_factory, self._snapshot_store, tasks_root)
            if tasks_root is not None
            else None
        )
        self._engine = engine

    @classmethod
    def from_url(
        cls,
        database_url: str | None = None,
        **kwargs,
    ) -> TaskMaterializationWorker:
        engine = create_database_engine(database_url)
        return cls(create_session_factory(engine), engine=engine, **kwargs)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    def run_once(self, materialization_id: uuid.UUID | None = None) -> WorkerRunResult:
        claim = self._claim(materialization_id)
        if claim is not None:
            self._inject("after_claim", claim.revision)
            try:
                self._snapshot_store.materialize(claim.revision)
                self._mark_ready(claim)
            except MaterializationCrash:
                raise
            except MaterializationLeaseLost as exc:
                return WorkerRunResult(
                    action="lease_lost",
                    materialization_id=claim.materialization_id,
                    error=str(exc),
                )
            except Exception as exc:
                terminal = self._record_failure(claim, exc)
                return WorkerRunResult(
                    action="failed" if terminal else "retry_scheduled",
                    materialization_id=claim.materialization_id,
                    error=_error_text(exc),
                )

            return self._activate_ready(
                ReadyMaterialization(claim.materialization_id, claim.revision),
                action="materialized",
            )

        ready = self._next_ready_for_activation(materialization_id)
        if ready is not None:
            return self._activate_ready(ready, action="activated")
        return WorkerRunResult(action="idle", materialization_id=materialization_id)

    def drain(
        self,
        materialization_id: uuid.UUID,
        *,
        timeout_seconds: float,
    ) -> TaskMaterializationStatus:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be nonnegative")
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = self.status(materialization_id)
            if status.terminal:
                return status
            self.run_once(materialization_id)
            status = self.status(materialization_id)
            if status.terminal or time.monotonic() >= deadline:
                return status
            time.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))

    def run_forever(self) -> None:
        if self._cache is not None:
            self._cache.recover_all()
        while True:
            try:
                result = self.run_once()
            except MaterializationCrash:
                raise
            except Exception:
                logger.exception("task materialization worker iteration failed")
                if self._cache is not None:
                    try:
                        self._cache.recover_all()
                    except Exception:
                        logger.exception("task compatibility cache recovery failed")
                time.sleep(self.poll_seconds)
                continue
            if result.action == "idle":
                time.sleep(self.poll_seconds)

    def status(self, materialization_id: uuid.UUID) -> TaskMaterializationStatus:
        current_revision = aliased(TaskRevision)
        with self._session_factory() as session:
            row = session.execute(
                select(TaskRevisionMaterialization, TaskRevision, Task, current_revision)
                .join(TaskRevision, TaskRevision.id == TaskRevisionMaterialization.revision_id)
                .join(Task, Task.id == TaskRevisionMaterialization.task_id)
                .outerjoin(current_revision, current_revision.id == Task.current_revision_id)
                .where(TaskRevisionMaterialization.id == materialization_id),
            ).one_or_none()
        if row is None:
            raise MaterializationError(f"materialization does not exist: {materialization_id}")
        materialization, revision, task, current = row
        descriptor = _revision_snapshot(task, revision)
        snapshot_path = self._snapshot_store.snapshot_path(revision.id, revision.content_hash)
        snapshot_exists = snapshot_path.exists()
        snapshot_valid: bool | None = None
        snapshot_error: str | None = None
        if snapshot_exists or materialization.state == TaskMaterializationState.SUCCEEDED:
            try:
                self._snapshot_store.validate(descriptor)
                snapshot_valid = True
            except SnapshotValidationError as exc:
                snapshot_valid = False
                snapshot_error = str(exc)

        current_number = current.revision_number if current is not None else None
        activation_state = _activation_state(materialization.state, revision, task, current_number)
        terminal = materialization.state == TaskMaterializationState.FAILED or (
            materialization.state == TaskMaterializationState.SUCCEEDED
            and activation_state in {"active", "superseded"}
        )
        return TaskMaterializationStatus(
            operation_id=materialization.id,
            workspace_id=materialization.workspace_id,
            task_id=materialization.task_id,
            task_key=task.task_key,
            revision_id=revision.id,
            revision_number=revision.revision_number,
            state=materialization.state,
            activation_state=activation_state,
            attempt_count=materialization.attempt_count,
            available_at=materialization.available_at,
            claimed_at=materialization.claimed_at,
            lease_expires_at=materialization.lease_expires_at,
            claimed_by=materialization.claimed_by,
            last_error=materialization.last_error,
            materialized_at=materialization.materialized_at,
            current_revision_id=task.current_revision_id,
            current_revision_number=current_number,
            snapshot_path=snapshot_path,
            snapshot_exists=snapshot_exists,
            snapshot_valid=snapshot_valid,
            snapshot_error=snapshot_error,
            terminal=terminal,
        )

    def recover_compatibility_caches(self) -> int:
        return self._cache.recover_all() if self._cache is not None else 0

    def _claim(self, materialization_id: uuid.UUID | None) -> MaterializationClaim | None:
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        eligible = or_(
            and_(
                TaskRevisionMaterialization.state == TaskMaterializationState.PENDING,
                TaskRevisionMaterialization.available_at <= now,
            ),
            and_(
                TaskRevisionMaterialization.state == TaskMaterializationState.PROCESSING,
                or_(
                    TaskRevisionMaterialization.lease_expires_at.is_(None),
                    TaskRevisionMaterialization.lease_expires_at <= now,
                ),
            ),
        )
        with self._session_factory() as session, session.begin():
            statement = (
                select(TaskRevisionMaterialization)
                .where(eligible)
                .order_by(
                    TaskRevisionMaterialization.available_at,
                    TaskRevisionMaterialization.created_at,
                )
                .limit(1)
            )
            if materialization_id is not None:
                statement = statement.where(TaskRevisionMaterialization.id == materialization_id)
            if session.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            candidate = session.scalar(statement)
            if candidate is None:
                return None

            claimed = session.execute(
                update(TaskRevisionMaterialization)
                .where(
                    TaskRevisionMaterialization.id == candidate.id,
                    eligible,
                )
                .values(
                    state=TaskMaterializationState.PROCESSING,
                    attempt_count=TaskRevisionMaterialization.attempt_count + 1,
                    claimed_at=now,
                    lease_expires_at=lease_expires_at,
                    claimed_by=self.worker_id,
                )
                .execution_options(synchronize_session=False),
            )
            if claimed.rowcount != 1:
                return None
            session.refresh(candidate)
            revision = session.get(TaskRevision, candidate.revision_id)
            task = session.get(Task, candidate.task_id)
            if revision is None or task is None:
                raise MaterializationError("materialization references missing task revision data")
            return MaterializationClaim(
                materialization_id=candidate.id,
                worker_id=self.worker_id,
                attempt_count=candidate.attempt_count,
                lease_expires_at=lease_expires_at,
                revision=_revision_snapshot(task, revision),
            )

    def _mark_ready(self, claim: MaterializationClaim) -> None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            materialization = session.scalar(
                select(TaskRevisionMaterialization)
                .where(TaskRevisionMaterialization.id == claim.materialization_id)
                .with_for_update(),
            )
            if not _claim_matches(materialization, claim):
                raise MaterializationLeaseLost(
                    f"materialization lease was replaced before ready commit: {claim.materialization_id}",
                )
            materialization.state = TaskMaterializationState.SUCCEEDED
            materialization.materialized_at = now
            materialization.lease_expires_at = None
            materialization.last_error = None

    def _record_failure(self, claim: MaterializationClaim, error: Exception) -> bool:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session, session.begin():
            materialization = session.scalar(
                select(TaskRevisionMaterialization)
                .where(TaskRevisionMaterialization.id == claim.materialization_id)
                .with_for_update(),
            )
            if not _claim_matches(materialization, claim):
                return False
            terminal = isinstance(error, SnapshotValidationError) or (
                materialization.attempt_count >= self.max_attempts
            )
            materialization.last_error = _error_text(error)
            materialization.lease_expires_at = None
            if terminal:
                materialization.state = TaskMaterializationState.FAILED
                append_audit_event(
                    session,
                    workspace_id=materialization.workspace_id,
                    event_type="task.revision_materialization_failed",
                    actor=None,
                    caller=None,
                    channel=AuditChannel.WORKER,
                    resource_type="task_revision_materialization",
                    resource_id=materialization.id,
                    details={
                        "task_id": str(materialization.task_id),
                        "revision_id": str(materialization.revision_id),
                        "attempt_count": materialization.attempt_count,
                        "worker_id": self.worker_id,
                        "error": materialization.last_error,
                    },
                )
            else:
                materialization.state = TaskMaterializationState.PENDING
                materialization.available_at = now + timedelta(seconds=self.retry_delay_seconds)
                materialization.claimed_at = None
                materialization.claimed_by = None
            return terminal

    def _next_ready_for_activation(
        self,
        materialization_id: uuid.UUID | None,
    ) -> ReadyMaterialization | None:
        current_revision = aliased(TaskRevision)
        with self._session_factory() as session:
            statement = (
                select(TaskRevisionMaterialization.id, TaskRevision, Task)
                .join(
                    TaskRevisionMaterialization,
                    TaskRevisionMaterialization.revision_id == TaskRevision.id,
                )
                .join(Task, Task.id == TaskRevision.task_id)
                .outerjoin(current_revision, current_revision.id == Task.current_revision_id)
                .where(
                    TaskRevisionMaterialization.state == TaskMaterializationState.SUCCEEDED,
                    or_(
                        Task.current_revision_id.is_(None),
                        current_revision.revision_number < TaskRevision.revision_number,
                    ),
                )
                .order_by(TaskRevision.revision_number.desc(), TaskRevision.created_at)
                .limit(1)
            )
            if materialization_id is not None:
                statement = statement.where(TaskRevisionMaterialization.id == materialization_id)
            row = session.execute(statement).one_or_none()
        if row is None:
            return None
        ready_id, revision, task = row
        return ReadyMaterialization(ready_id, _revision_snapshot(task, revision))

    def _activate_ready(
        self,
        ready: ReadyMaterialization,
        *,
        action: str,
    ) -> WorkerRunResult:
        try:
            self._snapshot_store.validate(ready.revision)
        except SnapshotValidationError as exc:
            self._record_ready_failure(ready.materialization_id, exc)
            return WorkerRunResult(
                action="failed",
                materialization_id=ready.materialization_id,
                error=_error_text(exc),
            )
        self._inject("before_activation", ready.revision)
        activation = self._activate(ready.revision.revision_id)
        self._inject("after_activation", ready.revision)
        cache_refreshed = self._refresh_cache(ready.revision.task_id)
        return WorkerRunResult(
            action=action,
            materialization_id=ready.materialization_id,
            activation_state=activation.state,
            cache_refreshed=cache_refreshed,
        )

    def _record_ready_failure(
        self,
        materialization_id: uuid.UUID,
        error: SnapshotValidationError,
    ) -> None:
        with self._session_factory() as session, session.begin():
            materialization = session.scalar(
                select(TaskRevisionMaterialization)
                .where(TaskRevisionMaterialization.id == materialization_id)
                .with_for_update(),
            )
            if materialization is None or materialization.state != TaskMaterializationState.SUCCEEDED:
                return
            materialization.state = TaskMaterializationState.FAILED
            materialization.last_error = _error_text(error)
            append_audit_event(
                session,
                workspace_id=materialization.workspace_id,
                event_type="task.revision_materialization_failed",
                actor=None,
                caller=None,
                channel=AuditChannel.WORKER,
                resource_type="task_revision_materialization",
                resource_id=materialization.id,
                details={
                    "task_id": str(materialization.task_id),
                    "revision_id": str(materialization.revision_id),
                    "attempt_count": materialization.attempt_count,
                    "worker_id": self.worker_id,
                    "error": materialization.last_error,
                    "phase": "ready_validation",
                },
            )

    def _activate(self, revision_id: uuid.UUID) -> ActivationResult:
        with self._session_factory() as session, session.begin():
            revision = session.get(TaskRevision, revision_id)
            materialization = session.scalar(
                select(TaskRevisionMaterialization).where(
                    TaskRevisionMaterialization.revision_id == revision_id,
                    TaskRevisionMaterialization.state == TaskMaterializationState.SUCCEEDED,
                ),
            )
            if revision is None or materialization is None:
                raise MaterializationError("revision is not ready for activation")
            task = session.scalar(select(Task).where(Task.id == revision.task_id).with_for_update())
            if task is None or task.workspace_id != revision.workspace_id:
                raise MaterializationError("revision task disappeared before activation")

            current = session.get(TaskRevision, task.current_revision_id) if task.current_revision_id else None
            current_number = current.revision_number if current is not None else None
            if task.current_revision_id == revision.id:
                return ActivationResult(
                    state="active",
                    changed=False,
                    revision_id=revision.id,
                    current_revision_id=revision.id,
                    current_revision_number=revision.revision_number,
                )
            if current_number is not None and current_number >= revision.revision_number:
                return ActivationResult(
                    state="superseded",
                    changed=False,
                    revision_id=revision.id,
                    current_revision_id=task.current_revision_id,
                    current_revision_number=current_number,
                )

            previous_revision_id = task.current_revision_id
            task.current_revision_id = revision.id
            session.flush()
            append_audit_event(
                session,
                workspace_id=task.workspace_id,
                event_type="task.revision_activated",
                actor=None,
                caller=None,
                channel=AuditChannel.WORKER,
                resource_type="task_revision",
                resource_id=revision.id,
                details={
                    "task_id": str(task.id),
                    "task_key": task.task_key,
                    "revision_number": revision.revision_number,
                    "previous_revision_id": (
                        str(previous_revision_id) if previous_revision_id is not None else None
                    ),
                    "materialization_id": str(materialization.id),
                    "worker_id": self.worker_id,
                },
            )
            return ActivationResult(
                state="active",
                changed=True,
                revision_id=revision.id,
                current_revision_id=revision.id,
                current_revision_number=revision.revision_number,
            )

    def _refresh_cache(self, task_id: uuid.UUID) -> bool | None:
        if self._cache is None:
            return None
        return self._cache.refresh(task_id)

    def _inject(self, point: str, revision: RevisionSnapshot) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point, revision)


def _revision_snapshot(task: Task, revision: TaskRevision) -> RevisionSnapshot:
    if task.id != revision.task_id or task.workspace_id != revision.workspace_id:
        raise SnapshotValidationError("task revision ownership differs")
    return RevisionSnapshot(
        workspace_id=revision.workspace_id,
        task_id=revision.task_id,
        task_key=task.task_key,
        revision_id=revision.id,
        revision_number=revision.revision_number,
        definition=revision.definition,
        rendered_task=revision.rendered_task,
        content_hash=revision.content_hash,
    )


def _validate_revision(revision: RevisionSnapshot) -> None:
    _require_safe_segment(revision.task_key)
    if revision.revision_number < 1:
        raise SnapshotValidationError("revision_number must be positive")
    if not isinstance(revision.definition, dict):
        raise SnapshotValidationError("revision definition must be an object")
    definition_task_key = str(revision.definition.get("task_id") or "").strip()
    if definition_task_key != revision.task_key:
        raise SnapshotValidationError(
            f"definition task_id differs: expected={revision.task_key}, actual={definition_task_key or '-'}",
        )
    try:
        raw = yaml.safe_load(revision.rendered_task)
    except yaml.YAMLError as exc:
        raise SnapshotValidationError("rendered task is not valid YAML") from exc
    if not isinstance(raw, dict):
        raise SnapshotValidationError("rendered task must be a YAML mapping")
    for key in ("task_id", "id_field", "input", "labels"):
        if key not in raw:
            raise SnapshotValidationError(f"rendered task is missing required key: {key}")
    rendered_task_key = str(raw.get("task_id") or "").strip()
    if rendered_task_key != revision.task_key:
        raise SnapshotValidationError(
            f"rendered task_id differs: expected={revision.task_key}, actual={rendered_task_key or '-'}",
        )
    if "revision" in raw:
        try:
            rendered_revision = int(raw["revision"])
        except (TypeError, ValueError) as exc:
            raise SnapshotValidationError("rendered task revision must be an integer") from exc
        if rendered_revision != revision.revision_number:
            raise SnapshotValidationError(
                f"rendered revision differs: expected={revision.revision_number}, actual={rendered_revision}",
            )
    calculated_hash = task_definition_fingerprint(revision.definition, revision.rendered_task)
    if calculated_hash != revision.content_hash:
        raise SnapshotValidationError(
            f"revision content hash differs: expected={revision.content_hash}, actual={calculated_hash}",
        )


def _claim_matches(
    materialization: TaskRevisionMaterialization | None,
    claim: MaterializationClaim,
) -> bool:
    return bool(
        materialization is not None
        and materialization.state == TaskMaterializationState.PROCESSING
        and materialization.claimed_by == claim.worker_id
        and materialization.attempt_count == claim.attempt_count
    )


def _activation_state(
    state: TaskMaterializationState,
    revision: TaskRevision,
    task: Task,
    current_revision_number: int | None,
) -> str:
    if state != TaskMaterializationState.SUCCEEDED:
        return "not_ready"
    if task.current_revision_id == revision.id:
        return "active"
    if current_revision_number is None or current_revision_number < revision.revision_number:
        return "awaiting_activation"
    return "superseded"


def _cache_root(tasks_root: str | Path) -> Path:
    parts: list[str] = []
    for chunk in str(tasks_root).split(os.pathsep):
        parts.extend(item.strip() for item in chunk.split(","))
    return Path(parts[-1]) if parts else Path("tasks")


def _cache_matches(target: Path, metadata: dict[str, Any], task_path: Path) -> bool:
    try:
        if not target.is_dir():
            return False
        stored = json.loads((target / CACHE_METADATA_FILE).read_text(encoding="utf-8"))
        if stored != metadata:
            return False
        cached_task = target / "task.yaml"
        return cached_task.is_file() and cached_task.read_bytes() == task_path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _replace_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists():
        backup = target.with_name(f".{target.name}.old.{os.getpid()}.{uuid.uuid4().hex}")
        os.replace(target, backup)
    try:
        os.replace(source, target)
        _fsync_dir(target.parent)
    except Exception:
        if backup is not None and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def _copy_file_fsync(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as source_handle, target.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle)
        target_handle.flush()
        os.fsync(target_handle.fileno())


def _write_bytes_fsync(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: str | Path) -> None:
    try:
        descriptor = os.open(Path(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise SnapshotValidationError("content_hash must be lowercase SHA-256")


def _require_safe_segment(value: str) -> None:
    if not value or ".." in value or "/" in value or "\\" in value:
        raise SnapshotValidationError(f"unsafe task key: {value}")


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def _error_text(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}".strip()
    return text[:4000]


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
