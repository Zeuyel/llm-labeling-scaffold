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
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from llm_labeling_scaffold.db import AuditChannel, DatabaseService, ExternalIdentity
from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.enums import TaskMaterializationState
from llm_labeling_scaffold.db.materialization import (
    CACHE_METADATA_FILE,
    ControlTaskSnapshotLoader,
    MaterializationCrash,
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


def _expire_lease(env: dict, materialization_id: uuid.UUID) -> None:
    with Session(env["engine"]) as session, session.begin():
        materialization = session.get(TaskRevisionMaterialization, materialization_id)
        materialization.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def test_publish_202_status_loader_and_compatibility_cache_are_separate(materialization_env):
    env = materialization_env
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

    loader = ControlTaskSnapshotLoader(env["factory"], env["runs_root"])
    loaded = loader.load("workspace", "controlled-task")
    assert loaded.path == final.snapshot_path / "task.yaml"
    assert loaded.raw["marker"] == "v1"

    cache_path = env["tasks_root"] / "controlled-task" / "task.yaml"
    metadata_path = cache_path.parent / CACHE_METADATA_FILE
    assert cache_path.exists()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["revision_id"] == str(published.revision_id)
    cache_path.write_text(_render_task(_task_definition("controlled-task", "stale-cache")), encoding="utf-8")
    assert loader.load("workspace", "controlled-task").raw["marker"] == "v1"

    assert worker.run_once(published.materialization_id).action == "idle"
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

    loader = ControlTaskSnapshotLoader(env["factory"], env["runs_root"])
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
    finally:
        owner_engine.dispose()
