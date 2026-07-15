from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest
import yaml
from alembic import command
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

import llm_labeling_scaffold.db.materialization as materialization_module
from llm_labeling_scaffold.config import load_task
from llm_labeling_scaffold.db import AuditChannel, DatabaseService, ExternalIdentity
from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.enums import TaskMaterializationState
from llm_labeling_scaffold.db.materialization import (
    CACHE_METADATA_FILE,
    SNAPSHOT_FILES,
    ControlTaskSnapshotLoader,
    MaterializationCrash,
    MaterializationLeaseLost,
    SnapshotValidationError,
    TaskMaterializationWorker,
)
from llm_labeling_scaffold.db.migration import build_alembic_config
from llm_labeling_scaffold.db.models import (
    AuditEvent,
    IdempotencyRecord,
    Task,
    TaskRevision,
    TaskRevisionMaterialization,
)


def _task_definition(task_key: str, marker: str) -> dict:
    return {
        "task_id": task_key,
        "id_field": "record_id",
        "input": {
            "path": "raw/input.jsonl",
            "text_fields": ["text"],
            "metadata_fields": [],
        },
        "labels": {
            "primary": {
                "name": "label",
                "type": "categorical",
                "values": ["yes", "no"],
            },
        },
        "marker": marker,
    }


def _render_task(definition: dict) -> str:
    return yaml.safe_dump(definition, allow_unicode=True, sort_keys=False)


@pytest.fixture
def materialization_env(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'materialization.db'}"
    engine = create_database_engine(
        database_url,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    identity = ExternalIdentity("https://materialization.example", "admin")
    with Session(engine) as session:
        bootstrap = bootstrap_admin(
            session,
            issuer=identity.issuer,
            subject=identity.subject,
            workspace_slug="workspace",
            workspace_name="Workspace",
        )
    with Session(engine) as session, session.begin():
        task = Task(
            workspace_id=bootstrap.workspace_id,
            task_key="controlled-task",
            name="Controlled Task",
            created_by_principal_id=bootstrap.principal_id,
        )
        session.add(task)
        session.flush()
        task_id = task.id

    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    service = DatabaseService(factory)
    try:
        yield {
            "engine": engine,
            "factory": factory,
            "service": service,
            "identity": identity,
            "task_id": task_id,
            "runs_root": tmp_path / "runs",
            "tasks_root": tmp_path / "tasks",
        }
    finally:
        engine.dispose()


def _publish(env: dict, marker: str, key: str, if_match: str = "*"):
    definition = _task_definition("controlled-task", marker)
    draft = env["service"].save_task_draft(
        actor_identity=env["identity"],
        caller_identity=env["identity"],
        workspace_slug="workspace",
        task_key="controlled-task",
        definition=definition,
        rendered_task=_render_task(definition),
        if_match=if_match,
        channel=AuditChannel.API,
    ).draft
    published = env["service"].publish_task_draft(
        actor_identity=env["identity"],
        caller_identity=env["identity"],
        workspace_slug="workspace",
        task_key="controlled-task",
        if_match=draft.etag,
        reason=f"Publish {marker}",
        idempotency_key=key,
        channel=AuditChannel.API,
    )
    return draft, published


def _worker(env: dict, **kwargs) -> TaskMaterializationWorker:
    return TaskMaterializationWorker(
        env["factory"],
        runs_root=env["runs_root"],
        tasks_root=env["tasks_root"],
        poll_seconds=0.01,
        retry_delay_seconds=0,
        **kwargs,
    )


def _snapshot_root(env: dict) -> Path:
    return env["runs_root"] / "_system" / "task_control" / "task_snapshots"


def _expire_lease(env: dict, materialization_id: uuid.UUID) -> None:
    with Session(env["engine"]) as session, session.begin():
        materialization = session.get(TaskRevisionMaterialization, materialization_id)
        materialization.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_publish_202_status_loader_and_compatibility_cache_are_separate(materialization_env):
    env = materialization_env
    logical_task_path = env["tasks_root"] / "controlled-task" / "task.yaml"
    relative_input = logical_task_path.parent / "raw" / "input.jsonl"
    relative_input.parent.mkdir(parents=True)
    relative_input.write_text('{"record_id":"1","text":"example"}\n', encoding="utf-8")
    logical_task_path.write_text(
        _render_task(_task_definition("controlled-task", "legacy")),
        encoding="utf-8",
    )
    legacy_input_path = load_task(logical_task_path).input_path
    _draft, published = _publish(env, "v1", "publish-v1")
    worker = _worker(env, worker_id="worker-happy")

    assert published.response_status == 202
    assert published.materialization_state == TaskMaterializationState.PENDING
    initial = worker.status(published.materialization_id)
    assert initial.state == TaskMaterializationState.PENDING
    assert initial.terminal is False

    final = worker.drain(published.materialization_id, timeout_seconds=1)
    assert final.state == TaskMaterializationState.SUCCEEDED
    assert final.activation_state == "active"
    assert final.snapshot_valid is True
    assert final.terminal is True
    assert final.snapshot_path.parts[-2:] == (
        str(published.revision_id),
        published.content_hash,
    )

    loader = ControlTaskSnapshotLoader(
        env["factory"],
        env["runs_root"],
        env["tasks_root"],
    )
    loaded = loader.load("workspace", "controlled-task")
    assert loaded.path == logical_task_path
    assert loaded.raw["marker"] == "v1"
    assert loaded.input_path == legacy_input_path == relative_input.resolve()
    assert relative_input.exists()

    cache_path = logical_task_path
    metadata_path = cache_path.parent / CACHE_METADATA_FILE
    assert cache_path.exists()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["revision_id"] == str(published.revision_id)
    cache_path.write_text(_render_task(_task_definition("controlled-task", "stale-cache")), encoding="utf-8")
    reloaded = loader.load("workspace", "controlled-task")
    assert reloaded.raw["marker"] == "v1"
    assert reloaded.input_path == legacy_input_path

    reconciled = worker.run_once(published.materialization_id)
    assert reconciled.action == "cache_reconciled"
    assert reconciled.cache_refreshed is True
    assert yaml.safe_load(cache_path.read_text(encoding="utf-8"))["marker"] == "v1"
    with Session(env["engine"]) as session:
        revision = session.get(TaskRevision, published.revision_id)
        claim = session.get(IdempotencyRecord, revision.idempotency_record_id)
        task = session.get(Task, env["task_id"])
        assert task.current_revision_id == published.revision_id
        assert claim.response_status == 202
        assert claim.response_body["materialization_state"] == "pending"
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "task.revision_activated",
                AuditEvent.resource_id == str(published.revision_id),
            ),
        ) == 1


def test_half_written_staging_is_invisible_and_expired_lease_recovers(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "half-write", "publish-half-write")
    crashed = False

    def inject(point, revision):
        nonlocal crashed
        if point == "after_task_write" and not crashed:
            crashed = True
            raise MaterializationCrash("half file")

    worker = _worker(env, worker_id="worker-crash", fault_injector=inject, lease_seconds=30)
    with pytest.raises(MaterializationCrash):
        worker.run_once(published.materialization_id)

    crashed_status = worker.status(published.materialization_id)
    assert crashed_status.state == TaskMaterializationState.PROCESSING
    assert crashed_status.snapshot_exists is False
    staging_tasks = list(
        (env["runs_root"] / "_system" / "task_control" / "task_snapshots" / ".staging").glob(
            "*/task.yaml",
        ),
    )
    assert len(staging_tasks) == 1
    assert not (env["tasks_root"] / "controlled-task" / "task.yaml").exists()

    _expire_lease(env, published.materialization_id)
    recovery = _worker(env, worker_id="worker-recovery")
    recovered = recovery.run_once(published.materialization_id)
    assert recovered.action == "materialized"
    final = recovery.status(published.materialization_id)
    assert final.activation_state == "active"
    assert final.attempt_count == 2
    assert final.snapshot_valid is True


@pytest.mark.parametrize("link_kind", ["directory", "task", "metadata"])
def test_compatibility_cache_rejects_symlinks_and_recovers(materialization_env, link_kind: str):
    env = materialization_env
    target = env["tasks_root"] / "controlled-task"
    outside = env["tasks_root"].parent / f"outside-{link_kind}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if link_kind == "directory":
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    else:
        target.mkdir()
        outside.write_text("sentinel", encoding="utf-8")
        filename = "task.yaml" if link_kind == "task" else CACHE_METADATA_FILE
        (target / filename).symlink_to(outside)

    _draft, published = _publish(env, "symlink", f"publish-symlink-{link_kind}")
    worker = _worker(env, worker_id=f"worker-symlink-{link_kind}")
    result = worker.run_once(published.materialization_id)
    assert result.activation_state == "active"
    assert result.cache_refreshed is False
    assert "compatibility cache" in result.error
    assert worker.status(published.materialization_id).activation_state == "active"
    assert ControlTaskSnapshotLoader(
        env["factory"],
        env["runs_root"],
        env["tasks_root"],
    ).load("workspace", "controlled-task").raw["marker"] == "symlink"
    assert worker.run_once(published.materialization_id).action == "cache_reconcile_failed"

    if link_kind == "directory":
        assert list(outside.iterdir()) == []
        target.unlink()
        target.mkdir()
    else:
        assert outside.read_text(encoding="utf-8") == "sentinel"
        filename = "task.yaml" if link_kind == "task" else CACHE_METADATA_FILE
        other_filename = CACHE_METADATA_FILE if link_kind == "task" else "task.yaml"
        assert not (target / other_filename).exists()
        (target / filename).unlink()
    repaired = worker.run_once(published.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert not target.is_symlink()
    assert not (target / "task.yaml").is_symlink()
    assert not (target / CACHE_METADATA_FILE).is_symlink()


@pytest.mark.parametrize("link_kind", ["root", "ancestor", "locks", "lock_file"])
def test_compatibility_cache_rejects_root_and_lock_path_symlinks(
    materialization_env,
    link_kind: str,
):
    env = materialization_env
    configured_root = (
        env["tasks_root"].parent / "cache-parent" / "tasks"
        if link_kind == "ancestor"
        else env["tasks_root"]
    )
    outside = env["tasks_root"].parent / f"root-lock-outside-{link_kind}"
    outside.mkdir()
    expected_outside: list[str] = []

    if link_kind == "root":
        attacked_path = configured_root
        attacked_path.symlink_to(outside, target_is_directory=True)
    elif link_kind == "ancestor":
        attacked_path = configured_root.parent
        attacked_path.symlink_to(outside, target_is_directory=True)
    elif link_kind == "locks":
        configured_root.mkdir()
        attacked_path = configured_root / "_locks"
        attacked_path.symlink_to(outside, target_is_directory=True)
    else:
        lock_directory = configured_root / "_locks" / "task_materialization"
        lock_directory.mkdir(parents=True)
        sentinel = outside / "sentinel"
        sentinel.write_text("sentinel", encoding="utf-8")
        expected_outside = ["sentinel"]
        attacked_path = lock_directory / "controlled-task.lock"
        attacked_path.symlink_to(sentinel)

    _draft, published = _publish(env, f"root-lock-{link_kind}", f"publish-root-lock-{link_kind}")
    worker = TaskMaterializationWorker(
        env["factory"],
        runs_root=env["runs_root"],
        tasks_root=configured_root,
        worker_id=f"worker-root-lock-{link_kind}",
        poll_seconds=0.01,
        retry_delay_seconds=0,
    )
    result = worker.run_once(published.materialization_id)
    assert result.activation_state == "active"
    assert result.cache_refreshed is False
    assert "compatibility cache" in result.error or "lock file" in result.error
    assert sorted(path.name for path in outside.iterdir()) == expected_outside
    if link_kind == "lock_file":
        assert (outside / "sentinel").read_text(encoding="utf-8") == "sentinel"

    attacked_path.unlink()
    if link_kind == "ancestor":
        attacked_path.mkdir()
    repaired = worker.run_once(published.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert (configured_root / "controlled-task" / "task.yaml").is_file()
    assert (configured_root / "controlled-task" / CACHE_METADATA_FILE).is_file()


@pytest.mark.parametrize(
    "exchange_point",
    ["after_cache_lock_acquired", "after_cache_task_write"],
)
def test_cache_root_exchange_is_detected_during_lock_and_write(
    materialization_env,
    exchange_point: str,
):
    env = materialization_env
    root = env["tasks_root"]
    displaced = root.with_name(f"{root.name}.{exchange_point}.displaced")
    outside = root.with_name(f"{root.name}.{exchange_point}.outside")
    outside.mkdir()
    exchange_requested = Event()
    exchange_finished = Event()

    def exchange_root():
        assert exchange_requested.wait(timeout=10)
        root.rename(displaced)
        root.symlink_to(outside, target_is_directory=True)
        exchange_finished.set()

    def inject(point, revision):
        if point == exchange_point and not exchange_requested.is_set():
            exchange_requested.set()
            assert exchange_finished.wait(timeout=10)

    _draft, published = _publish(env, exchange_point, f"publish-{exchange_point}")
    worker = _worker(
        env,
        worker_id=f"worker-{exchange_point}",
        fault_injector=inject,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        exchange = executor.submit(exchange_root)
        result = worker.run_once(published.materialization_id)
        exchange.result(timeout=10)

    assert result.activation_state == "active"
    assert result.cache_refreshed is False
    assert "cache root changed" in result.error
    assert list(outside.iterdir()) == []
    lock_path = displaced / "_locks" / "task_materialization" / "controlled-task.lock"
    assert lock_path.is_file()
    task_path = displaced / "controlled-task" / "task.yaml"
    metadata_path = displaced / "controlled-task" / CACHE_METADATA_FILE
    if exchange_point == "after_cache_lock_acquired":
        assert not task_path.exists()
    else:
        assert task_path.is_file()
        assert not metadata_path.exists()

    root.unlink()
    displaced.rename(root)
    repaired = worker.run_once(published.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert (root / "controlled-task" / CACHE_METADATA_FILE).is_file()


def test_cache_locks_exchange_is_detected_while_lock_is_held(materialization_env):
    env = materialization_env
    root = env["tasks_root"]
    locks = root / "_locks"
    displaced = root / "_locks.displaced"
    outside = root.with_name("locks-exchange-outside")
    outside.mkdir()
    exchange_requested = Event()
    exchange_finished = Event()

    def exchange_locks():
        assert exchange_requested.wait(timeout=10)
        locks.rename(displaced)
        locks.symlink_to(outside, target_is_directory=True)
        exchange_finished.set()

    def inject(point, revision):
        if point == "after_cache_lock_acquired" and not exchange_requested.is_set():
            exchange_requested.set()
            assert exchange_finished.wait(timeout=10)

    _draft, published = _publish(env, "locks-exchange", "publish-locks-exchange")
    worker = _worker(
        env,
        worker_id="worker-locks-exchange",
        fault_injector=inject,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        exchange = executor.submit(exchange_locks)
        result = worker.run_once(published.materialization_id)
        exchange.result(timeout=10)

    assert result.activation_state == "active"
    assert result.cache_refreshed is False
    assert "lock directory changed" in result.error
    assert list(outside.iterdir()) == []
    assert (
        displaced / "task_materialization" / "controlled-task.lock"
    ).is_file()
    assert not (root / "controlled-task").exists()

    locks.unlink()
    displaced.rename(locks)
    repaired = worker.run_once(published.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert (root / "controlled-task" / CACHE_METADATA_FILE).is_file()


def test_cache_root_ancestor_exchange_is_detected_before_metadata_write(materialization_env):
    env = materialization_env
    configured_root = env["tasks_root"].parent / "cache-runtime-parent" / "tasks"
    ancestor = configured_root.parent
    displaced = ancestor.with_name("cache-runtime-parent.displaced")
    outside = ancestor.with_name("cache-runtime-parent.outside")
    outside.mkdir()
    exchanged = False

    def inject(point, revision):
        nonlocal exchanged
        if point == "after_cache_task_write" and not exchanged:
            exchanged = True
            ancestor.rename(displaced)
            ancestor.symlink_to(outside, target_is_directory=True)

    _draft, published = _publish(env, "cache-ancestor-exchange", "publish-cache-ancestor")
    worker = TaskMaterializationWorker(
        env["factory"],
        runs_root=env["runs_root"],
        tasks_root=configured_root,
        worker_id="worker-cache-ancestor-exchange",
        poll_seconds=0.01,
        retry_delay_seconds=0,
        fault_injector=inject,
    )
    result = worker.run_once(published.materialization_id)

    assert result.activation_state == "active"
    assert result.cache_refreshed is False
    assert "cache root changed" in result.error
    assert list(outside.iterdir()) == []
    displaced_task = displaced / "tasks" / "controlled-task"
    assert (displaced_task / "task.yaml").is_file()
    assert not (displaced_task / CACHE_METADATA_FILE).exists()

    ancestor.unlink()
    displaced.rename(ancestor)
    repaired = worker.run_once(published.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert (configured_root / "controlled-task" / CACHE_METADATA_FILE).is_file()


def test_cache_lock_file_exchange_is_detected_before_metadata_write(materialization_env):
    env = materialization_env
    root = env["tasks_root"]
    lock_path = root / "_locks" / "task_materialization" / "controlled-task.lock"
    displaced = lock_path.with_name("controlled-task.lock.displaced")
    sentinel = root.with_name("cache-lock-file-sentinel")
    sentinel.write_text("sentinel", encoding="utf-8")
    exchanged = False

    def inject(point, revision):
        nonlocal exchanged
        if point == "after_cache_task_write" and not exchanged:
            exchanged = True
            lock_path.rename(displaced)
            lock_path.symlink_to(sentinel)

    _draft, published = _publish(env, "cache-lock-file-exchange", "publish-cache-lock-file")
    worker = _worker(
        env,
        worker_id="worker-cache-lock-file-exchange",
        fault_injector=inject,
    )
    result = worker.run_once(published.materialization_id)

    assert result.activation_state == "active"
    assert result.cache_refreshed is False
    assert "lock file changed" in result.error
    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    target = root / "controlled-task"
    assert (target / "task.yaml").is_file()
    assert not (target / CACHE_METADATA_FILE).exists()

    lock_path.unlink()
    displaced.rename(lock_path)
    repaired = worker.run_once(published.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert (target / CACHE_METADATA_FILE).is_file()


def test_compatibility_cache_mismatch_after_crash_is_detected_and_recovered(materialization_env):
    env = materialization_env
    first_draft, first = _publish(env, "cache-v1", "publish-cache-v1")
    first_worker = _worker(env, worker_id="worker-cache-v1")
    first_worker.drain(first.materialization_id, timeout_seconds=1)
    metadata_path = env["tasks_root"] / "controlled-task" / CACHE_METADATA_FILE
    task_path = env["tasks_root"] / "controlled-task" / "task.yaml"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["revision_id"] == str(
        first.revision_id,
    )

    _second_draft, second = _publish(
        env,
        "cache-v2",
        "publish-cache-v2",
        if_match=first_draft.etag,
    )
    crashed = False

    def inject(point, revision):
        nonlocal crashed
        if point == "after_cache_task_write" and revision.revision_id == second.revision_id and not crashed:
            crashed = True
            raise MaterializationCrash("cache metadata not committed")

    crashing_worker = _worker(
        env,
        worker_id="worker-cache-crash",
        fault_injector=inject,
    )
    with pytest.raises(MaterializationCrash):
        crashing_worker.run_once(second.materialization_id)
    assert crashing_worker.status(second.materialization_id).activation_state == "active"
    assert yaml.safe_load(task_path.read_text(encoding="utf-8"))["marker"] == "cache-v2"
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["revision_id"] == str(
        first.revision_id,
    )
    loader = ControlTaskSnapshotLoader(
        env["factory"],
        env["runs_root"],
        env["tasks_root"],
    )
    assert loader.load("workspace", "controlled-task").raw["marker"] == "cache-v2"

    recovery = _worker(env, worker_id="worker-cache-recovery")
    repaired = recovery.run_once(second.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert json.loads(metadata_path.read_text(encoding="utf-8"))["revision_id"] == str(
        second.revision_id,
    )
    assert recovery.run_once(second.materialization_id).cache_refreshed is False


def test_caught_cache_failure_is_queued_and_repaired_on_next_worker_run(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "cache-retry", "publish-cache-retry")
    failed_once = False

    def inject(point, revision):
        nonlocal failed_once
        if point == "after_cache_task_write" and not failed_once:
            failed_once = True
            raise OSError("cache metadata write failed")

    worker = _worker(
        env,
        worker_id="worker-cache-retry",
        fault_injector=inject,
    )
    first = worker.run_once(published.materialization_id)
    assert first.activation_state == "active"
    assert first.cache_refreshed is False
    assert "cache metadata write failed" in first.error

    repaired = worker.run_once()
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    metadata = json.loads(
        (env["tasks_root"] / "controlled-task" / CACHE_METADATA_FILE).read_text(
            encoding="utf-8",
        ),
    )
    assert metadata["revision_id"] == str(published.revision_id)


def test_persistent_cache_repair_failure_does_not_block_outbox(materialization_env):
    env = materialization_env
    target = env["tasks_root"] / "controlled-task"
    outside = env["tasks_root"].parent / "persistent-cache-outside"
    target.parent.mkdir(parents=True)
    outside.mkdir()
    target.symlink_to(outside, target_is_directory=True)

    first_draft, first = _publish(env, "blocked-cache-v1", "publish-blocked-cache-v1")
    worker = _worker(env, worker_id="worker-persistent-cache")
    first_result = worker.run_once(first.materialization_id)
    assert first_result.activation_state == "active"
    assert first_result.cache_refreshed is False

    _second_draft, second = _publish(
        env,
        "blocked-cache-v2",
        "publish-blocked-cache-v2",
        if_match=first_draft.etag,
    )
    second_result = worker.run_once()
    assert second_result.action == "materialized"
    assert second_result.materialization_id == second.materialization_id
    assert second_result.activation_state == "active"
    assert second_result.cache_refreshed is False
    assert worker.status(second.materialization_id).terminal is True
    assert list(outside.iterdir()) == []


def test_cache_directory_exchange_is_detected_without_writing_through_symlink(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "directory-race", "publish-directory-race")
    target = env["tasks_root"] / "controlled-task"
    displaced = env["tasks_root"] / "controlled-task.displaced"
    outside = env["tasks_root"].parent / "directory-race-outside"
    outside.mkdir()
    exchange_requested = Event()
    exchange_finished = Event()

    def exchange_directory():
        assert exchange_requested.wait(timeout=10)
        target.rename(displaced)
        target.symlink_to(outside, target_is_directory=True)
        exchange_finished.set()

    def inject(point, revision):
        if point == "after_cache_task_write":
            exchange_requested.set()
            assert exchange_finished.wait(timeout=10)

    worker = _worker(
        env,
        worker_id="worker-directory-race",
        fault_injector=inject,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        exchange = executor.submit(exchange_directory)
        result = worker.run_once(published.materialization_id)
        exchange.result(timeout=10)
    assert result.activation_state == "active"
    assert result.cache_refreshed is False
    assert "directory changed during write" in result.error
    assert list(outside.iterdir()) == []
    assert (displaced / "task.yaml").exists()
    assert not (displaced / CACHE_METADATA_FILE).exists()

    target.unlink()
    displaced.rename(target)
    repaired = worker.run_once(published.materialization_id)
    assert repaired.action == "cache_reconciled"
    assert repaired.cache_refreshed is True
    assert json.loads((target / CACHE_METADATA_FILE).read_text(encoding="utf-8"))[
        "revision_id"
    ] == str(published.revision_id)


def test_empty_task_root_segments_are_filtered_without_implicit_cwd(materialization_env, tmp_path: Path):
    env = materialization_env
    with pytest.raises(ValueError, match="non-empty path"):
        TaskMaterializationWorker(
            env["factory"],
            runs_root=env["runs_root"],
            tasks_root=f"{os.pathsep},",
        )

    cache_root = tmp_path / "explicit-cache"
    roots = f"{os.pathsep}{cache_root}{os.pathsep},"
    _draft, published = _publish(env, "multi-root", "publish-multi-root")
    worker = TaskMaterializationWorker(
        env["factory"],
        runs_root=env["runs_root"],
        tasks_root=roots,
        worker_id="worker-multi-root",
        poll_seconds=0.01,
    )
    worker.drain(published.materialization_id, timeout_seconds=1)
    assert (cache_root / "controlled-task" / "task.yaml").exists()


def test_replaced_lease_failure_reports_lease_lost_not_retry(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "lease-lost", "publish-lease-lost")
    replacement = _worker(env, worker_id="worker-replacement")
    replacement_claim = None

    def inject(point, revision):
        nonlocal replacement_claim
        if point == "before_snapshot_commit":
            _expire_lease(env, published.materialization_id)
            replacement_claim = replacement._claim(published.materialization_id)
            assert replacement_claim is not None
            assert replacement_claim.attempt_count == 2
            raise OSError("stale attempt failed")

    stale = _worker(
        env,
        worker_id="worker-stale",
        fault_injector=inject,
    )
    result = stale.run_once(published.materialization_id)
    assert result.action == "lease_lost"
    assert "MaterializationLeaseLost" in result.error
    status = stale.status(published.materialization_id)
    assert status.state == TaskMaterializationState.PROCESSING
    assert status.attempt_count == 2
    assert status.claimed_by == "worker-replacement"

    _expire_lease(env, published.materialization_id)
    recovery = _worker(env, worker_id="worker-after-lease-lost")
    assert recovery.run_once(published.materialization_id).activation_state == "active"
    assert recovery.status(published.materialization_id).attempt_count == 3


@pytest.mark.parametrize(
    "link_kind",
    ["root", "ancestor", "locks", "staging", "revision", "content_hash", "lock_file"],
)
def test_snapshot_store_rejects_symlinked_authority_paths(materialization_env, link_kind: str):
    env = materialization_env
    _draft, published = _publish(env, f"snapshot-{link_kind}", f"publish-snapshot-{link_kind}")
    root = _snapshot_root(env)
    outside = env["runs_root"].parent / f"snapshot-outside-{link_kind}"
    outside.mkdir()
    expected_outside: list[str] = []

    if link_kind == "root":
        root.parent.mkdir(parents=True)
        attacked_path = root
        attacked_path.symlink_to(outside, target_is_directory=True)
    elif link_kind == "ancestor":
        root.parent.parent.mkdir(parents=True)
        attacked_path = root.parent
        attacked_path.symlink_to(outside, target_is_directory=True)
    elif link_kind == "locks":
        root.mkdir(parents=True)
        attacked_path = root / ".locks"
        attacked_path.symlink_to(outside, target_is_directory=True)
    elif link_kind == "staging":
        root.mkdir(parents=True)
        attacked_path = root / ".staging"
        attacked_path.symlink_to(outside, target_is_directory=True)
    elif link_kind == "revision":
        root.mkdir(parents=True)
        attacked_path = root / str(published.revision_id)
        attacked_path.symlink_to(outside, target_is_directory=True)
    elif link_kind == "content_hash":
        revision_directory = root / str(published.revision_id)
        revision_directory.mkdir(parents=True)
        attacked_path = revision_directory / published.content_hash
        attacked_path.symlink_to(outside, target_is_directory=True)
    else:
        lock_directory = root / ".locks"
        lock_directory.mkdir(parents=True)
        sentinel = outside / "sentinel"
        sentinel.write_text("sentinel", encoding="utf-8")
        expected_outside = ["sentinel"]
        attacked_path = lock_directory / f"{published.revision_id}.lock"
        attacked_path.symlink_to(sentinel)

    worker = _worker(env, worker_id=f"worker-snapshot-{link_kind}")
    result = worker.run_once(published.materialization_id)
    status = worker.status(published.materialization_id)
    assert result.action == "failed"
    assert "snapshot" in result.error
    assert status.state == TaskMaterializationState.FAILED
    assert status.activation_state == "not_ready"
    assert sorted(path.name for path in outside.iterdir()) == expected_outside
    if link_kind == "lock_file":
        assert (outside / "sentinel").read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize(
    ("exchange_kind", "exchange_point"),
    [
        ("ancestor", "after_task_write"),
        ("root", "after_task_write"),
        ("locks", "after_snapshot_lock_acquired"),
        ("staging", "after_task_write"),
        ("revision", "before_snapshot_commit"),
        ("content_hash", "after_snapshot_commit"),
    ],
)
def test_snapshot_store_detects_runtime_directory_exchange_without_external_writes(
    materialization_env,
    exchange_kind: str,
    exchange_point: str,
):
    env = materialization_env
    _draft, published = _publish(
        env,
        f"snapshot-exchange-{exchange_kind}",
        f"publish-snapshot-exchange-{exchange_kind}",
    )
    root = _snapshot_root(env)
    outside = env["runs_root"].parent / f"snapshot-exchange-outside-{exchange_kind}"
    outside.mkdir()
    exchange_requested = Event()
    exchange_finished = Event()

    if exchange_kind == "ancestor":
        attacked_path = env["runs_root"]
        displaced = attacked_path.with_name("runs.displaced")
    elif exchange_kind == "root":
        attacked_path = root
        displaced = root.with_name("task_snapshots.displaced")
    elif exchange_kind == "locks":
        attacked_path = root / ".locks"
        displaced = root / ".locks.displaced"
    elif exchange_kind == "staging":
        attacked_path = root / ".staging"
        displaced = root / ".staging.displaced"
    elif exchange_kind == "revision":
        attacked_path = root / str(published.revision_id)
        displaced = root / f"{published.revision_id}.displaced"
    else:
        revision_directory = root / str(published.revision_id)
        attacked_path = revision_directory / published.content_hash
        displaced = revision_directory / f"{published.content_hash}.displaced"

    def exchange_directory():
        assert exchange_requested.wait(timeout=10)
        attacked_path.rename(displaced)
        attacked_path.symlink_to(outside, target_is_directory=True)
        exchange_finished.set()

    def inject(point, revision):
        if point == exchange_point and not exchange_requested.is_set():
            exchange_requested.set()
            assert exchange_finished.wait(timeout=10)

    worker = _worker(
        env,
        worker_id=f"worker-snapshot-exchange-{exchange_kind}",
        fault_injector=inject,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        exchange = executor.submit(exchange_directory)
        result = worker.run_once(published.materialization_id)
        exchange.result(timeout=10)

    status = worker.status(published.materialization_id)
    assert result.action == "failed"
    assert status.state == TaskMaterializationState.FAILED
    assert list(outside.rglob("*")) == []
    assert attacked_path.is_symlink()


def test_snapshot_lock_file_exchange_stops_before_snapshot_commit(materialization_env):
    env = materialization_env
    _draft, published = _publish(
        env,
        "snapshot-lock-file-exchange",
        "publish-snapshot-lock-file-exchange",
    )
    root = _snapshot_root(env)
    lock_path = root / ".locks" / f"{published.revision_id}.lock"
    displaced = lock_path.with_name(f"{published.revision_id}.lock.displaced")
    sentinel = env["runs_root"].parent / "snapshot-lock-file-sentinel"
    sentinel.write_text("sentinel", encoding="utf-8")
    exchanged = False

    def inject(point, revision):
        nonlocal exchanged
        if point == "after_task_write" and not exchanged:
            exchanged = True
            lock_path.rename(displaced)
            lock_path.symlink_to(sentinel)

    worker = _worker(
        env,
        worker_id="worker-snapshot-lock-file-exchange",
        fault_injector=inject,
    )
    result = worker.run_once(published.materialization_id)
    target = root / str(published.revision_id) / published.content_hash

    assert result.action == "failed"
    assert "lock file changed" in result.error
    assert not target.exists()
    assert not target.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert displaced.is_file()
    assert lock_path.is_symlink()


@pytest.mark.parametrize("filename", sorted(SNAPSHOT_FILES))
def test_snapshot_loader_rejects_symlinked_snapshot_files(
    materialization_env,
    filename: str,
):
    env = materialization_env
    _draft, published = _publish(
        env,
        f"snapshot-file-{filename}",
        f"publish-snapshot-file-{filename}",
    )
    worker = _worker(env, worker_id=f"worker-snapshot-file-{filename}")
    worker.drain(published.materialization_id, timeout_seconds=1)
    snapshot_path = worker.status(published.materialization_id).snapshot_path
    outside = env["runs_root"].parent / f"snapshot-file-outside-{filename}"
    outside.write_text("sentinel", encoding="utf-8")
    file_path = snapshot_path / filename
    file_path.unlink()
    file_path.symlink_to(outside)

    loader = ControlTaskSnapshotLoader(
        env["factory"],
        env["runs_root"],
        env["tasks_root"],
    )
    with pytest.raises(SnapshotValidationError):
        loader.load("workspace", "controlled-task")
    status = worker.status(published.materialization_id)
    assert status.snapshot_valid is False
    assert outside.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO test requires POSIX mkfifo")
def test_snapshot_loader_rejects_fifo_snapshot_file(materialization_env):
    env = materialization_env
    _draft, published = _publish(
        env,
        "snapshot-file-fifo",
        "publish-snapshot-file-fifo",
    )
    worker = _worker(env, worker_id="worker-snapshot-file-fifo")
    worker.drain(published.materialization_id, timeout_seconds=1)
    snapshot_path = worker.status(published.materialization_id).snapshot_path
    file_path = snapshot_path / "task.yaml"
    file_path.unlink()
    os.mkfifo(file_path)

    loader = ControlTaskSnapshotLoader(
        env["factory"],
        env["runs_root"],
        env["tasks_root"],
    )
    with pytest.raises(SnapshotValidationError):
        loader.load("workspace", "controlled-task")
    assert worker.status(published.materialization_id).snapshot_valid is False


@pytest.mark.parametrize("exchange_kind", ["revision", "content_hash"])
def test_snapshot_validate_detects_directory_exchange_during_fd_read(
    materialization_env,
    monkeypatch,
    exchange_kind: str,
):
    env = materialization_env
    _draft, published = _publish(
        env,
        "snapshot-validate-exchange",
        "publish-snapshot-validate-exchange",
    )
    worker = _worker(env, worker_id="worker-snapshot-validate-exchange")
    worker.drain(published.materialization_id, timeout_seconds=1)
    snapshot_path = worker.status(published.materialization_id).snapshot_path
    revision_directory = snapshot_path.parent
    if exchange_kind == "revision":
        attacked_path = revision_directory
        displaced = revision_directory.with_name(f"{revision_directory.name}.displaced")
    else:
        attacked_path = snapshot_path
        displaced = snapshot_path.with_name(f"{snapshot_path.name}.displaced")
    outside = env["runs_root"].parent / f"snapshot-validate-exchange-outside-{exchange_kind}"
    outside.mkdir()
    original_listdir = materialization_module.os.listdir
    exchanged = False

    def exchange_before_listdir(path):
        nonlocal exchanged
        if not exchanged and isinstance(path, int):
            exchanged = True
            attacked_path.rename(displaced)
            attacked_path.symlink_to(outside, target_is_directory=True)
        return original_listdir(path)

    monkeypatch.setattr(materialization_module.os, "listdir", exchange_before_listdir)
    loader = ControlTaskSnapshotLoader(
        env["factory"],
        env["runs_root"],
        env["tasks_root"],
    )
    with pytest.raises(SnapshotValidationError):
        loader.load("workspace", "controlled-task")
    assert list(outside.iterdir()) == []


def test_snapshot_commit_never_replaces_concurrent_empty_target(materialization_env):
    env = materialization_env
    _draft, published = _publish(
        env,
        "snapshot-no-replace",
        "publish-snapshot-no-replace",
    )
    target: Path | None = None
    target_inode: int | None = None

    def inject(point, revision):
        nonlocal target, target_inode
        if point == "before_snapshot_rename" and target is None:
            target = _snapshot_root(env) / str(revision.revision_id) / revision.content_hash
            target.mkdir()
            target_inode = target.stat().st_ino

    worker = _worker(
        env,
        worker_id="worker-snapshot-no-replace",
        fault_injector=inject,
    )
    result = worker.run_once(published.materialization_id)
    status = worker.status(published.materialization_id)
    assert result.action == "failed"
    assert status.state == TaskMaterializationState.FAILED
    assert target is not None
    assert target.stat().st_ino == target_inode
    assert list(target.iterdir()) == []


def test_crash_before_snapshot_commit_recovers_from_a_new_attempt(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "pre-commit", "publish-pre-commit")
    crashed = False

    def inject(point, revision):
        nonlocal crashed
        if point == "before_snapshot_commit" and not crashed:
            crashed = True
            raise MaterializationCrash("before commit")

    worker = _worker(env, worker_id="worker-pre-commit", fault_injector=inject)
    with pytest.raises(MaterializationCrash):
        worker.run_once(published.materialization_id)
    status = worker.status(published.materialization_id)
    assert status.state == TaskMaterializationState.PROCESSING
    assert status.snapshot_exists is False

    _expire_lease(env, published.materialization_id)
    recovery = _worker(env, worker_id="worker-pre-commit-recovery")
    assert recovery.run_once(published.materialization_id).activation_state == "active"
    assert recovery.status(published.materialization_id).attempt_count == 2


def test_orphan_snapshot_is_validated_and_reused_without_overwrite(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "orphan", "publish-orphan")
    crashed = False

    def inject(point, revision):
        nonlocal crashed
        if point == "after_snapshot_commit" and not crashed:
            crashed = True
            raise MaterializationCrash("orphan snapshot")

    worker = _worker(env, worker_id="worker-orphan", fault_injector=inject)
    with pytest.raises(MaterializationCrash):
        worker.run_once(published.materialization_id)

    orphan = worker.status(published.materialization_id)
    assert orphan.state == TaskMaterializationState.PROCESSING
    assert orphan.snapshot_valid is True
    before = orphan.snapshot_path.stat()
    before_bytes = (orphan.snapshot_path / "task.yaml").read_bytes()

    _expire_lease(env, published.materialization_id)
    recovery = _worker(env, worker_id="worker-orphan-recovery")
    recovery.run_once(published.materialization_id)
    final = recovery.status(published.materialization_id)
    after = final.snapshot_path.stat()
    assert final.activation_state == "active"
    assert final.attempt_count == 2
    assert after.st_ino == before.st_ino
    assert (final.snapshot_path / "task.yaml").read_bytes() == before_bytes


def test_ready_snapshot_recovers_after_crash_before_activation(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "ready", "publish-ready")
    crashed = False

    def inject(point, revision):
        nonlocal crashed
        if point == "before_activation" and not crashed:
            crashed = True
            raise MaterializationCrash("before activation")

    worker = _worker(env, worker_id="worker-before-activation", fault_injector=inject)
    with pytest.raises(MaterializationCrash):
        worker.run_once(published.materialization_id)

    ready = worker.status(published.materialization_id)
    assert ready.state == TaskMaterializationState.SUCCEEDED
    assert ready.activation_state == "awaiting_activation"
    assert ready.attempt_count == 1
    with Session(env["engine"]) as session:
        assert session.get(Task, env["task_id"]).current_revision_id is None

    recovery = _worker(env, worker_id="worker-activation-recovery")
    result = recovery.run_once(published.materialization_id)
    assert result.action == "activated"
    assert recovery.status(published.materialization_id).activation_state == "active"


def test_retryable_failure_returns_to_pending_and_recovers(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "retry", "publish-retry")
    failed_once = False

    def inject(point, revision):
        nonlocal failed_once
        if point == "before_snapshot_commit" and not failed_once:
            failed_once = True
            raise OSError("transient filesystem error")

    worker = _worker(env, worker_id="worker-retry", fault_injector=inject)
    first = worker.run_once(published.materialization_id)
    assert first.action == "retry_scheduled"
    pending = worker.status(published.materialization_id)
    assert pending.state == TaskMaterializationState.PENDING
    assert "transient filesystem error" in pending.last_error

    second = worker.run_once(published.materialization_id)
    assert second.action == "materialized"
    final = worker.status(published.materialization_id)
    assert final.activation_state == "active"
    assert final.attempt_count == 2
    assert final.last_error is None


def test_conflicting_target_fails_closed_and_keeps_old_active_revision(materialization_env):
    env = materialization_env
    first_draft, first = _publish(env, "v1", "publish-conflict-v1")
    worker = _worker(env, worker_id="worker-conflict-v1")
    worker.drain(first.materialization_id, timeout_seconds=1)

    _second_draft, second = _publish(
        env,
        "v2",
        "publish-conflict-v2",
        if_match=first_draft.etag,
    )
    second_status = worker.status(second.materialization_id)
    second_status.snapshot_path.mkdir(parents=True)
    conflict_path = second_status.snapshot_path / "task.yaml"
    conflict_path.write_text("task_id: controlled-task\n", encoding="utf-8")

    result = worker.run_once(second.materialization_id)
    failed = worker.status(second.materialization_id)
    assert result.action == "failed"
    assert failed.state == TaskMaterializationState.FAILED
    assert failed.snapshot_valid is False
    assert conflict_path.read_text(encoding="utf-8") == "task_id: controlled-task\n"

    loader = ControlTaskSnapshotLoader(
        env["factory"],
        env["runs_root"],
        env["tasks_root"],
    )
    assert loader.load("workspace", "controlled-task").raw["marker"] == "v1"
    with Session(env["engine"]) as session:
        assert session.get(Task, env["task_id"]).current_revision_id == first.revision_id
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "task.revision_materialization_failed",
                AuditEvent.resource_id == str(second.materialization_id),
            ),
        ) == 1


def test_out_of_order_workers_cannot_regress_current_revision_or_cache(materialization_env):
    env = materialization_env
    first_draft, first = _publish(env, "v1", "publish-order-v1")
    _second_draft, second = _publish(
        env,
        "v2",
        "publish-order-v2",
        if_match=first_draft.etag,
    )
    newer_worker = _worker(env, worker_id="worker-newer")
    older_worker = _worker(env, worker_id="worker-older")

    newer = newer_worker.run_once(second.materialization_id)
    older = older_worker.run_once(first.materialization_id)
    assert newer.activation_state == "active"
    assert older.activation_state == "superseded"
    assert newer_worker.status(second.materialization_id).activation_state == "active"
    assert older_worker.status(first.materialization_id).activation_state == "superseded"

    metadata = json.loads(
        (env["tasks_root"] / "controlled-task" / CACHE_METADATA_FILE).read_text(encoding="utf-8"),
    )
    assert metadata["revision_id"] == str(second.revision_id)
    assert yaml.safe_load(
        (env["tasks_root"] / "controlled-task" / "task.yaml").read_text(encoding="utf-8"),
    )["marker"] == "v2"
    with Session(env["engine"]) as session:
        task = session.get(Task, env["task_id"])
        assert task.current_revision_id == second.revision_id
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.event_type == "task.revision_activated",
            ),
        ) == 1


def test_duplicate_workers_share_one_lease_attempt_and_one_activation(materialization_env):
    env = materialization_env
    _draft, published = _publish(env, "duplicate", "publish-duplicate")
    claimed = Event()
    release = Event()

    def block_after_claim(point, revision):
        if point == "after_claim":
            claimed.set()
            assert release.wait(timeout=10)

    first_worker = _worker(
        env,
        worker_id="worker-duplicate-a",
        fault_injector=block_after_claim,
    )
    second_worker = _worker(env, worker_id="worker-duplicate-b")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first_worker.run_once, published.materialization_id)
            assert claimed.wait(timeout=10)
            assert second_worker.run_once(published.materialization_id).action == "idle"
            release.set()
            assert future.result(timeout=10).activation_state == "active"
        status = second_worker.status(published.materialization_id)
        assert status.attempt_count == 1
        with Session(env["engine"]) as session:
            assert session.scalar(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.event_type == "task.revision_activated",
                    AuditEvent.resource_id == str(published.revision_id),
                ),
            ) == 1
    finally:
        release.set()


@pytest.mark.skipif(
    not os.environ.get("LLS_TEST_POSTGRES_URL") or not os.environ.get("LLS_TEST_POSTGRES_APP_URL"),
    reason="PostgreSQL owner/app test URLs are not set",
)
def test_postgres_workers_skip_locked_fence_duplicates_and_activate_monotonically(tmp_path: Path):
    owner_url = os.environ["LLS_TEST_POSTGRES_URL"]
    app_url = os.environ["LLS_TEST_POSTGRES_APP_URL"]
    command.upgrade(build_alembic_config(owner_url), "head")
    suffix = uuid.uuid4().hex[:12]
    workspace_slug = f"materialize-{suffix}"
    task_key = f"materialize-task-{suffix}"
    identity = ExternalIdentity("https://materialization-postgres.example", f"admin-{suffix}")
    owner_engine = create_database_engine(owner_url)
    deferred_materializations: list[
        tuple[uuid.UUID, datetime, datetime | None]
    ] = []
    try:
        with Session(owner_engine) as session:
            bootstrap = bootstrap_admin(
                session,
                issuer=identity.issuer,
                subject=identity.subject,
                workspace_slug=workspace_slug,
                workspace_name="Materialization PostgreSQL",
            )
        with Session(owner_engine) as session, session.begin():
            task = Task(
                workspace_id=bootstrap.workspace_id,
                task_key=task_key,
                name="Materialization PostgreSQL",
                created_by_principal_id=bootstrap.principal_id,
            )
            session.add(task)
            session.flush()
            task_id = task.id

        service = DatabaseService.from_url(app_url)
        try:
            definition_v1 = _task_definition(task_key, "v1")
            draft_v1 = service.save_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition=definition_v1,
                rendered_task=_render_task(definition_v1),
                if_match="*",
                channel=AuditChannel.API,
            ).draft
            first = service.publish_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                if_match=draft_v1.etag,
                reason="PostgreSQL revision 1",
                idempotency_key=f"materialize-v1-{suffix}",
                channel=AuditChannel.API,
            )
            definition_v2 = _task_definition(task_key, "v2")
            draft_v2 = service.save_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition=definition_v2,
                rendered_task=_render_task(definition_v2),
                if_match=draft_v1.etag,
                channel=AuditChannel.API,
            ).draft
            second = service.publish_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                if_match=draft_v2.etag,
                reason="PostgreSQL revision 2",
                idempotency_key=f"materialize-v2-{suffix}",
                channel=AuditChannel.API,
            )
        finally:
            service.close()

        with Session(owner_engine) as session, session.begin():
            deferred_materializations = list(
                session.execute(
                    select(
                        TaskRevisionMaterialization.id,
                        TaskRevisionMaterialization.available_at,
                        TaskRevisionMaterialization.lease_expires_at,
                    ).where(
                        TaskRevisionMaterialization.state.in_(
                            [
                                TaskMaterializationState.PENDING,
                                TaskMaterializationState.PROCESSING,
                            ],
                        ),
                        TaskRevisionMaterialization.id.not_in(
                            [first.materialization_id, second.materialization_id],
                        ),
                    ),
                ),
            )
            if deferred_materializations:
                session.execute(
                    update(TaskRevisionMaterialization)
                    .where(
                        TaskRevisionMaterialization.id.in_(
                            [item[0] for item in deferred_materializations],
                        ),
                    )
                    .values(
                        available_at=datetime.now(timezone.utc) + timedelta(days=1),
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                    ),
                )

        newer_activated = Event()

        def inject(point, revision):
            if point == "after_claim" and revision.revision_number == 1:
                assert newer_activated.wait(timeout=10)
            if point == "after_activation" and revision.revision_number == 2:
                newer_activated.set()

        start = Barrier(2)

        def run_worker(worker_id: str):
            worker = TaskMaterializationWorker.from_url(
                app_url,
                runs_root=tmp_path / "runs",
                tasks_root=tmp_path / "tasks",
                worker_id=worker_id,
                fault_injector=inject,
                poll_seconds=0.01,
            )
            try:
                start.wait()
                return worker.run_once()
            finally:
                worker.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(run_worker, (f"pg-a-{suffix}", f"pg-b-{suffix}")))
        assert {result.activation_state for result in results} == {"active", "superseded"}

        app_engine = create_database_engine(app_url)
        try:
            with Session(app_engine) as session:
                task = session.get(Task, task_id)
                attempts = {
                    row.revision_id: row.attempt_count
                    for row in session.scalars(
                        select(TaskRevisionMaterialization).where(
                            TaskRevisionMaterialization.id.in_(
                                [first.materialization_id, second.materialization_id],
                            ),
                        ),
                    )
                }
                assert task.current_revision_id == second.revision_id
                assert attempts == {first.revision_id: 1, second.revision_id: 1}
                assert session.scalar(
                    select(func.count()).select_from(AuditEvent).where(
                        AuditEvent.event_type == "task.revision_activated",
                        AuditEvent.resource_id == str(second.revision_id),
                    ),
                ) == 1
        finally:
            app_engine.dispose()

        service = DatabaseService.from_url(app_url)
        try:
            definition_v3 = _task_definition(task_key, "v3")
            draft_v3 = service.save_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition=definition_v3,
                rendered_task=_render_task(definition_v3),
                if_match=draft_v2.etag,
                channel=AuditChannel.API,
            ).draft
            third = service.publish_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                if_match=draft_v3.etag,
                reason="PostgreSQL revision 3",
                idempotency_key=f"materialize-v3-{suffix}",
                channel=AuditChannel.API,
            )
        finally:
            service.close()

        claimed = Event()
        release = Event()

        def block_after_claim(point, revision):
            if point == "after_claim":
                claimed.set()
                assert release.wait(timeout=10)

        first_worker = TaskMaterializationWorker.from_url(
            app_url,
            runs_root=tmp_path / "runs",
            tasks_root=tmp_path / "tasks",
            worker_id=f"pg-duplicate-a-{suffix}",
            fault_injector=block_after_claim,
        )
        second_worker = TaskMaterializationWorker.from_url(
            app_url,
            runs_root=tmp_path / "runs",
            tasks_root=tmp_path / "tasks",
            worker_id=f"pg-duplicate-b-{suffix}",
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(first_worker.run_once, third.materialization_id)
                assert claimed.wait(timeout=10)
                duplicate = second_worker.run_once(third.materialization_id)
                assert duplicate.action == "idle"
                release.set()
                assert future.result(timeout=10).activation_state == "active"
            assert second_worker.status(third.materialization_id).attempt_count == 1
        finally:
            release.set()
            first_worker.close()
            second_worker.close()

        service = DatabaseService.from_url(app_url)
        try:
            definition_v4 = _task_definition(task_key, "v4")
            draft_v4 = service.save_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition=definition_v4,
                rendered_task=_render_task(definition_v4),
                if_match=draft_v3.etag,
                channel=AuditChannel.API,
            ).draft
            fourth = service.publish_task_draft(
                actor_identity=identity,
                caller_identity=identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                if_match=draft_v4.etag,
                reason="PostgreSQL revision 4",
                idempotency_key=f"materialize-v4-{suffix}",
                channel=AuditChannel.API,
            )
        finally:
            service.close()

        stale_worker = TaskMaterializationWorker.from_url(
            app_url,
            runs_root=tmp_path / "runs",
            tasks_root=tmp_path / "tasks",
            worker_id=f"pg-stale-{suffix}",
        )
        takeover_worker = TaskMaterializationWorker.from_url(
            app_url,
            runs_root=tmp_path / "runs",
            tasks_root=tmp_path / "tasks",
            worker_id=f"pg-takeover-{suffix}",
        )
        try:
            stale_claim = stale_worker._claim(fourth.materialization_id)
            assert stale_claim is not None
            assert stale_claim.attempt_count == 1
            with Session(owner_engine) as session, session.begin():
                session.execute(
                    update(TaskRevisionMaterialization)
                    .where(TaskRevisionMaterialization.id == fourth.materialization_id)
                    .values(
                        lease_expires_at=func.current_timestamp() - text("INTERVAL '1 second'"),
                    ),
                )
            takeover = takeover_worker.run_once(fourth.materialization_id)
            assert takeover.activation_state == "active"
            takeover_status = takeover_worker.status(fourth.materialization_id)
            assert takeover_status.attempt_count == 2
            assert takeover_status.claimed_by == f"pg-takeover-{suffix}"
            with pytest.raises(MaterializationLeaseLost):
                stale_worker._mark_ready(stale_claim)
            assert takeover_worker.status(fourth.materialization_id).attempt_count == 2
        finally:
            stale_worker.close()
            takeover_worker.close()
    finally:
        if deferred_materializations:
            with Session(owner_engine) as session, session.begin():
                for materialization_id, available_at, lease_expires_at in deferred_materializations:
                    session.execute(
                        update(TaskRevisionMaterialization)
                        .where(TaskRevisionMaterialization.id == materialization_id)
                        .values(
                            available_at=available_at,
                            lease_expires_at=lease_expires_at,
                        ),
                    )
        owner_engine.dispose()
