from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import logging
import os
import socket
import stat
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, aliased

from ..config import TaskConfig
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
RENAME_NOREPLACE = 1


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


class FailureDisposition(str, Enum):
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    LEASE_LOST = "lease_lost"


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
class LoadedTaskRevision:
    revision_id: uuid.UUID
    revision_number: int
    definition: dict[str, Any]
    rendered_task: str
    task_config: TaskConfig


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
class ValidatedSnapshot:
    ref: SnapshotRef
    task: TaskConfig
    task_bytes: bytes


@dataclass(frozen=True)
class PinnedRootDirectory:
    parent_fd: int
    root_fd: int
    basename: str
    path: Path
    device: int
    inode: int
    description: str
    path_entries: tuple[PinnedDirectoryEntry, ...]


@dataclass(frozen=True)
class PinnedDirectoryEntry:
    parent_fd: int
    fd: int
    name: str
    device: int
    inode: int
    description: str


@dataclass(frozen=True)
class PinnedRegularFile:
    parent_fd: int
    fd: int
    name: str
    device: int
    inode: int
    description: str


@dataclass(frozen=True)
class PinnedFileLock:
    root: PinnedRootDirectory
    directories: tuple[PinnedDirectoryEntry, ...]
    file: PinnedRegularFile


@dataclass(frozen=True)
class PinnedSnapshotAuthority:
    root: PinnedRootDirectory
    directories: tuple[PinnedDirectoryEntry, ...]
    files: tuple[tuple[PinnedRegularFile, bytes], ...]
    validated: ValidatedSnapshot


@dataclass(frozen=True)
class PinnedCacheDirectory:
    root: PinnedRootDirectory
    lock: PinnedFileLock
    task_fd: int
    task_key: str
    device: int
    inode: int


@dataclass(frozen=True)
class ActivationResult:
    state: str
    changed: bool
    revision_id: uuid.UUID
    current_revision_id: uuid.UUID | None
    current_revision_number: int | None
    previous_revision_id: uuid.UUID | None = None


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

    def snapshot_exists(self, revision: RevisionSnapshot) -> bool:
        revision_name = str(revision.revision_id)
        _require_safe_segment(revision_name)
        _require_sha256(revision.content_hash)
        try:
            with _pinned_root_directory(
                self.root,
                description="task snapshot root",
                create=False,
            ) as snapshot_root:
                with _pinned_directory_at(
                    snapshot_root.root_fd,
                    revision_name,
                    description="task snapshot revision directory",
                    create=False,
                ) as revision_directory:
                    try:
                        os.stat(
                            revision.content_hash,
                            dir_fd=revision_directory.fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        return False
                    return True
        except FileNotFoundError:
            return False
        except SnapshotValidationError:
            return True

    def materialize(self, revision: RevisionSnapshot) -> SnapshotRef:
        expected = self._expected_files(revision)
        target = self.snapshot_path(revision.revision_id, revision.content_hash)
        revision_name = str(revision.revision_id)
        _require_safe_segment(revision_name)
        _require_sha256(revision.content_hash)

        with _pinned_root_directory(
            self.root,
            description="task snapshot root",
        ) as snapshot_root:
            with self._revision_lock(snapshot_root, revision) as revision_lock:
                _assert_pinned_file_lock(revision_lock)
                with _pinned_directory_at(
                    snapshot_root.root_fd,
                    revision_name,
                    description=f"task snapshot revision directory: {target.parent}",
                ) as revision_directory:
                    _assert_snapshot_path(
                        snapshot_root,
                        (revision_directory,),
                        revision_lock,
                    )
                    existing = self._validate_existing_target(
                        snapshot_root,
                        revision_directory,
                        target,
                        revision,
                        expected,
                        lock=revision_lock,
                    )
                    if existing is not None:
                        return existing.ref

                    _assert_snapshot_path(
                        snapshot_root,
                        (revision_directory,),
                        revision_lock,
                    )
                    with _pinned_directory_at(
                        snapshot_root.root_fd,
                        ".staging",
                        description="task snapshot staging root",
                    ) as staging_root:
                        _assert_snapshot_path(
                            snapshot_root,
                            (revision_directory, staging_root),
                            revision_lock,
                        )
                        staging_name = (
                            f"{revision.revision_id}.{os.getpid()}.{uuid.uuid4().hex}"
                        )
                        staging_path = self.root / ".staging" / staging_name
                        _assert_snapshot_path(
                            snapshot_root,
                            (revision_directory, staging_root),
                            revision_lock,
                        )
                        staging = _create_pinned_directory_at(
                            staging_root.fd,
                            staging_name,
                            description=f"task snapshot staging directory: {staging_path}",
                            mode=0o755,
                        )
                        _assert_snapshot_path(
                            snapshot_root,
                            (revision_directory, staging_root, staging),
                            revision_lock,
                        )
                        preserve_staging = False
                        committed = False
                        try:
                            _write_new_regular_file_at(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                staging,
                                "task.yaml",
                                expected["task.yaml"],
                                description=f"task snapshot file: {staging_path / 'task.yaml'}",
                                lock=revision_lock,
                            )
                            self._inject("after_task_write", revision)
                            _write_new_regular_file_at(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                staging,
                                "definition.json",
                                expected["definition.json"],
                                description=f"task snapshot file: {staging_path / 'definition.json'}",
                                lock=revision_lock,
                            )
                            _write_new_regular_file_at(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                staging,
                                "manifest.json",
                                expected["manifest.json"],
                                description=f"task snapshot file: {staging_path / 'manifest.json'}",
                                lock=revision_lock,
                            )
                            self._validate_snapshot_directory(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                staging,
                                staging_path,
                                revision,
                                expected,
                                lock=revision_lock,
                            )
                            _assert_snapshot_path(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                revision_lock,
                            )
                            os.fsync(staging.fd)
                            _assert_snapshot_path(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                revision_lock,
                            )
                            self._inject("before_snapshot_commit", revision)

                            existing = self._validate_existing_target(
                                snapshot_root,
                                revision_directory,
                                target,
                                revision,
                                expected,
                                lock=revision_lock,
                            )
                            if existing is not None:
                                return existing.ref
                            _assert_snapshot_path(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                revision_lock,
                            )
                            self._inject("before_snapshot_rename", revision)
                            _assert_snapshot_path(
                                snapshot_root,
                                (revision_directory, staging_root, staging),
                                revision_lock,
                            )
                            try:
                                _rename_directory_noreplace_at(
                                    staging_root.fd,
                                    staging.name,
                                    revision_directory.fd,
                                    revision.content_hash,
                                )
                            except FileExistsError:
                                existing = self._validate_existing_target(
                                    snapshot_root,
                                    revision_directory,
                                    target,
                                    revision,
                                    expected,
                                    lock=revision_lock,
                                )
                                if existing is None:
                                    raise SnapshotConflictError(
                                        f"snapshot target changed during commit: {target}",
                                    )
                                return existing.ref
                            committed = True
                            committed_directory = PinnedDirectoryEntry(
                                parent_fd=revision_directory.fd,
                                fd=staging.fd,
                                name=revision.content_hash,
                                device=staging.device,
                                inode=staging.inode,
                                description=f"task snapshot target: {target}",
                            )
                            _assert_snapshot_path(
                                snapshot_root,
                                (revision_directory, staging_root, committed_directory),
                                revision_lock,
                            )
                            os.fsync(revision_directory.fd)
                            _assert_snapshot_path(
                                snapshot_root,
                                (revision_directory, staging_root, committed_directory),
                                revision_lock,
                            )
                            os.fsync(staging_root.fd)
                            _assert_snapshot_path(
                                snapshot_root,
                                (revision_directory, staging_root, committed_directory),
                                revision_lock,
                            )
                            self._inject("after_snapshot_commit", revision)
                            return self._validate_snapshot_directory(
                                snapshot_root,
                                (revision_directory, staging_root, committed_directory),
                                committed_directory,
                                target,
                                revision,
                                expected,
                                lock=revision_lock,
                            ).ref
                        except MaterializationCrash:
                            preserve_staging = True
                            raise
                        finally:
                            try:
                                if not preserve_staging and not committed:
                                    _cleanup_staging_directory(
                                        snapshot_root,
                                        staging_root,
                                        staging,
                                        lock=revision_lock,
                                    )
                            finally:
                                os.close(staging.fd)

    def validate(self, revision: RevisionSnapshot) -> SnapshotRef:
        return self._validated_snapshot(revision).ref

    def load(self, revision: RevisionSnapshot) -> TaskConfig:
        return self._validated_snapshot(revision).task

    def read_task_bytes(self, revision: RevisionSnapshot) -> tuple[SnapshotRef, bytes]:
        validated = self._validated_snapshot(revision)
        return validated.ref, validated.task_bytes

    @contextmanager
    def pinned(self, revision: RevisionSnapshot):
        expected = self._expected_files(revision)
        target = self.snapshot_path(revision.revision_id, revision.content_hash)
        revision_name = str(revision.revision_id)
        _require_safe_segment(revision_name)
        try:
            with _pinned_root_directory(
                self.root,
                description="task snapshot root",
                create=False,
            ) as snapshot_root:
                with _pinned_directory_at(
                    snapshot_root.root_fd,
                    revision_name,
                    description=f"task snapshot revision directory: {target.parent}",
                    create=False,
                ) as revision_directory:
                    with _pinned_directory_at(
                        revision_directory.fd,
                        revision.content_hash,
                        description=f"task snapshot target: {target}",
                        create=False,
                    ) as target_directory:
                        path_entries = (revision_directory, target_directory)
                        _require_snapshot_entries(snapshot_root, path_entries, target_directory, target)
                        with ExitStack() as stack:
                            pinned_files: list[tuple[PinnedRegularFile, bytes]] = []
                            for name, expected_bytes in expected.items():
                                description = f"task snapshot file: {target / name}"
                                try:
                                    file = stack.enter_context(
                                        _pinned_existing_regular_file_at(
                                            target_directory.fd,
                                            name,
                                            description=description,
                                        ),
                                    )
                                except SnapshotValidationError as exc:
                                    raise SnapshotConflictError(
                                        f"snapshot file is missing or not regular: {description}",
                                    ) from exc
                                pinned_files.append((file, expected_bytes))
                            validated = self._validated_snapshot_from_bytes(
                                target,
                                revision,
                                expected,
                            )
                            authority = PinnedSnapshotAuthority(
                                root=snapshot_root,
                                directories=path_entries,
                                files=tuple(pinned_files),
                                validated=validated,
                            )
                            _assert_pinned_snapshot_authority(authority)
                            try:
                                yield authority
                            finally:
                                _assert_pinned_snapshot_authority(authority)
        except FileNotFoundError as exc:
            raise SnapshotConflictError(f"snapshot target does not exist: {target}") from exc

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

    def _validated_snapshot(self, revision: RevisionSnapshot) -> ValidatedSnapshot:
        with self.pinned(revision) as authority:
            return authority.validated

    def _validate_existing_target(
        self,
        snapshot_root: PinnedRootDirectory,
        revision_directory: PinnedDirectoryEntry,
        path: Path,
        revision: RevisionSnapshot,
        expected: dict[str, bytes],
        *,
        lock: PinnedFileLock | None = None,
    ) -> ValidatedSnapshot | None:
        _assert_snapshot_path(snapshot_root, (revision_directory,), lock)
        try:
            with _pinned_directory_at(
                revision_directory.fd,
                revision.content_hash,
                description=f"task snapshot target: {path}",
                create=False,
            ) as target_directory:
                return self._validate_snapshot_directory(
                    snapshot_root,
                    (revision_directory, target_directory),
                    target_directory,
                    path,
                    revision,
                    expected,
                    lock=lock,
                )
        except FileNotFoundError:
            return None

    def _validate_snapshot_directory(
        self,
        snapshot_root: PinnedRootDirectory,
        path_entries: tuple[PinnedDirectoryEntry, ...],
        directory: PinnedDirectoryEntry,
        path: Path,
        revision: RevisionSnapshot,
        expected: dict[str, bytes],
        *,
        lock: PinnedFileLock | None = None,
    ) -> ValidatedSnapshot:
        _require_snapshot_entries(snapshot_root, path_entries, directory, path, lock=lock)
        actual: dict[str, bytes] = {}
        for name, expected_bytes in expected.items():
            actual_bytes = _read_snapshot_regular_file_at(
                snapshot_root,
                path_entries,
                directory,
                name,
                description=f"task snapshot file: {path / name}",
                expected_size=len(expected_bytes),
                lock=lock,
            )
            if actual_bytes != expected_bytes:
                raise SnapshotConflictError(f"snapshot file content differs: {path / name}")
            actual[name] = actual_bytes

        _assert_snapshot_path(snapshot_root, path_entries, lock)
        return self._validated_snapshot_from_bytes(path, revision, actual)

    def _validated_snapshot_from_bytes(
        self,
        path: Path,
        revision: RevisionSnapshot,
        actual: dict[str, bytes],
    ) -> ValidatedSnapshot:
        try:
            definition = json.loads(actual["definition.json"].decode("utf-8"))
            manifest = json.loads(actual["manifest.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotConflictError(f"snapshot JSON is invalid: {path}") from exc
        if definition != revision.definition:
            raise SnapshotConflictError(f"snapshot definition differs: {path}")
        if not isinstance(manifest, dict):
            raise SnapshotConflictError(f"snapshot manifest is not an object: {path}")

        try:
            task_raw = yaml.safe_load(actual["task.yaml"].decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SnapshotConflictError(f"snapshot task YAML is invalid: {path}") from exc
        if not isinstance(task_raw, dict) or task_raw.get("task_id") != revision.task_key:
            raise SnapshotConflictError(
                f"snapshot task_id differs: expected={revision.task_key}, actual={task_raw!r}",
            )
        ref = SnapshotRef(
            path=path,
            task_path=path / "task.yaml",
            definition_path=path / "definition.json",
            manifest_path=path / "manifest.json",
            content_hash=revision.content_hash,
        )
        return ValidatedSnapshot(
            ref=ref,
            task=TaskConfig(path=ref.task_path, raw=task_raw),
            task_bytes=actual["task.yaml"],
        )

    @contextmanager
    def _revision_lock(
        self,
        snapshot_root: PinnedRootDirectory,
        revision: RevisionSnapshot,
    ):
        revision_name = str(revision.revision_id)
        _require_safe_segment(revision_name)
        with _pinned_file_lock(
            snapshot_root,
            directories=((".locks", "task snapshot lock directory"),),
            filename=f"{revision_name}.lock",
            file_description="task snapshot revision lock file",
            after_acquired=lambda: self._inject("after_snapshot_lock_acquired", revision),
        ) as lock:
            yield lock

    def _inject(self, point: str, revision: RevisionSnapshot) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point, revision)


class CompatibilityTaskCache:
    def __init__(
        self,
        session_factory,
        snapshot_store: TaskSnapshotStore,
        tasks_root: str | Path,
        *,
        fault_injector: FaultInjector | None = None,
    ):
        self._session_factory = session_factory
        self._snapshot_store = snapshot_store
        self.root = _cache_root(tasks_root)
        self._fault_injector = fault_injector

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
            snapshot, task_bytes = self._snapshot_store.read_task_bytes(descriptor)
            with _pinned_root_directory(
                self.root,
                description="compatibility cache root",
            ) as cache_root:
                with self._task_lock(cache_root, descriptor) as cache_lock:
                    return self._write_cache(
                        cache_root,
                        cache_lock,
                        descriptor,
                        snapshot,
                        task_bytes,
                    )

    def recover_all(self, *, failed_task_ids: set[uuid.UUID] | None = None) -> int:
        with self._session_factory() as session:
            task_ids = tuple(
                session.scalars(select(Task.id).where(Task.current_revision_id.is_not(None))).all(),
            )
        changed = 0
        failed: set[uuid.UUID] = set()
        for task_id in task_ids:
            try:
                changed += int(self.refresh(task_id))
            except Exception:
                failed.add(task_id)
                logger.exception("task compatibility cache refresh failed", extra={"task_id": str(task_id)})
        if failed_task_ids is not None:
            failed_task_ids.clear()
            failed_task_ids.update(failed)
        return changed

    def _write_cache(
        self,
        cache_root: PinnedRootDirectory,
        cache_lock: PinnedFileLock,
        revision: RevisionSnapshot,
        snapshot: SnapshotRef,
        task_bytes: bytes,
    ) -> bool:
        metadata = {
            "source": "database_current_revision",
            "workspace_id": str(revision.workspace_id),
            "task_id": str(revision.task_id),
            "task_key": revision.task_key,
            "revision_id": str(revision.revision_id),
            "revision_number": revision.revision_number,
            "content_hash": revision.content_hash,
            "snapshot_path": str(snapshot.path),
            "task_sha256": _sha256_bytes(task_bytes),
        }
        metadata_bytes = (
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        with _pinned_cache_directory(cache_root, cache_lock, revision.task_key) as directory:
            _require_regular_or_missing_at(directory, "task.yaml")
            _require_regular_or_missing_at(directory, CACHE_METADATA_FILE)
            if _cache_matches_at(directory, metadata, metadata_bytes, task_bytes):
                return False
            _write_bytes_atomic_at(directory, "task.yaml", task_bytes)
            self._inject("after_cache_task_write", revision)
            _write_bytes_atomic_at(directory, CACHE_METADATA_FILE, metadata_bytes)
            if not _cache_matches_at(directory, metadata, metadata_bytes, task_bytes):
                raise SnapshotValidationError("compatibility cache verification failed after write")
            return True

    def _inject(self, point: str, revision: RevisionSnapshot) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point, revision)

    @contextmanager
    def _task_lock(self, cache_root: PinnedRootDirectory, revision: RevisionSnapshot):
        _require_safe_segment(revision.task_key)
        with _pinned_file_lock(
            cache_root,
            directories=(
                ("_locks", "compatibility cache lock directory"),
                ("task_materialization", "task materialization lock directory"),
            ),
            filename=f"{revision.task_key}.lock",
            file_description="task materialization lock file",
            after_acquired=lambda: self._inject("after_cache_lock_acquired", revision),
        ) as lock:
            yield lock


class ControlTaskSnapshotLoader:
    def __init__(
        self,
        session_factory,
        runs_root: str | Path,
        tasks_root: str | Path = "tasks",
        *,
        engine=None,
    ):
        self._session_factory = session_factory
        self._snapshot_store = TaskSnapshotStore(runs_root)
        self._logical_root = _cache_root(tasks_root)
        self._engine = engine

    @classmethod
    def from_url(
        cls,
        database_url: str | None,
        runs_root: str | Path,
        tasks_root: str | Path = "tasks",
    ) -> ControlTaskSnapshotLoader:
        engine = create_database_engine(database_url)
        return cls(create_session_factory(engine), runs_root, tasks_root, engine=engine)

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    def load(self, workspace_slug: str, task_key: str) -> TaskConfig:
        loaded = self.load_active_revision(workspace_slug, task_key)
        if loaded is None:
            raise SnapshotValidationError(f"task has no ready current revision: {workspace_slug}/{task_key}")
        return loaded.task_config

    def load_active_revision(self, workspace_slug: str, task_key: str) -> LoadedTaskRevision | None:
        _require_safe_segment(workspace_slug)
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
            return None
        task, revision, _materialization = row
        descriptor = _revision_snapshot(task, revision)
        snapshot_task = self._snapshot_store.load(descriptor)
        return LoadedTaskRevision(
            revision_id=revision.id,
            revision_number=revision.revision_number,
            definition=dict(revision.definition),
            rendered_task=revision.rendered_task,
            task_config=TaskConfig(
                path=self._logical_root / task_key / "task.yaml",
                raw=snapshot_task.raw,
            ),
        )


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
            CompatibilityTaskCache(
                session_factory,
                self._snapshot_store,
                tasks_root,
                fault_injector=fault_injector,
            )
            if tasks_root is not None
            else None
        )
        self._pending_cache_repairs: set[uuid.UUID] = set()
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
        cache_repair: WorkerRunResult | None = None
        if materialization_id is None:
            cache_repair = self._reconcile_queued_cache()
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
                    error=_error_text(exc),
                )
            except Exception as exc:
                disposition = self._record_failure(claim, exc)
                error = _error_text(exc)
                if disposition == FailureDisposition.LEASE_LOST:
                    error = (
                        f"MaterializationLeaseLost: claim was replaced while recording failure; "
                        f"original={error}"
                    )
                return WorkerRunResult(
                    action=disposition.value,
                    materialization_id=claim.materialization_id,
                    error=error,
                )

            return self._activate_ready(
                ReadyMaterialization(claim.materialization_id, claim.revision),
                action="materialized",
            )

        ready = self._next_ready_for_activation(materialization_id)
        if ready is not None:
            return self._activate_ready(ready, action="activated")
        if materialization_id is not None:
            status = self.status(materialization_id)
            if status.terminal:
                return self._reconcile_task_cache(
                    status.task_id,
                    materialization_id=materialization_id,
                )
        elif self._cache is not None:
            if cache_repair is not None:
                return cache_repair
            changed = self._recover_all_caches()
            if changed:
                return WorkerRunResult(action="cache_reconciled", cache_refreshed=True)
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
                self._reconcile_task_cache(
                    status.task_id,
                    materialization_id=materialization_id,
                )
                return status
            self.run_once(materialization_id)
            status = self.status(materialization_id)
            if status.terminal:
                self._reconcile_task_cache(
                    status.task_id,
                    materialization_id=materialization_id,
                )
                return status
            if time.monotonic() >= deadline:
                return status
            time.sleep(min(self.poll_seconds, max(0.0, deadline - time.monotonic())))

    def run_forever(self) -> None:
        if self._cache is not None:
            self._recover_all_caches()
        while True:
            try:
                ready = self._next_ready_for_activation(None)
                result = (
                    self._activate_ready(ready, action="activated")
                    if ready is not None
                    else self.run_once()
                )
            except MaterializationCrash:
                raise
            except Exception:
                logger.exception("task materialization worker iteration failed")
                if self._cache is not None:
                    try:
                        self._recover_all_caches()
                    except Exception:
                        logger.exception("task compatibility cache recovery failed")
                time.sleep(self.poll_seconds)
                continue
            if result.action in {"idle", "cache_reconcile_failed"}:
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
        snapshot_exists = self._snapshot_store.snapshot_exists(descriptor)
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
        return self._recover_all_caches()

    def _recover_all_caches(self) -> int:
        if self._cache is None:
            return 0
        return self._cache.recover_all(failed_task_ids=self._pending_cache_repairs)

    def _reconcile_queued_cache(self) -> WorkerRunResult | None:
        if self._cache is None or not self._pending_cache_repairs:
            return None
        task_id = next(iter(self._pending_cache_repairs))
        return self._reconcile_task_cache(task_id)

    def _reconcile_task_cache(
        self,
        task_id: uuid.UUID,
        *,
        materialization_id: uuid.UUID | None = None,
    ) -> WorkerRunResult:
        if self._cache is None:
            return WorkerRunResult(action="idle", materialization_id=materialization_id)
        try:
            changed = self._cache.refresh(task_id)
        except Exception as exc:
            self._pending_cache_repairs.add(task_id)
            logger.exception(
                "task compatibility cache reconciliation failed",
                extra={"task_id": str(task_id)},
            )
            return WorkerRunResult(
                action="cache_reconcile_failed",
                materialization_id=materialization_id,
                cache_refreshed=False,
                error=_error_text(exc),
            )
        self._pending_cache_repairs.discard(task_id)
        return WorkerRunResult(
            action="cache_reconciled",
            materialization_id=materialization_id,
            cache_refreshed=changed,
        )

    def _claim(self, materialization_id: uuid.UUID | None) -> MaterializationClaim | None:
        with self._session_factory() as session, session.begin():
            now = _database_now(session)
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
        with self._session_factory() as session, session.begin():
            now = _database_now(session)
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

    def _record_failure(
        self,
        claim: MaterializationClaim,
        error: Exception,
    ) -> FailureDisposition:
        with self._session_factory() as session, session.begin():
            now = _database_now(session)
            materialization = session.scalar(
                select(TaskRevisionMaterialization)
                .where(TaskRevisionMaterialization.id == claim.materialization_id)
                .with_for_update(),
            )
            if not _claim_matches(materialization, claim):
                return FailureDisposition.LEASE_LOST
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
            return (
                FailureDisposition.FAILED
                if terminal
                else FailureDisposition.RETRY_SCHEDULED
            )

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
        activation: ActivationResult | None = None
        validation_phase = "ready_validation"
        try:
            with self._snapshot_store.pinned(ready.revision) as snapshot_authority:
                validation_phase = "activation_validation"
                self._inject("before_activation", ready.revision)
                _assert_pinned_snapshot_authority(snapshot_authority)
                activation = self._activate(
                    ready.revision.revision_id,
                    snapshot_authority,
                )
                self._inject("after_activation", ready.revision)
                _assert_pinned_snapshot_authority(snapshot_authority)
        except SnapshotValidationError as exc:
            self._record_ready_failure(
                ready.materialization_id,
                exc,
                activation=activation,
                phase=validation_phase,
            )
            return WorkerRunResult(
                action="failed",
                materialization_id=ready.materialization_id,
                error=_error_text(exc),
            )
        if activation is None:
            raise MaterializationError("activation completed without a result")
        cache_error: str | None = None
        try:
            cache_refreshed = self._refresh_cache(ready.revision.task_id)
        except Exception as exc:
            cache_refreshed = False
            cache_error = _error_text(exc)
            self._pending_cache_repairs.add(ready.revision.task_id)
            logger.exception(
                "task compatibility cache refresh failed after activation",
                extra={"task_id": str(ready.revision.task_id)},
            )
        else:
            self._pending_cache_repairs.discard(ready.revision.task_id)
        return WorkerRunResult(
            action=action,
            materialization_id=ready.materialization_id,
            activation_state=activation.state,
            cache_refreshed=cache_refreshed,
            error=cache_error,
        )

    def _record_ready_failure(
        self,
        materialization_id: uuid.UUID,
        error: SnapshotValidationError,
        *,
        activation: ActivationResult | None = None,
        phase: str = "ready_validation",
    ) -> None:
        with self._session_factory() as lookup_session:
            failed_snapshot = lookup_session.get(TaskRevisionMaterialization, materialization_id)
            if failed_snapshot is None:
                return
            task_snapshot = lookup_session.get(Task, failed_snapshot.task_id)
            previous_revision = (
                lookup_session.get(TaskRevision, activation.previous_revision_id)
                if activation is not None
                and activation.changed
                and activation.previous_revision_id is not None
                else None
            )
            previous_descriptor = (
                _revision_snapshot(task_snapshot, previous_revision)
                if task_snapshot is not None
                and previous_revision is not None
                and previous_revision.task_id == task_snapshot.id
                and previous_revision.workspace_id == task_snapshot.workspace_id
                else None
            )

        previous_authority: PinnedSnapshotAuthority | None = None
        previous_validation_error: SnapshotValidationError | None = None
        restored_revision_id: uuid.UUID | None = None
        fallback_reason = "not_requested"
        post_commit_error: SnapshotValidationError | None = None
        try:
            with ExitStack() as authority_stack:
                if activation is not None and activation.changed:
                    if activation.previous_revision_id is None:
                        fallback_reason = "no_previous_revision"
                    elif previous_descriptor is None:
                        fallback_reason = "previous_revision_missing"
                        previous_validation_error = SnapshotValidationError(
                            "previous revision snapshot descriptor is unavailable",
                        )
                    else:
                        try:
                            previous_authority = authority_stack.enter_context(
                                self._snapshot_store.pinned(previous_descriptor),
                            )
                        except SnapshotValidationError as exc:
                            previous_validation_error = exc
                            fallback_reason = "previous_snapshot_invalid"

                with self._session_factory() as session, session.begin():
                    task, materializations = self._lock_task_materializations(
                        session,
                        failed_snapshot.task_id,
                        tuple(
                            revision_id
                            for revision_id in (
                                failed_snapshot.revision_id,
                                activation.previous_revision_id if activation is not None else None,
                            )
                            if revision_id is not None
                        ),
                    )
                    materialization = materializations.get(failed_snapshot.revision_id)
                    if (
                        task is None
                        or materialization is None
                        or materialization.id != materialization_id
                        or materialization.state != TaskMaterializationState.SUCCEEDED
                    ):
                        return

                    activation_reverted = task.current_revision_id == materialization.revision_id
                    if activation_reverted:
                        previous_materialization = (
                            materializations.get(activation.previous_revision_id)
                            if activation is not None
                            and activation.previous_revision_id is not None
                            else None
                        )
                        if activation is None or not activation.changed:
                            fallback_reason = "no_proven_previous_revision"
                        elif (
                            previous_materialization is not None
                            and previous_materialization.state == TaskMaterializationState.SUCCEEDED
                            and previous_authority is not None
                        ):
                            try:
                                _assert_pinned_snapshot_authority(previous_authority)
                            except SnapshotValidationError as exc:
                                previous_validation_error = exc
                                fallback_reason = "previous_snapshot_invalid"
                            else:
                                restored_revision_id = activation.previous_revision_id
                                fallback_reason = "previous_valid"
                        elif previous_materialization is None:
                            fallback_reason = "previous_materialization_missing"
                        elif previous_materialization.state != TaskMaterializationState.SUCCEEDED:
                            fallback_reason = "previous_not_succeeded"

                        if (
                            previous_materialization is not None
                            and previous_materialization.state == TaskMaterializationState.SUCCEEDED
                            and restored_revision_id is None
                            and previous_validation_error is not None
                        ):
                            self._mark_locked_materialization_failed(
                                session,
                                previous_materialization,
                                previous_validation_error,
                                phase="fallback_validation",
                                details={"current_cleared": True},
                            )
                        task.current_revision_id = restored_revision_id

                    self._mark_locked_materialization_failed(
                        session,
                        materialization,
                        error,
                        phase=phase,
                        details={
                            "activation_reverted": activation_reverted,
                            "restored_revision_id": (
                                str(restored_revision_id)
                                if restored_revision_id is not None
                                else None
                            ),
                            "fallback_reason": fallback_reason,
                        },
                    )

                if restored_revision_id is not None and previous_authority is not None:
                    _assert_pinned_snapshot_authority(previous_authority)
        except SnapshotValidationError as exc:
            post_commit_error = exc

        if post_commit_error is not None and restored_revision_id is not None:
            self._clear_invalid_restored_current(
                failed_snapshot.task_id,
                restored_revision_id,
                post_commit_error,
            )

    def _lock_task_materializations(
        self,
        session: Session,
        task_id: uuid.UUID,
        revision_ids: tuple[uuid.UUID, ...],
    ) -> tuple[Task | None, dict[uuid.UUID, TaskRevisionMaterialization]]:
        task = session.scalar(select(Task).where(Task.id == task_id).with_for_update())
        if task is None:
            return None, {}
        materializations = tuple(
            session.scalars(
                select(TaskRevisionMaterialization)
                .where(
                    TaskRevisionMaterialization.task_id == task_id,
                    TaskRevisionMaterialization.revision_id.in_(set(revision_ids)),
                )
                .order_by(TaskRevisionMaterialization.id)
                .with_for_update()
                .execution_options(populate_existing=True),
            ).all(),
        )
        return task, {item.revision_id: item for item in materializations}

    def _mark_locked_materialization_failed(
        self,
        session: Session,
        materialization: TaskRevisionMaterialization,
        error: BaseException,
        *,
        phase: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        materialization.state = TaskMaterializationState.FAILED
        materialization.last_error = _error_text(error)
        event_details = {
            "task_id": str(materialization.task_id),
            "revision_id": str(materialization.revision_id),
            "attempt_count": materialization.attempt_count,
            "worker_id": self.worker_id,
            "error": materialization.last_error,
            "phase": phase,
        }
        if details is not None:
            event_details.update(details)
        append_audit_event(
            session,
            workspace_id=materialization.workspace_id,
            event_type="task.revision_materialization_failed",
            actor=None,
            caller=None,
            channel=AuditChannel.WORKER,
            resource_type="task_revision_materialization",
            resource_id=materialization.id,
            details=event_details,
        )

    def _clear_invalid_restored_current(
        self,
        task_id: uuid.UUID,
        revision_id: uuid.UUID,
        error: SnapshotValidationError,
    ) -> None:
        with self._session_factory() as session, session.begin():
            task, materializations = self._lock_task_materializations(
                session,
                task_id,
                (revision_id,),
            )
            if task is None:
                return
            current_cleared = task.current_revision_id == revision_id
            if current_cleared:
                task.current_revision_id = None
            materialization = materializations.get(revision_id)
            if (
                materialization is not None
                and materialization.state == TaskMaterializationState.SUCCEEDED
            ):
                self._mark_locked_materialization_failed(
                    session,
                    materialization,
                    error,
                    phase="fallback_validation",
                    details={"current_cleared": current_cleared},
                )

    def _activate(
        self,
        revision_id: uuid.UUID,
        snapshot_authority: PinnedSnapshotAuthority,
    ) -> ActivationResult:
        with self._session_factory() as session, session.begin():
            revision = session.get(TaskRevision, revision_id)
            if revision is None:
                raise MaterializationError("revision is not ready for activation")
            task, materializations = self._lock_task_materializations(
                session,
                revision.task_id,
                (revision_id,),
            )
            materialization = materializations.get(revision_id)
            if (
                task is None
                or task.workspace_id != revision.workspace_id
                or materialization is None
                or materialization.state != TaskMaterializationState.SUCCEEDED
            ):
                raise MaterializationError("revision task disappeared before activation")
            _assert_pinned_snapshot_authority(snapshot_authority)

            current = session.get(TaskRevision, task.current_revision_id) if task.current_revision_id else None
            current_number = current.revision_number if current is not None else None
            if task.current_revision_id == revision.id:
                _assert_pinned_snapshot_authority(snapshot_authority)
                return ActivationResult(
                    state="active",
                    changed=False,
                    revision_id=revision.id,
                    current_revision_id=revision.id,
                    current_revision_number=revision.revision_number,
                )
            if current_number is not None and current_number >= revision.revision_number:
                _assert_pinned_snapshot_authority(snapshot_authority)
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
            _assert_pinned_snapshot_authority(snapshot_authority)
            return ActivationResult(
                state="active",
                changed=True,
                revision_id=revision.id,
                current_revision_id=revision.id,
                current_revision_number=revision.revision_number,
                previous_revision_id=previous_revision_id,
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
        parts.extend(item for item in (part.strip() for part in chunk.split(",")) if item)
    if not parts:
        raise ValueError("tasks_root must contain at least one non-empty path")
    return Path(parts[-1])


@contextmanager
def _pinned_root_directory(root: Path, *, description: str, create: bool = True):
    absolute = Path(os.path.abspath(os.fspath(root)))
    components = absolute.parts[1:]
    if not components:
        raise SnapshotValidationError(f"{description} cannot be the filesystem root")

    try:
        current_fd = os.open(absolute.anchor, _directory_open_flags())
    except OSError as exc:
        raise SnapshotValidationError(
            f"{description} anchor is not a safe directory: {absolute.anchor}",
        ) from exc

    descriptors = [current_fd]
    path_entries: list[PinnedDirectoryEntry] = []
    try:
        current_path = Path(absolute.anchor)
        for index, component in enumerate(components):
            current_path /= component
            component_description = (
                f"{description}: {absolute}"
                if index == len(components) - 1
                else f"{description} path component: {current_path}"
            )
            next_fd = (
                _open_or_create_directory_at(
                    current_fd,
                    component,
                    description=component_description,
                )
                if create
                else _open_existing_directory_at(
                    current_fd,
                    component,
                    description=component_description,
                )
            )
            descriptors.append(next_fd)
            opened = os.fstat(next_fd)
            path_entries.append(
                PinnedDirectoryEntry(
                    parent_fd=current_fd,
                    fd=next_fd,
                    name=component,
                    device=opened.st_dev,
                    inode=opened.st_ino,
                    description=component_description,
                ),
            )
            current_fd = next_fd

        root_entry = path_entries[-1]
        pinned = PinnedRootDirectory(
            parent_fd=root_entry.parent_fd,
            root_fd=root_entry.fd,
            basename=root_entry.name,
            path=absolute,
            device=root_entry.device,
            inode=root_entry.inode,
            description=description,
            path_entries=tuple(path_entries),
        )
        _assert_pinned_root(pinned)
        try:
            yield pinned
        finally:
            _assert_pinned_root(pinned)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _pinned_directory_at(
    parent_fd: int,
    name: str,
    *,
    description: str,
    create: bool = True,
):
    descriptor = (
        _open_or_create_directory_at(parent_fd, name, description=description)
        if create
        else _open_existing_directory_at(parent_fd, name, description=description)
    )
    try:
        opened = os.fstat(descriptor)
        pinned = PinnedDirectoryEntry(
            parent_fd=parent_fd,
            fd=descriptor,
            name=name,
            device=opened.st_dev,
            inode=opened.st_ino,
            description=description,
        )
        _assert_pinned_directory(pinned)
        yield pinned
        _assert_pinned_directory(pinned)
    finally:
        os.close(descriptor)


def _create_pinned_directory_at(
    parent_fd: int,
    name: str,
    *,
    description: str,
    mode: int,
) -> PinnedDirectoryEntry:
    _require_safe_segment(name)
    try:
        os.mkdir(name, mode=mode, dir_fd=parent_fd)
        os.fsync(parent_fd)
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise SnapshotValidationError(f"{description} could not be created safely") from exc
    try:
        opened = os.fstat(descriptor)
        pinned = PinnedDirectoryEntry(
            parent_fd=parent_fd,
            fd=descriptor,
            name=name,
            device=opened.st_dev,
            inode=opened.st_ino,
            description=description,
        )
        _assert_pinned_directory(pinned)
        return pinned
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def _pinned_regular_file_at(parent_fd: int, name: str, *, description: str):
    flags = (
        os.O_RDWR
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    created = False
    try:
        descriptor = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise SnapshotValidationError(f"{description} is not a safe regular file") from exc
    except OSError as exc:
        raise SnapshotValidationError(f"{description} could not be created safely") from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SnapshotValidationError(f"{description} is not a regular file")
        if created:
            os.fsync(descriptor)
            os.fsync(parent_fd)
        pinned = PinnedRegularFile(
            parent_fd=parent_fd,
            fd=descriptor,
            name=name,
            device=opened.st_dev,
            inode=opened.st_ino,
            description=description,
        )
        _assert_pinned_regular_file(pinned)
        yield pinned
        _assert_pinned_regular_file(pinned)
    finally:
        os.close(descriptor)


@contextmanager
def _pinned_existing_regular_file_at(parent_fd: int, name: str, *, description: str):
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise SnapshotValidationError(f"{description} is not a safe regular file") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SnapshotValidationError(f"{description} is not a regular file")
        pinned = PinnedRegularFile(
            parent_fd=parent_fd,
            fd=descriptor,
            name=name,
            device=opened.st_dev,
            inode=opened.st_ino,
            description=description,
        )
        _assert_pinned_regular_file(pinned)
        yield pinned
        _assert_pinned_regular_file(pinned)
    finally:
        os.close(descriptor)


@contextmanager
def _pinned_cache_directory(
    root: PinnedRootDirectory,
    lock: PinnedFileLock,
    task_key: str,
):
    _require_safe_segment(task_key)
    _assert_pinned_root(root)
    _assert_pinned_file_lock(lock)
    with _pinned_directory_at(
        root.root_fd,
        task_key,
        description=f"compatibility cache target: {root.path / task_key}",
    ) as task:
        directory = PinnedCacheDirectory(
            root=root,
            lock=lock,
            task_fd=task.fd,
            task_key=task_key,
            device=task.device,
            inode=task.inode,
        )
        _assert_pinned_cache_directory(directory)
        yield directory
        _assert_pinned_cache_directory(directory)


def _open_or_create_directory_at(parent_fd: int, name: str, *, description: str) -> int:
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SnapshotValidationError(f"{description} could not be created safely") from exc
        else:
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise SnapshotValidationError(
                    f"{description} parent directory could not be synced",
                ) from exc
        try:
            return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise SnapshotValidationError(f"{description} is not a safe directory") from exc
    except OSError as exc:
        raise SnapshotValidationError(f"{description} is not a safe directory") from exc


def _open_existing_directory_at(parent_fd: int, name: str, *, description: str) -> int:
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SnapshotValidationError(f"{description} is not a safe directory") from exc


def _directory_open_flags() -> int:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotValidationError("secure directory writes require O_DIRECTORY and O_NOFOLLOW")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _assert_pinned_root(root: PinnedRootDirectory) -> None:
    try:
        for component in root.path_entries:
            entry = os.stat(
                component.name,
                dir_fd=component.parent_fd,
                follow_symlinks=False,
            )
            opened = os.fstat(component.fd)
            expected = (component.device, component.inode)
            if (
                not stat.S_ISDIR(entry.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (entry.st_dev, entry.st_ino) != expected
                or (opened.st_dev, opened.st_ino) != expected
            ):
                raise SnapshotValidationError(
                    f"{root.description} changed during operation: {root.path}",
                )
    except OSError as exc:
        raise SnapshotValidationError(
            f"{root.description} changed during operation: {root.path}",
        ) from exc


def _assert_pinned_directory(directory: PinnedDirectoryEntry) -> None:
    try:
        entry = os.stat(
            directory.name,
            dir_fd=directory.parent_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(directory.fd)
    except OSError as exc:
        raise SnapshotValidationError(f"{directory.description} changed during operation") from exc
    expected = (directory.device, directory.inode)
    if (
        not stat.S_ISDIR(entry.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (entry.st_dev, entry.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise SnapshotValidationError(f"{directory.description} changed during operation")


def _assert_pinned_regular_file(file: PinnedRegularFile) -> None:
    try:
        entry = os.stat(
            file.name,
            dir_fd=file.parent_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(file.fd)
    except OSError as exc:
        raise SnapshotValidationError(f"{file.description} changed during operation") from exc
    expected = (file.device, file.inode)
    if (
        not stat.S_ISREG(entry.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or (entry.st_dev, entry.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise SnapshotValidationError(f"{file.description} changed during operation")


@contextmanager
def _pinned_file_lock(
    root: PinnedRootDirectory,
    *,
    directories: tuple[tuple[str, str], ...],
    filename: str,
    file_description: str,
    after_acquired: Callable[[], None] | None = None,
):
    _assert_pinned_root(root)
    with ExitStack() as stack:
        parent_fd = root.root_fd
        pinned_directories: list[PinnedDirectoryEntry] = []
        for name, description in directories:
            directory = stack.enter_context(
                _pinned_directory_at(parent_fd, name, description=description),
            )
            pinned_directories.append(directory)
            parent_fd = directory.fd
            _assert_pinned_lock_path(root, pinned_directories)
        lock_file = stack.enter_context(
            _pinned_regular_file_at(
                parent_fd,
                filename,
                description=file_description,
            ),
        )
        lock = PinnedFileLock(
            root=root,
            directories=tuple(pinned_directories),
            file=lock_file,
        )
        _assert_pinned_file_lock(lock)
        locked = False
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fd, fcntl.LOCK_EX)
                locked = True
            if after_acquired is not None:
                after_acquired()
            _assert_pinned_file_lock(lock)
            yield lock
        finally:
            try:
                _assert_pinned_file_lock(lock)
            finally:
                if locked:
                    fcntl.flock(lock_file.fd, fcntl.LOCK_UN)


def _assert_pinned_lock_path(
    root: PinnedRootDirectory,
    directories: tuple[PinnedDirectoryEntry, ...] | list[PinnedDirectoryEntry],
    lock_file: PinnedRegularFile | None = None,
) -> None:
    _assert_pinned_root(root)
    for directory in directories:
        _assert_pinned_directory(directory)
    if lock_file is not None:
        _assert_pinned_regular_file(lock_file)


def _assert_pinned_file_lock(lock: PinnedFileLock) -> None:
    _assert_pinned_lock_path(lock.root, lock.directories, lock.file)


def _assert_snapshot_path(
    root: PinnedRootDirectory,
    directories: tuple[PinnedDirectoryEntry, ...],
    lock: PinnedFileLock | None = None,
) -> None:
    _assert_pinned_root(root)
    for directory in directories:
        _assert_pinned_directory(directory)
    if lock is not None:
        _assert_pinned_file_lock(lock)


def _require_snapshot_entries(
    root: PinnedRootDirectory,
    path_entries: tuple[PinnedDirectoryEntry, ...],
    directory: PinnedDirectoryEntry,
    path: Path,
    *,
    lock: PinnedFileLock | None = None,
) -> None:
    _assert_snapshot_path(root, path_entries, lock)
    try:
        entries = set(os.listdir(directory.fd))
    except OSError as exc:
        raise SnapshotConflictError(f"snapshot directory cannot be listed: {path}") from exc
    if entries != SNAPSHOT_FILES:
        raise SnapshotConflictError(
            f"snapshot files differ: expected={sorted(SNAPSHOT_FILES)}, actual={sorted(entries)}",
        )
    _assert_snapshot_path(root, path_entries, lock)


def _assert_pinned_snapshot_authority(authority: PinnedSnapshotAuthority) -> None:
    target_directory = authority.directories[-1]
    _require_snapshot_entries(
        authority.root,
        authority.directories,
        target_directory,
        authority.validated.ref.path,
    )
    for file, expected_bytes in authority.files:
        actual = _read_pinned_regular_file_exact(file, len(expected_bytes))
        if actual != expected_bytes:
            raise SnapshotConflictError(
                f"snapshot file content differs: {authority.validated.ref.path / file.name}",
            )
        _assert_snapshot_path(authority.root, authority.directories)


def _read_pinned_regular_file_exact(file: PinnedRegularFile, expected_size: int) -> bytes:
    _assert_pinned_regular_file(file)
    try:
        opened = os.fstat(file.fd)
        if opened.st_size != expected_size:
            raise SnapshotValidationError(
                f"{file.description} size differs: expected={expected_size}, actual={opened.st_size}",
            )
        os.lseek(file.fd, 0, os.SEEK_SET)
        remaining = expected_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(file.fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SnapshotValidationError(f"{file.description} ended before expected size")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file.fd, 1):
            raise SnapshotValidationError(f"{file.description} exceeds expected size")
        final = os.fstat(file.fd)
    except OSError as exc:
        raise SnapshotValidationError(f"{file.description} could not be read safely") from exc
    if final.st_size != expected_size:
        raise SnapshotValidationError(
            f"{file.description} size changed during read: expected={expected_size}, actual={final.st_size}",
        )
    _assert_pinned_regular_file(file)
    return b"".join(chunks)


def _write_new_regular_file_at(
    root: PinnedRootDirectory,
    path_entries: tuple[PinnedDirectoryEntry, ...],
    directory: PinnedDirectoryEntry,
    name: str,
    value: bytes,
    *,
    description: str,
    lock: PinnedFileLock | None = None,
) -> None:
    _require_safe_segment(name)
    _assert_snapshot_path(root, path_entries, lock)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o644, dir_fd=directory.fd)
    except OSError as exc:
        raise SnapshotValidationError(f"{description} could not be created safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SnapshotValidationError(f"{description} is not a regular file")
        pinned = PinnedRegularFile(
            parent_fd=directory.fd,
            fd=descriptor,
            name=name,
            device=opened.st_dev,
            inode=opened.st_ino,
            description=description,
        )
        _assert_pinned_regular_file(pinned)
        _assert_snapshot_path(root, path_entries, lock)
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("task snapshot write made no progress")
            remaining = remaining[written:]
        _assert_pinned_regular_file(pinned)
        _assert_snapshot_path(root, path_entries, lock)
        os.fsync(descriptor)
        _assert_pinned_regular_file(pinned)
        _assert_snapshot_path(root, path_entries, lock)
    finally:
        os.close(descriptor)


def _read_snapshot_regular_file_at(
    root: PinnedRootDirectory,
    path_entries: tuple[PinnedDirectoryEntry, ...],
    directory: PinnedDirectoryEntry,
    name: str,
    *,
    description: str,
    expected_size: int,
    lock: PinnedFileLock | None = None,
) -> bytes:
    _assert_snapshot_path(root, path_entries, lock)
    try:
        with _pinned_existing_regular_file_at(
            directory.fd,
            name,
            description=description,
        ) as file:
            _assert_snapshot_path(root, path_entries, lock)
            value = _read_pinned_regular_file_exact(file, expected_size)
            _assert_snapshot_path(root, path_entries, lock)
            return value
    except SnapshotValidationError as exc:
        raise SnapshotConflictError(f"snapshot file is missing or not regular: {description}") from exc


def _cleanup_staging_directory(
    root: PinnedRootDirectory,
    staging_root: PinnedDirectoryEntry,
    staging: PinnedDirectoryEntry,
    *,
    lock: PinnedFileLock | None = None,
) -> None:
    _assert_snapshot_path(root, (staging_root, staging), lock)
    entries = set(os.listdir(staging.fd))
    unexpected = entries - SNAPSHOT_FILES
    if unexpected:
        raise SnapshotValidationError(
            f"task snapshot staging directory contains unexpected entries: {sorted(unexpected)}",
        )
    for name in entries:
        entry = os.stat(name, dir_fd=staging.fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            raise SnapshotValidationError(
                f"task snapshot staging entry is an unexpected directory: {name}",
            )
        os.unlink(name, dir_fd=staging.fd)
    _assert_snapshot_path(root, (staging_root, staging), lock)
    os.fsync(staging.fd)
    _assert_snapshot_path(root, (staging_root, staging), lock)
    os.rmdir(staging.name, dir_fd=staging_root.fd)
    _assert_snapshot_path(root, (staging_root,), lock)
    os.fsync(staging_root.fd)
    _assert_snapshot_path(root, (staging_root,), lock)


def _rename_directory_noreplace_at(
    source_parent_fd: int,
    source_name: str,
    target_parent_fd: int,
    target_name: str,
) -> None:
    _require_safe_segment(source_name)
    _require_safe_segment(target_name)
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise SnapshotValidationError("atomic no-replace snapshot commit is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_parent_fd,
        os.fsencode(source_name),
        target_parent_fd,
        os.fsencode(target_name),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), target_name)
    raise OSError(error_number, os.strerror(error_number), target_name)


def _assert_pinned_cache_directory(directory: PinnedCacheDirectory) -> None:
    _assert_pinned_file_lock(directory.lock)
    _assert_pinned_root(directory.root)
    try:
        entry = os.stat(
            directory.task_key,
            dir_fd=directory.root.root_fd,
            follow_symlinks=False,
        )
        opened = os.fstat(directory.task_fd)
    except OSError as exc:
        raise SnapshotValidationError("compatibility cache directory was removed during write") from exc
    expected = (directory.device, directory.inode)
    if (
        not stat.S_ISDIR(entry.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (entry.st_dev, entry.st_ino) != expected
        or (opened.st_dev, opened.st_ino) != expected
    ):
        raise SnapshotValidationError("compatibility cache directory changed during write")


def _cache_matches_at(
    directory: PinnedCacheDirectory,
    metadata: dict[str, Any],
    metadata_bytes: bytes,
    task_bytes: bytes,
) -> bool:
    try:
        _assert_pinned_cache_directory(directory)
        cached_metadata = _read_regular_file_at(
            directory,
            CACHE_METADATA_FILE,
            expected_size=len(metadata_bytes),
        )
        cached_task = _read_regular_file_at(
            directory,
            "task.yaml",
            expected_size=len(task_bytes),
        )
        stored = json.loads(cached_metadata.decode("utf-8"))
        _assert_pinned_cache_directory(directory)
        return stored == metadata and cached_task == task_bytes
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
        return False


def _read_regular_file_at(
    directory: PinnedCacheDirectory,
    name: str,
    *,
    expected_size: int,
) -> bytes:
    _assert_pinned_cache_directory(directory)
    with _pinned_existing_regular_file_at(
        directory.task_fd,
        name,
        description=f"compatibility cache file: {name}",
    ) as file:
        value = _read_pinned_regular_file_exact(file, expected_size)
        _assert_pinned_cache_directory(directory)
        return value


def _write_bytes_atomic_at(
    directory: PinnedCacheDirectory,
    name: str,
    value: bytes,
) -> None:
    _assert_pinned_cache_directory(directory)
    _require_regular_or_missing_at(directory, name)
    temporary = f".{name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory.task_fd)
        opened = os.fstat(descriptor)
        temporary_file = PinnedRegularFile(
            parent_fd=directory.task_fd,
            fd=descriptor,
            name=temporary,
            device=opened.st_dev,
            inode=opened.st_ino,
            description=f"compatibility cache temporary file: {temporary}",
        )
        _assert_pinned_regular_file(temporary_file)
        remaining = memoryview(value)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("compatibility cache write made no progress")
            remaining = remaining[written:]
        _assert_pinned_cache_directory(directory)
        os.fsync(descriptor)
        _assert_pinned_regular_file(temporary_file)
        _assert_pinned_cache_directory(directory)
        _require_regular_or_missing_at(directory, name)
        _assert_pinned_regular_file(temporary_file)
        _assert_pinned_cache_directory(directory)
        os.replace(
            temporary,
            name,
            src_dir_fd=directory.task_fd,
            dst_dir_fd=directory.task_fd,
        )
        committed_file = PinnedRegularFile(
            parent_fd=directory.task_fd,
            fd=descriptor,
            name=name,
            device=temporary_file.device,
            inode=temporary_file.inode,
            description=f"compatibility cache file: {name}",
        )
        _assert_pinned_regular_file(committed_file)
        _assert_pinned_cache_directory(directory)
        os.fsync(directory.task_fd)
        _assert_pinned_regular_file(committed_file)
        _assert_pinned_cache_directory(directory)
        os.close(descriptor)
        descriptor = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory.task_fd)
        except FileNotFoundError:
            pass


def _require_regular_or_missing_at(directory: PinnedCacheDirectory, name: str) -> None:
    _assert_pinned_cache_directory(directory)
    try:
        file_stat = os.stat(name, dir_fd=directory.task_fd, follow_symlinks=False)
    except FileNotFoundError:
        file_stat = None
    if file_stat is not None and not stat.S_ISREG(file_stat.st_mode):
        raise SnapshotValidationError(f"compatibility cache file is not regular: {name}")
    _assert_pinned_cache_directory(directory)


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


def _database_now(session: Session) -> datetime:
    value = session.scalar(select(func.current_timestamp()))
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MaterializationError("database clock returned an invalid timestamp") from exc
    if not isinstance(value, datetime):
        raise MaterializationError("database clock did not return a timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
