from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import delete, func, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from llm_labeling_scaffold.db import (
    DatabaseService,
    ExternalIdentity,
    IdempotencyClaimStatus,
    IdentityTypeConflict,
    Permission,
    PrincipalType,
)
from llm_labeling_scaffold.db import migration as migration_module
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.migration import build_alembic_config, upgrade_database
from llm_labeling_scaffold.db.models import (
    AuditEvent,
    MigrationRun,
    Principal,
    RoleBinding,
    Task,
    WorkspaceSetting,
)


EXPECTED_TABLES = {
    "alembic_version",
    "principals",
    "workspaces",
    "role_bindings",
    "tasks",
    "idempotency_records",
    "workspace_settings",
    "audit_events",
    "migration_runs",
    "task_drafts",
    "task_revisions",
    "task_revision_materializations",
}

FORBIDDEN_TABLES = {
    "sessions",
    "oauth_clients",
    "authorization_codes",
    "refresh_tokens",
}


def test_clean_database_upgrades_to_head_and_cli_upgrade_records_runs(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migration.db'}"
    command.upgrade(build_alembic_config(database_url), "head")
    first = upgrade_database(database_url)
    second = upgrade_database(database_url)

    engine = create_database_engine(database_url)
    try:
        table_names = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLES <= table_names
        assert FORBIDDEN_TABLES.isdisjoint(table_names)
        task_unique_constraints = inspect(engine).get_unique_constraints("tasks")
        assert any(constraint["column_names"] == ["task_key"] for constraint in task_unique_constraints)
        idempotency_columns = {column["name"] for column in inspect(engine).get_columns("idempotency_records")}
        assert "idempotency_key_hash" in idempotency_columns
        assert "idempotency_key" not in idempotency_columns
        assert "current_revision_id" in {column["name"] for column in inspect(engine).get_columns("tasks")}
        assert first.applied_revision == "20260714_0002"
        assert second.applied_revision == "20260714_0002"
        with Session(engine) as session:
            assert len(session.scalars(select(MigrationRun)).all()) == 2
        with Session(engine) as session:
            result = bootstrap_admin(
                session,
                issuer="https://sqlite-migration.example",
                subject="admin",
                workspace_slug="sqlite-migration",
                workspace_name="SQLite Migration",
            )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    update(AuditEvent)
                    .where(AuditEvent.workspace_id == result.workspace_id)
                    .values(event_type="overwritten"),
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    delete(AuditEvent).where(AuditEvent.workspace_id == result.workspace_id),
                )
    finally:
        engine.dispose()


def test_upgrade_preserves_original_error_when_failure_recording_also_fails(monkeypatch, caplog):
    original_error = RuntimeError("migration failed")

    def fail_upgrade(config, revision):
        raise original_error

    def fail_recorder(*args, **kwargs):
        raise OSError("recorder failed")

    monkeypatch.setattr(migration_module.command, "upgrade", fail_upgrade)
    monkeypatch.setattr(migration_module, "_record_run_if_available", fail_recorder)

    with caplog.at_level("WARNING"), pytest.raises(RuntimeError) as exc_info:
        upgrade_database("sqlite+pysqlite:///:memory:")

    assert exc_info.value is original_error
    assert "failed to record migration failure" in caplog.text


@pytest.mark.skipif(not os.environ.get("LLS_TEST_POSTGRES_URL"), reason="LLS_TEST_POSTGRES_URL is not set")
def test_postgres_upgrade_and_audit_trigger_rejects_mutation():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    engine = create_database_engine(database_url)
    suffix = uuid.uuid4().hex[:12]
    try:
        with Session(engine) as session:
            result = bootstrap_admin(
                session,
                issuer="https://postgres-test.example",
                subject=f"admin-{suffix}",
                workspace_slug=f"postgres-{suffix}",
                workspace_name="PostgreSQL Test",
            )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    update(AuditEvent)
                    .where(AuditEvent.workspace_id == result.workspace_id)
                    .values(event_type="overwritten"),
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    delete(AuditEvent).where(AuditEvent.workspace_id == result.workspace_id),
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE TABLE audit_events"))
        with engine.connect() as connection:
            event_type = connection.scalar(
                text("SELECT event_type FROM audit_events WHERE workspace_id = :workspace_id"),
                {"workspace_id": result.workspace_id},
            )
            jsonb_columns = set(
                connection.execute(
                    text(
                        """
                        SELECT table_name, column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema() AND data_type = 'jsonb'
                        """
                    )
                ).tuples()
            )
        assert event_type == "workspace.admin_bootstrapped"
        assert {
            ("idempotency_records", "response_body"),
            ("workspace_settings", "setting_value"),
            ("audit_events", "details"),
            ("task_drafts", "definition"),
            ("task_revisions", "definition"),
        } <= jsonb_columns
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("LLS_TEST_POSTGRES_URL") or not os.environ.get("LLS_TEST_POSTGRES_APP_URL"),
    reason="PostgreSQL owner/app test URLs are not set",
)
def test_postgres_runtime_role_has_dml_without_ddl_or_truncate():
    owner_url = os.environ["LLS_TEST_POSTGRES_URL"]
    app_url = os.environ["LLS_TEST_POSTGRES_APP_URL"]
    command.upgrade(build_alembic_config(owner_url), "head")
    suffix = uuid.uuid4().hex[:12]
    owner_engine = create_database_engine(owner_url)
    app_engine = create_database_engine(app_url)
    try:
        with Session(owner_engine) as session:
            result = bootstrap_admin(
                session,
                issuer="https://runtime-role.example",
                subject=f"admin-{suffix}",
                workspace_slug=f"runtime-{suffix}",
                workspace_name="Runtime Role Test",
            )

        with Session(app_engine) as session, session.begin():
            assert session.scalar(text("SELECT current_user")) == make_url(app_url).username
            assert session.scalar(
                text("SELECT has_table_privilege(current_user, 'audit_events', 'TRUNCATE')"),
            ) is False
            assert session.scalar(
                text("SELECT has_database_privilege(current_user, current_database(), 'TEMP')"),
            ) is False
            assert session.scalar(select(func.count()).select_from(AuditEvent)) >= 1
            setting = WorkspaceSetting(
                workspace_id=result.workspace_id,
                setting_key=f"runtime-{suffix}",
                setting_value={"runtime": True},
                updated_by_principal_id=result.principal_id,
            )
            session.add(setting)

        with Session(app_engine) as session:
            assert session.scalar(
                select(WorkspaceSetting.setting_value).where(
                    WorkspaceSetting.workspace_id == result.workspace_id,
                    WorkspaceSetting.setting_key == f"runtime-{suffix}",
                ),
            ) == {"runtime": True}

        with pytest.raises(DBAPIError):
            with app_engine.begin() as connection:
                connection.execute(text("CREATE TABLE runtime_forbidden (id integer)"))
        with pytest.raises(DBAPIError):
            with app_engine.begin() as connection:
                connection.execute(text("CREATE TEMP TABLE runtime_temp_forbidden (id integer)"))
        with pytest.raises(DBAPIError):
            with app_engine.begin() as connection:
                connection.execute(text("TRUNCATE TABLE audit_events"))
        with pytest.raises(DBAPIError):
            with app_engine.begin() as connection:
                connection.execute(
                    update(AuditEvent)
                    .where(AuditEvent.workspace_id == result.workspace_id)
                    .values(event_type="runtime-overwrite"),
                )
        with pytest.raises(DBAPIError):
            with app_engine.begin() as connection:
                connection.execute(
                    delete(AuditEvent).where(AuditEvent.workspace_id == result.workspace_id),
                )
    finally:
        app_engine.dispose()
        owner_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("LLS_TEST_POSTGRES_URL") or not os.environ.get("LLS_TEST_POSTGRES_APP_URL"),
    reason="PostgreSQL owner/app test URLs are not set",
)
def test_postgres_concurrent_idempotency_claim_is_atomic():
    owner_url = os.environ["LLS_TEST_POSTGRES_URL"]
    app_url = os.environ["LLS_TEST_POSTGRES_APP_URL"]
    command.upgrade(build_alembic_config(owner_url), "head")
    suffix = uuid.uuid4().hex[:12]
    actor_identity = ExternalIdentity("https://claim-concurrency.example", f"admin-{suffix}")
    caller_identity = ExternalIdentity("llm-labeling-scaffold", f"service-{suffix}")

    owner_engine = create_database_engine(owner_url)
    try:
        with Session(owner_engine) as session:
            result = bootstrap_admin(
                session,
                issuer=actor_identity.issuer,
                subject=actor_identity.subject,
                workspace_slug=f"claim-{suffix}",
                workspace_name="Claim Concurrency",
            )
        owner_service = DatabaseService.from_url(owner_url)
        try:
            owner_service.resolve_or_provision(caller_identity, PrincipalType.SERVICE)
        finally:
            owner_service.close()
        with Session(owner_engine) as session, session.begin():
            session.add(
                Task(
                    workspace_id=result.workspace_id,
                    task_key=f"task-{suffix}",
                    name="Task",
                    created_by_principal_id=result.principal_id,
                )
            )
    finally:
        owner_engine.dispose()

    barrier = Barrier(2)

    def claim():
        service = DatabaseService.from_url(app_url)
        try:
            barrier.wait()
            return service.claim_idempotency(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=f"claim-{suffix}",
                task_key=f"task-{suffix}",
                required_permission=Permission.TASK_PUBLISH,
                operation="task.publish",
                idempotency_key=f"key-{suffix}",
                request_payload={"task": "task", "revision": 1},
            )
        finally:
            service.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _: claim(), range(2)))

    assert claims[0].record_id == claims[1].record_id
    assert {claim.status for claim in claims} == {
        IdempotencyClaimStatus.CLAIMED,
        IdempotencyClaimStatus.PENDING,
    }


@pytest.mark.skipif(not os.environ.get("LLS_TEST_POSTGRES_URL"), reason="LLS_TEST_POSTGRES_URL is not set")
def test_postgres_concurrent_resolve_or_provision_is_idempotent():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    identity = ExternalIdentity(
        "https://concurrency-test.example",
        f"subject-{uuid.uuid4().hex}",
        email_snapshot="shared@example.test",
    )
    barrier = Barrier(2)

    def provision():
        service = DatabaseService.from_url(database_url)
        try:
            barrier.wait()
            return service.resolve_or_provision(identity, PrincipalType.USER)
        finally:
            service.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        principals = list(executor.map(lambda _: provision(), range(2)))

    assert principals[0].id == principals[1].id
    service = DatabaseService.from_url(database_url)
    try:
        with pytest.raises(IdentityTypeConflict):
            service.resolve_or_provision(identity, PrincipalType.SERVICE)
    finally:
        service.close()

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(Principal).where(
                    Principal.issuer == identity.issuer,
                    Principal.subject == identity.subject,
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(RoleBinding).where(
                    RoleBinding.principal_id == principals[0].id,
                )
            ) == 0
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("LLS_TEST_POSTGRES_URL"), reason="LLS_TEST_POSTGRES_URL is not set")
def test_postgres_concurrent_bootstrap_is_idempotent():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    suffix = uuid.uuid4().hex[:12]
    barrier = Barrier(2)

    def bootstrap():
        engine = create_database_engine(database_url)
        try:
            with Session(engine) as session:
                barrier.wait()
                return bootstrap_admin(
                    session,
                    issuer="https://bootstrap-concurrency.example",
                    subject=f"subject-{suffix}",
                    workspace_slug=f"bootstrap-{suffix}",
                    workspace_name="Concurrent Bootstrap",
                )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: bootstrap(), range(2)))

    assert results[0].principal_id == results[1].principal_id
    assert results[0].workspace_id == results[1].workspace_id
    assert results[0].role_binding_id == results[1].role_binding_id
    assert sum(result.changed for result in results) == 1

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.workspace_id == results[0].workspace_id,
                    AuditEvent.event_type == "workspace.admin_bootstrapped",
                )
            ) == 1
    finally:
        engine.dispose()
