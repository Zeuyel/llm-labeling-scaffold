from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from alembic import command
from sqlalchemy import delete, event, func, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from llm_labeling_scaffold.db import (
    AuditChannel,
    AuthorizationDenied,
    DatabaseService,
    ExternalIdentity,
    IdempotencyClaimStatus,
    IdentityTypeConflict,
    LastWorkspaceAdmin,
    MembershipMutationAction,
    Permission,
    PrincipalType,
    TaskDraftConflict,
    Role,
)
from llm_labeling_scaffold.db import migration as migration_module
from llm_labeling_scaffold.db.bootstrap import bootstrap_admin
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.enums import IdempotencyState
from llm_labeling_scaffold.db.migration import build_alembic_config, upgrade_database
from llm_labeling_scaffold.db.models import (
    AuditEvent,
    IdempotencyRecord,
    MigrationRun,
    Principal,
    RoleBinding,
    Task,
    TaskRevision,
    TaskRevisionMaterialization,
    Workspace,
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


def _role_binding_principal_ondelete(engine) -> str:
    foreign_key = next(
        foreign_key
        for foreign_key in inspect(engine).get_foreign_keys("role_bindings")
        if foreign_key["constrained_columns"] == ["principal_id"]
    )
    return foreign_key["options"]["ondelete"].upper()


def _seed_principal_deletion_boundary(engine, prefix: str) -> dict[str, object]:
    issuer = f"https://{prefix}.example"
    last_admin_identity = ExternalIdentity(issuer, "last-admin")
    actor_identity = ExternalIdentity(issuer, "actor-admin")
    removable_identity = ExternalIdentity(issuer, "removable-admin")
    with Session(engine) as session, session.begin():
        last_admin = Principal(
            issuer=issuer,
            subject=last_admin_identity.subject,
            principal_type=PrincipalType.USER,
        )
        actor = Principal(
            issuer=issuer,
            subject=actor_identity.subject,
            principal_type=PrincipalType.USER,
        )
        removable = Principal(
            issuer=issuer,
            subject=removable_identity.subject,
            principal_type=PrincipalType.USER,
        )
        last_workspace = Workspace(slug=f"{prefix}-last", name="Last Admin Workspace")
        shared_workspace = Workspace(slug=f"{prefix}-shared", name="Shared Admin Workspace")
        session.add_all([last_admin, actor, removable, last_workspace, shared_workspace])
        session.flush()
        session.add_all(
            [
                RoleBinding(
                    workspace_id=last_workspace.id,
                    principal_id=last_admin.id,
                    role=Role.ADMIN,
                ),
                RoleBinding(
                    workspace_id=shared_workspace.id,
                    principal_id=actor.id,
                    role=Role.ADMIN,
                ),
                RoleBinding(
                    workspace_id=shared_workspace.id,
                    principal_id=removable.id,
                    role=Role.ADMIN,
                ),
            ]
        )
        return {
            "last_admin_id": last_admin.id,
            "removable_id": removable.id,
            "shared_workspace_slug": shared_workspace.slug,
            "actor_identity": actor_identity,
            "removable_identity": removable_identity,
        }


def _assert_principal_deletion_boundary(engine, seeded: dict[str, object]) -> None:
    for principal_id in (seeded["last_admin_id"], seeded["removable_id"]):
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(delete(Principal).where(Principal.id == principal_id))
        with Session(engine) as session:
            assert session.get(Principal, principal_id) is not None
            assert session.scalar(
                select(func.count()).select_from(RoleBinding).where(
                    RoleBinding.principal_id == principal_id,
                )
            ) == 1

    service = DatabaseService(sessionmaker(bind=engine, class_=Session, expire_on_commit=False))
    revoked = service.revoke_workspace_membership(
        workspace_slug=seeded["shared_workspace_slug"],
        target_identity=seeded["removable_identity"],
        actor_identity=seeded["actor_identity"],
        caller_identity=seeded["actor_identity"],
        channel=AuditChannel.API,
    )
    assert revoked.action == MembershipMutationAction.REVOKED

    with engine.begin() as connection:
        assert connection.execute(
            delete(Principal).where(Principal.id == seeded["removable_id"]),
        ).rowcount == 1
    with Session(engine) as session:
        assert session.get(Principal, seeded["removable_id"]) is None
        assert session.scalar(
            select(func.count()).select_from(RoleBinding).where(
                RoleBinding.principal_id == seeded["removable_id"],
            )
        ) == 0


def test_sqlite_membership_revision_upgrades_existing_0001_database(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'membership-upgrade.db'}"
    config = build_alembic_config(database_url)
    command.upgrade(config, "20260713_0001")
    engine = create_database_engine(database_url)
    try:
        assert _role_binding_principal_ondelete(engine) == "CASCADE"
        with engine.connect() as connection:
            triggers = set(
                connection.scalars(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'"),
                )
            )
        assert {
            "trg_role_bindings_last_workspace_admin_update",
            "trg_role_bindings_last_workspace_admin_delete",
        }.isdisjoint(triggers)
        seeded = _seed_principal_deletion_boundary(engine, "sqlite-upgrade")
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    try:
        assert _role_binding_principal_ondelete(engine) == "RESTRICT"
        assert next(iter(RoleBinding.__table__.c.principal_id.foreign_keys)).ondelete == "RESTRICT"
        assert {
            "uq_role_bindings_workspace_principal",
            "uq_role_bindings_task_principal",
            "ix_role_bindings_principal_workspace",
        } <= {index["name"] for index in inspect(engine).get_indexes("role_bindings")}
        with engine.connect() as connection:
            triggers = set(
                connection.scalars(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'"),
                )
            )
        assert {
            "trg_role_bindings_last_workspace_admin_update",
            "trg_role_bindings_last_workspace_admin_delete",
        } <= triggers
        _assert_principal_deletion_boundary(engine, seeded)
    finally:
        engine.dispose()


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
        task_foreign_keys = inspect(engine).get_foreign_keys("tasks")
        assert any(
            foreign_key["constrained_columns"] == ["workspace_id", "id", "current_revision_id"]
            and foreign_key["referred_table"] == "task_revisions"
            and foreign_key["referred_columns"] == ["workspace_id", "task_id", "id"]
            for foreign_key in task_foreign_keys
        )
        revision_foreign_keys = inspect(engine).get_foreign_keys("task_revisions")
        assert any(
            foreign_key["constrained_columns"] == ["workspace_id", "task_id"]
            and foreign_key["referred_table"] == "tasks"
            and foreign_key["options"].get("ondelete") == "RESTRICT"
            for foreign_key in revision_foreign_keys
        )
        idempotency_columns = {column["name"] for column in inspect(engine).get_columns("idempotency_records")}
        assert "idempotency_key_hash" in idempotency_columns
        assert "idempotency_key" not in idempotency_columns
        assert "current_revision_id" in {column["name"] for column in inspect(engine).get_columns("tasks")}
        assert "lease_expires_at" in {
            column["name"] for column in inspect(engine).get_columns("task_revision_materializations")
        }
        materialization_indexes = {
            index["name"]: index["column_names"]
            for index in inspect(engine).get_indexes("task_revision_materializations")
        }
        assert materialization_indexes["ix_task_revision_materializations_workspace_task"] == [
            "workspace_id",
            "task_id",
        ]
        assert first.applied_revision == "20260715_0003"
        assert second.applied_revision == "20260715_0003"
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
        with Session(engine) as session, session.begin():
            inactive_admin = Principal(
                issuer="https://sqlite-migration.example",
                subject="inactive-admin",
                principal_type=PrincipalType.USER,
                is_active=False,
            )
            session.add(inactive_admin)
            session.flush()
            session.add(
                RoleBinding(
                    workspace_id=result.workspace_id,
                    principal_id=inactive_admin.id,
                    role=Role.ADMIN,
                )
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
                connection.execute(
                    update(RoleBinding)
                    .where(RoleBinding.id == result.role_binding_id)
                    .values(role=Role.VIEWER),
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(delete(RoleBinding).where(RoleBinding.id == result.role_binding_id))
    finally:
        engine.dispose()


def test_task_revision_schema_rejects_cross_task_pointer_and_parent_delete(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'task-revision-constraints.db'}"
    command.upgrade(build_alembic_config(database_url), "head")
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            bootstrap = bootstrap_admin(
                session,
                issuer="https://task-revision-schema.example",
                subject="admin",
                workspace_slug="task-revision-schema",
                workspace_name="Task Revision Schema",
            )
        with Session(engine) as session, session.begin():
            workspace = session.get(Workspace, bootstrap.workspace_id)
            task_a = Task(
                workspace_id=workspace.id,
                task_key="schema-task-a",
                name="Schema Task A",
                created_by_principal_id=bootstrap.principal_id,
            )
            task_b = Task(
                workspace_id=workspace.id,
                task_key="schema-task-b",
                name="Schema Task B",
                created_by_principal_id=bootstrap.principal_id,
            )
            session.add_all([task_a, task_b])
            session.flush()
            claim = IdempotencyRecord(
                workspace_id=workspace.id,
                actor_principal_id=bootstrap.principal_id,
                caller_principal_id=bootstrap.principal_id,
                operation="task.publish",
                idempotency_key_hash="a" * 64,
                request_fingerprint="b" * 64,
                state=IdempotencyState.SUCCEEDED,
                response_status=202,
                response_body={},
            )
            session.add(claim)
            session.flush()
            revision = TaskRevision(
                workspace_id=workspace.id,
                task_id=task_b.id,
                revision_number=1,
                draft_version=1,
                draft_fingerprint="c" * 64,
                definition={"task_id": task_b.task_key},
                rendered_task="task_id: schema-task-b\n",
                reason="schema constraint test",
                actor_principal_id=bootstrap.principal_id,
                caller_principal_id=bootstrap.principal_id,
                channel=AuditChannel.API,
                content_hash="d" * 64,
                idempotency_record_id=claim.id,
            )
            session.add(revision)
            session.flush()
            task_a_id = task_a.id
            task_b_id = task_b.id
            revision_id = revision.id

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    update(Task)
                    .where(Task.id == task_a_id)
                    .values(current_revision_id=revision_id),
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(delete(Task).where(Task.id == task_b_id))
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
def test_postgres_membership_revision_upgrades_existing_0001_database():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    config = build_alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "20260713_0001")
    suffix = uuid.uuid4().hex[:12]
    try:
        engine = create_database_engine(database_url)
        try:
            assert _role_binding_principal_ondelete(engine) == "CASCADE"
            with engine.connect() as connection:
                assert connection.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_trigger
                            WHERE tgname = 'trg_role_bindings_last_workspace_admin'
                              AND NOT tgisinternal
                        )
                        """
                    )
                ) is False
            seeded = _seed_principal_deletion_boundary(engine, f"pg-upgrade-{suffix}")
        finally:
            engine.dispose()
        command.upgrade(config, "head")
        engine = create_database_engine(database_url)
        try:
            assert _role_binding_principal_ondelete(engine) == "RESTRICT"
            with engine.connect() as connection:
                assert connection.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pg_trigger
                            WHERE tgname = 'trg_role_bindings_last_workspace_admin'
                              AND NOT tgisinternal
                        )
                        """
                    )
                ) is True
            _assert_principal_deletion_boundary(engine, seeded)
        finally:
            engine.dispose()
    finally:
        command.upgrade(config, "head")


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
        with Session(engine) as session, session.begin():
            inactive_admin = Principal(
                issuer="https://postgres-test.example",
                subject=f"inactive-admin-{suffix}",
                principal_type=PrincipalType.USER,
                is_active=False,
            )
            session.add(inactive_admin)
            session.flush()
            session.add(
                RoleBinding(
                    workspace_id=result.workspace_id,
                    principal_id=inactive_admin.id,
                    role=Role.ADMIN,
                )
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
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    update(RoleBinding)
                    .where(RoleBinding.id == result.role_binding_id)
                    .values(role=Role.VIEWER),
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(delete(RoleBinding).where(RoleBinding.id == result.role_binding_id))
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


@pytest.mark.skipif(
    not os.environ.get("LLS_TEST_POSTGRES_URL") or not os.environ.get("LLS_TEST_POSTGRES_APP_URL"),
    reason="PostgreSQL owner/app test URLs are not set",
)
def test_postgres_task_draft_publish_concurrency_permissions_and_triggers():
    owner_url = os.environ["LLS_TEST_POSTGRES_URL"]
    app_url = os.environ["LLS_TEST_POSTGRES_APP_URL"]
    command.upgrade(build_alembic_config(owner_url), "head")
    suffix = uuid.uuid4().hex[:12]
    workspace_slug = f"task-publish-{suffix}"
    task_key = f"task-{suffix}"
    other_task_key = f"other-{suffix}"
    actor_identity = ExternalIdentity("https://task-publish.example", f"admin-{suffix}")
    caller_identity = ExternalIdentity("llm-labeling-scaffold", f"service-{suffix}")

    owner_engine = create_database_engine(owner_url)
    app_engine = create_database_engine(app_url)
    try:
        with Session(owner_engine) as session:
            bootstrap = bootstrap_admin(
                session,
                issuer=actor_identity.issuer,
                subject=actor_identity.subject,
                workspace_slug=workspace_slug,
                workspace_name="Task Publish PostgreSQL",
            )
        owner_service = DatabaseService.from_url(owner_url)
        try:
            owner_service.resolve_or_provision(caller_identity, PrincipalType.SERVICE)
        finally:
            owner_service.close()
        with Session(owner_engine) as session, session.begin():
            task = Task(
                workspace_id=bootstrap.workspace_id,
                task_key=task_key,
                name="Task Publish PostgreSQL",
                created_by_principal_id=bootstrap.principal_id,
            )
            other_task = Task(
                workspace_id=bootstrap.workspace_id,
                task_key=other_task_key,
                name="Other PostgreSQL Task",
                created_by_principal_id=bootstrap.principal_id,
            )
            session.add_all([task, other_task])
            session.flush()
            task_id = task.id
            other_task_id = other_task.id

        app_service = DatabaseService.from_url(app_url)
        try:
            first = app_service.save_task_draft(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition={"task_id": task_key, "state": "a"},
                rendered_task=f"task_id: {task_key}\nstate: a\n",
                if_match=None,
                channel=AuditChannel.MCP,
                if_none_match="*",
            ).draft
        finally:
            app_service.close()

        save_barrier = Barrier(2)

        def save_variant(state: str):
            service = DatabaseService.from_url(app_url)
            try:
                save_barrier.wait()
                try:
                    result = service.save_task_draft(
                        actor_identity=actor_identity,
                        caller_identity=caller_identity,
                        workspace_slug=workspace_slug,
                        task_key=task_key,
                        definition={"task_id": task_key, "state": state},
                        rendered_task=f"task_id: {task_key}\nstate: {state}\n",
                        if_match=first.etag,
                        channel=AuditChannel.MCP,
                    )
                    return "saved", result.draft.etag
                except TaskDraftConflict as exc:
                    return "conflict", exc.current_etag
            finally:
                service.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            save_results = list(executor.map(save_variant, ("b", "c")))
        assert sorted(result[0] for result in save_results) == ["conflict", "saved"]

        app_service = DatabaseService.from_url(app_url)
        try:
            concurrent_draft = app_service.get_task_draft(
                identity=actor_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
            )
            assert concurrent_draft.version == 2
            aba_draft = app_service.save_task_draft(
                actor_identity=actor_identity,
                caller_identity=caller_identity,
                workspace_slug=workspace_slug,
                task_key=task_key,
                definition=first.definition,
                rendered_task=first.rendered_task,
                if_match=concurrent_draft.etag,
                channel=AuditChannel.MCP,
            ).draft
            assert aba_draft.version == 3
            assert aba_draft.fingerprint == first.fingerprint
            assert aba_draft.etag != first.etag
            with pytest.raises(TaskDraftConflict):
                app_service.save_task_draft(
                    actor_identity=actor_identity,
                    caller_identity=caller_identity,
                    workspace_slug=workspace_slug,
                    task_key=task_key,
                    definition=first.definition,
                    rendered_task=first.rendered_task,
                    if_match=first.etag,
                    channel=AuditChannel.MCP,
                )
        finally:
            app_service.close()

        publish_barrier = Barrier(2)

        def publish():
            service = DatabaseService.from_url(app_url)
            try:
                publish_barrier.wait()
                return service.publish_task_draft(
                    actor_identity=actor_identity,
                    caller_identity=caller_identity,
                    workspace_slug=workspace_slug,
                    task_key=task_key,
                    if_match=aba_draft.etag,
                    reason="Concurrent PostgreSQL release",
                    idempotency_key=f"publish-{suffix}",
                    channel=AuditChannel.MCP,
                )
            finally:
                service.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            publish_results = list(executor.map(lambda _: publish(), range(2)))
        assert publish_results[0].revision_id == publish_results[1].revision_id
        assert publish_results[0].materialization_id == publish_results[1].materialization_id
        assert {result.replayed for result in publish_results} == {False, True}
        revision_id = publish_results[0].revision_id

        with Session(owner_engine) as session:
            revision = session.get(TaskRevision, revision_id)
            task = session.get(Task, task_id)
            actor = session.get(Principal, revision.actor_principal_id)
            caller = session.get(Principal, revision.caller_principal_id)
            assert task.current_revision_id is None
            assert (actor.issuer, actor.subject) == (actor_identity.issuer, actor_identity.subject)
            assert (caller.issuer, caller.subject) == (caller_identity.issuer, caller_identity.subject)
            assert revision.channel == AuditChannel.MCP
            assert session.scalar(
                select(func.count()).select_from(TaskRevision).where(TaskRevision.task_id == task_id),
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(TaskRevisionMaterialization).where(
                    TaskRevisionMaterialization.task_id == task_id,
                ),
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.event_type == "task.revision_published",
                    AuditEvent.resource_id == str(revision_id),
                ),
            ) == 1

        with Session(app_engine) as session:
            assert session.scalar(
                text("SELECT has_table_privilege(current_user, 'task_revisions', 'SELECT, INSERT')"),
            ) is True
            assert session.scalar(
                text("SELECT has_table_privilege(current_user, 'task_revisions', 'UPDATE, DELETE')"),
            ) is True
            assert session.scalar(
                text("SELECT has_table_privilege(current_user, 'task_revisions', 'TRUNCATE')"),
            ) is False
        with pytest.raises(DBAPIError):
            with app_engine.begin() as connection:
                connection.execute(
                    update(TaskRevision).where(TaskRevision.id == revision_id).values(reason="overwritten"),
                )
        with pytest.raises(DBAPIError):
            with app_engine.begin() as connection:
                connection.execute(delete(TaskRevision).where(TaskRevision.id == revision_id))
        with owner_engine.connect() as connection:
            assert connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_trigger
                    WHERE tgrelid = 'task_revisions'::regclass
                      AND tgname IN (
                        'trg_task_revisions_immutable',
                        'trg_task_revisions_immutable_truncate'
                      )
                      AND NOT tgisinternal
                    """
                )
            ) == 2
        with pytest.raises(DBAPIError):
            with owner_engine.begin() as connection:
                connection.execute(text("TRUNCATE TABLE task_revisions CASCADE"))
        with pytest.raises(DBAPIError):
            with owner_engine.begin() as connection:
                connection.execute(
                    update(Task)
                    .where(Task.id == other_task_id)
                    .values(current_revision_id=revision_id),
                )
        with pytest.raises(DBAPIError):
            with owner_engine.begin() as connection:
                connection.execute(delete(Task).where(Task.id == task_id))
    finally:
        app_engine.dispose()
        owner_engine.dispose()


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


@pytest.mark.skipif(not os.environ.get("LLS_TEST_POSTGRES_URL"), reason="LLS_TEST_POSTGRES_URL is not set")
def test_postgres_concurrent_first_membership_grant_is_idempotent():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    suffix = uuid.uuid4().hex[:12]
    admin_identity = ExternalIdentity("https://membership-grant.example", f"admin-{suffix}")
    target_identity = ExternalIdentity("https://membership-grant.example", f"target-{suffix}")
    workspace_slug = f"membership-grant-{suffix}"
    setup_engine = create_database_engine(database_url)
    try:
        with Session(setup_engine) as session:
            bootstrap = bootstrap_admin(
                session,
                issuer=admin_identity.issuer,
                subject=admin_identity.subject,
                workspace_slug=workspace_slug,
                workspace_name="Concurrent Membership Grant",
            )
        setup_service = DatabaseService.from_url(database_url)
        try:
            setup_service.resolve_or_provision(target_identity)
        finally:
            setup_service.close()
    finally:
        setup_engine.dispose()

    workspace_lock_attempts = Barrier(3)

    def grant():
        service_engine = create_database_engine(database_url)
        factory = sessionmaker(bind=service_engine, class_=Session, expire_on_commit=False)
        service = DatabaseService(factory, service_engine)
        workspace_lock_seen = False

        def wait_before_workspace_lock(connection, cursor, statement, parameters, context, executemany):
            nonlocal workspace_lock_seen
            normalized = " ".join(statement.lower().split())
            if not workspace_lock_seen and "from workspaces" in normalized and "for update" in normalized:
                workspace_lock_seen = True
                workspace_lock_attempts.wait(timeout=10)

        event.listen(service_engine, "before_cursor_execute", wait_before_workspace_lock)
        try:
            return service.grant_workspace_membership(
                workspace_slug=workspace_slug,
                target_identity=target_identity,
                role=Role.VIEWER,
                actor_identity=admin_identity,
                caller_identity=admin_identity,
                channel=AuditChannel.API,
            )
        finally:
            event.remove(service_engine, "before_cursor_execute", wait_before_workspace_lock)
            service.close()

    lock_engine = create_database_engine(database_url)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            with lock_engine.begin() as connection:
                connection.execute(
                    select(Workspace.id)
                    .where(Workspace.id == bootstrap.workspace_id)
                    .with_for_update()
                )
                futures = [executor.submit(grant) for _ in range(2)]
                workspace_lock_attempts.wait(timeout=10)
            results = [future.result(timeout=20) for future in futures]
    finally:
        lock_engine.dispose()

    assert {result.action for result in results} == {
        MembershipMutationAction.GRANTED,
        MembershipMutationAction.UNCHANGED,
    }
    assert results[0].binding_id == results[1].binding_id

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(RoleBinding).where(
                    RoleBinding.workspace_id == bootstrap.workspace_id,
                    RoleBinding.principal_id == results[0].principal.id,
                    RoleBinding.task_id.is_(None),
                )
            ) == 1
            assert session.scalar(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.workspace_id == bootstrap.workspace_id,
                    AuditEvent.event_type == "workspace.membership_granted",
                )
            ) == 1
    finally:
        engine.dispose()


def _seed_two_postgres_admins(database_url: str, prefix: str):
    suffix = uuid.uuid4().hex[:12]
    first_identity = ExternalIdentity(f"https://{prefix}.example", f"admin-a-{suffix}")
    second_identity = ExternalIdentity(f"https://{prefix}.example", f"admin-b-{suffix}")
    workspace_slug = f"{prefix}-{suffix}"
    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            bootstrap_admin(
                session,
                issuer=first_identity.issuer,
                subject=first_identity.subject,
                workspace_slug=workspace_slug,
                workspace_name="Membership Concurrency",
            )
    finally:
        engine.dispose()
    service = DatabaseService.from_url(database_url)
    try:
        service.resolve_or_provision(second_identity)
        service.grant_workspace_membership(
            workspace_slug=workspace_slug,
            target_identity=second_identity,
            role=Role.ADMIN,
            actor_identity=first_identity,
            caller_identity=first_identity,
            channel=AuditChannel.API,
        )
    finally:
        service.close()
    return workspace_slug, first_identity, second_identity


@pytest.mark.skipif(not os.environ.get("LLS_TEST_POSTGRES_URL"), reason="LLS_TEST_POSTGRES_URL is not set")
def test_postgres_concurrent_membership_revocation_rechecks_actor_after_lock():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    workspace_slug, first_identity, second_identity = _seed_two_postgres_admins(
        database_url,
        "membership-revoke",
    )
    barrier = Barrier(2)

    def revoke(actor: ExternalIdentity, target: ExternalIdentity):
        service = DatabaseService.from_url(database_url)
        try:
            barrier.wait()
            try:
                return service.revoke_workspace_membership(
                    workspace_slug=workspace_slug,
                    target_identity=target,
                    actor_identity=actor,
                    caller_identity=actor,
                    channel=AuditChannel.API,
                )
            except AuthorizationDenied as exc:
                return exc
        finally:
            service.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda pair: revoke(*pair),
                ((first_identity, second_identity), (second_identity, first_identity)),
            )
        )

    assert sum(
        getattr(outcome, "action", None) == MembershipMutationAction.REVOKED
        for outcome in outcomes
    ) == 1
    assert sum(isinstance(outcome, AuthorizationDenied) for outcome in outcomes) == 1

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == workspace_slug))
            assert session.scalar(
                select(func.count()).select_from(RoleBinding).where(
                    RoleBinding.workspace_id == workspace_id,
                    RoleBinding.task_id.is_(None),
                    RoleBinding.role == Role.ADMIN,
                )
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("LLS_TEST_POSTGRES_URL"), reason="LLS_TEST_POSTGRES_URL is not set")
def test_postgres_concurrent_admin_downgrade_preserves_one_admin():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    workspace_slug, first_identity, second_identity = _seed_two_postgres_admins(
        database_url,
        "membership-downgrade",
    )
    barrier = Barrier(2)

    def downgrade(identity: ExternalIdentity):
        service = DatabaseService.from_url(database_url)
        try:
            barrier.wait()
            try:
                return service.change_workspace_membership(
                    workspace_slug=workspace_slug,
                    target_identity=identity,
                    role=Role.VIEWER,
                    actor_identity=identity,
                    caller_identity=identity,
                    channel=AuditChannel.API,
                )
            except LastWorkspaceAdmin as exc:
                return exc
        finally:
            service.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(downgrade, (first_identity, second_identity)))

    assert sum(
        getattr(outcome, "action", None) == MembershipMutationAction.CHANGED
        for outcome in outcomes
    ) == 1
    assert sum(isinstance(outcome, LastWorkspaceAdmin) for outcome in outcomes) == 1

    engine = create_database_engine(database_url)
    try:
        with Session(engine) as session:
            workspace_id = session.scalar(select(Workspace.id).where(Workspace.slug == workspace_slug))
            assert session.scalar(
                select(func.count()).select_from(RoleBinding).where(
                    RoleBinding.workspace_id == workspace_id,
                    RoleBinding.task_id.is_(None),
                    RoleBinding.role == Role.ADMIN,
                )
            ) == 1
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("LLS_TEST_POSTGRES_URL"), reason="LLS_TEST_POSTGRES_URL is not set")
def test_postgres_service_and_direct_sql_membership_updates_share_lock_order():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    workspace_slug, first_identity, second_identity = _seed_two_postgres_admins(
        database_url,
        "membership-mixed-lock",
    )
    lookup_engine = create_database_engine(database_url)
    try:
        with Session(lookup_engine) as session:
            target_binding_id = session.scalar(
                select(RoleBinding.id)
                .join(Principal, Principal.id == RoleBinding.principal_id)
                .join(Workspace, Workspace.id == RoleBinding.workspace_id)
                .where(
                    Workspace.slug == workspace_slug,
                    Principal.issuer == second_identity.issuer,
                    Principal.subject == second_identity.subject,
                    RoleBinding.task_id.is_(None),
                )
            )
    finally:
        lookup_engine.dispose()

    row_locked = Event()
    service_binding_query_started = Event()

    def direct_change():
        direct_engine = create_database_engine(database_url)
        try:
            with direct_engine.begin() as connection:
                connection.execute(
                    select(RoleBinding.id)
                    .where(RoleBinding.id == target_binding_id)
                    .with_for_update()
                )
                row_locked.set()
                assert service_binding_query_started.wait(timeout=10)
                connection.execute(
                    update(RoleBinding)
                    .where(RoleBinding.id == target_binding_id)
                    .values(role=Role.VIEWER)
                )
            return "changed"
        finally:
            direct_engine.dispose()

    def service_change():
        assert row_locked.wait(timeout=10)
        service_engine = create_database_engine(database_url)
        factory = sessionmaker(bind=service_engine, class_=Session, expire_on_commit=False)
        service = DatabaseService(factory, service_engine)

        def capture_binding_lock(connection, cursor, statement, parameters, context, executemany):
            normalized = " ".join(statement.lower().split())
            if "from role_bindings" in normalized and "for update" in normalized:
                service_binding_query_started.set()

        event.listen(service_engine, "before_cursor_execute", capture_binding_lock)
        try:
            return service.change_workspace_membership(
                workspace_slug=workspace_slug,
                target_identity=second_identity,
                role=Role.VIEWER,
                actor_identity=first_identity,
                caller_identity=first_identity,
                channel=AuditChannel.API,
            )
        finally:
            event.remove(service_engine, "before_cursor_execute", capture_binding_lock)
            service.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        direct_future = executor.submit(direct_change)
        service_future = executor.submit(service_change)
        direct_result = direct_future.result(timeout=20)
        service_result = service_future.result(timeout=20)

    assert direct_result == "changed"
    assert service_result.action == MembershipMutationAction.UNCHANGED
