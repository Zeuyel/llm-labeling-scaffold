from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.enums import AuditChannel
from llm_labeling_scaffold.db.migration import build_alembic_config
from llm_labeling_scaffold.db.models import (
    AllocationAssignmentItem,
    AnnotatorCohortMember,
    AnnotatorCohortRevision,
)
from tests.test_allocation_schema import (
    ALLOCATION_TABLES,
    _build_graph,
    _build_peer_draft_plan,
    _build_unsealed_cohort_revision,
    _claim,
    _confirm_plan,
    _database_schema_names,
    _hash,
    _metadata_schema_names,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("LLS_TEST_POSTGRES_URL"),
    reason="LLS_TEST_POSTGRES_URL is not set",
)


def _upgrade_engine():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    command.upgrade(build_alembic_config(database_url), "head")
    return create_database_engine(database_url)


def test_postgres_allocation_schema_uses_jsonb_and_rejects_direct_sql_bypasses():
    engine = _upgrade_engine()
    try:
        ids = _build_graph(engine)
        with engine.connect() as connection:
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
            lifecycle_values = connection.execute(
                text(
                    """
                    SELECT enumlabel
                    FROM pg_enum
                    JOIN pg_type ON pg_type.oid = pg_enum.enumtypid
                    WHERE pg_type.typname = 'allocation_plan_lifecycle'
                    ORDER BY enumsortorder
                    """
                )
            ).scalars().all()
        assert {
            ("allocation_plans", "capacity_snapshot"),
            ("allocation_plans", "qc_snapshot"),
            ("allocation_workspace_groups", "member_mapping_ids"),
        } <= jsonb_columns
        assert lifecycle_values == ["draft", "confirmed"]
        inspector = inspect(engine)
        for table_name in sorted(ALLOCATION_TABLES):
            actual_names = _database_schema_names(inspector, table_name)
            assert actual_names == _metadata_schema_names(table_name, engine.dialect.name)
            assert all(
                len(name.encode("utf-8")) <= 63
                for names in actual_names.values()
                for name in names
            )

        with Session(engine) as session, session.begin():
            sealed_insert_claim = _claim(
                session,
                ids["workspace"],
                ids["principal"],
                f"cohort.sealed.insert.postgres.{uuid.uuid4()}",
            )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO annotator_cohort_revisions (
                            id, workspace_id, cohort_id, revision_number, fingerprint,
                            member_count, connection_binding_id, actor_principal_id,
                            caller_principal_id, channel, idempotency_record_id, sealed_at
                        ) VALUES (
                            :id, :workspace_id, :cohort_id, 2, :fingerprint,
                            1, :binding_id, :principal_id,
                            :principal_id, 'api', :idempotency_id, :sealed_at
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "workspace_id": ids["workspace"],
                        "cohort_id": ids["cohort"],
                        "fingerprint": _hash(f"sealed-insert-{uuid.uuid4()}"),
                        "binding_id": ids["binding"],
                        "principal_id": ids["principal"],
                        "idempotency_id": sealed_insert_claim,
                        "sealed_at": datetime.now(timezone.utc),
                    },
                )

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM allocation_plan_states WHERE plan_id = :plan_id"),
                    {"plan_id": ids["plan"]},
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE argilla_connection_bindings "
                        "SET argilla_workspace_id = :new_id WHERE id = :binding_id"
                    ),
                    {"new_id": uuid.uuid4(), "binding_id": ids["binding"]},
                )
        for statement, params in (
            (
                "UPDATE argilla_connection_bindings SET connection_config_id = :value "
                "WHERE id = :row_id",
                {"value": f"replacement-{uuid.uuid4()}", "row_id": ids["binding"]},
            ),
            (
                "UPDATE argilla_connection_bindings SET id = :value WHERE id = :row_id",
                {"value": uuid.uuid4(), "row_id": ids["binding"]},
            ),
            (
                "UPDATE argilla_connection_bindings SET created_at = :value WHERE id = :row_id",
                {"value": datetime(2000, 1, 1, tzinfo=timezone.utc), "row_id": ids["binding"]},
            ),
            (
                "UPDATE argilla_annotator_mappings SET id = :value WHERE id = :row_id",
                {"value": uuid.uuid4(), "row_id": ids["mapping_a"]},
            ),
            (
                "UPDATE argilla_annotator_mappings SET principal_id = :value WHERE id = :row_id",
                {"value": ids["principal"], "row_id": ids["mapping_a"]},
            ),
            (
                "UPDATE argilla_annotator_mappings SET created_at = :value WHERE id = :row_id",
                {"value": datetime(2000, 1, 1, tzinfo=timezone.utc), "row_id": ids["mapping_a"]},
            ),
        ):
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(text(statement), params)
        for table_name, row_id in (
            ("argilla_connection_bindings", ids["binding"]),
            ("argilla_annotator_mappings", ids["mapping_a"]),
        ):
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        text(f"DELETE FROM {table_name} WHERE id = :row_id"),
                        {"row_id": row_id},
                    )

        with Session(engine) as session, session.begin():
            reinsert_item = AllocationAssignmentItem(
                workspace_id=ids["workspace"],
                plan_id=ids["plan"],
                dataset_group_id=ids["direct_dataset"],
                record_id=f"postgres-reinsert-{uuid.uuid4()}",
                external_id=f"postgres-reinsert-{uuid.uuid4()}",
                batch_id="default",
                content_hash=_hash(f"postgres-reinsert-{uuid.uuid4()}"),
            )
            session.add(reinsert_item)
            session.flush()
            reinsert_item_id = reinsert_item.id
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM allocation_record_bindings WHERE item_id = :item_id"),
                    {"item_id": reinsert_item_id},
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO allocation_record_bindings (
                            item_id, workspace_id, plan_id,
                            argilla_dataset_id, argilla_record_id, bound_at
                        ) VALUES (
                            :item_id, :workspace_id, :plan_id,
                            :dataset_id, :record_id, :bound_at
                        )
                        """
                    ),
                    {
                        "item_id": reinsert_item_id,
                        "workspace_id": ids["workspace"],
                        "plan_id": ids["plan"],
                        "dataset_id": ids["remote_direct_dataset"],
                        "record_id": uuid.uuid4(),
                        "bound_at": datetime.now(timezone.utc),
                    },
                )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE allocation_record_bindings
                    SET argilla_dataset_id = :dataset_id,
                        argilla_record_id = :record_id,
                        bound_at = :bound_at
                    WHERE item_id = :item_id
                    """
                ),
                {
                    "item_id": reinsert_item_id,
                    "dataset_id": ids["remote_direct_dataset"],
                    "record_id": uuid.uuid4(),
                    "bound_at": datetime.now(timezone.utc),
                },
            )

        for statement, params in (
            (
                """
                INSERT INTO allocation_assignments (
                    id, workspace_id, plan_id, planner_assignment_id, item_id,
                    phase, role, assignee_mapping_id, required_submissions
                ) VALUES (
                    :id, :workspace_id, :plan_id, 'pg-direct-on-pooled', :item_id,
                    'production', 'primary', :mapping_id, 1
                )
                """,
                {
                    "id": uuid.uuid4(),
                    "workspace_id": ids["workspace"],
                    "plan_id": ids["plan"],
                    "item_id": ids["pooled_item"],
                    "mapping_id": ids["mapping_a"],
                },
            ),
            (
                """
                INSERT INTO allocation_assignments (
                    id, workspace_id, plan_id, planner_assignment_id, item_id,
                    phase, role, pool_id, required_submissions
                ) VALUES (
                    :id, :workspace_id, :plan_id, 'pg-pooled-on-direct', :item_id,
                    'production', 'shared', :pool_id, 1
                )
                """,
                {
                    "id": uuid.uuid4(),
                    "workspace_id": ids["workspace"],
                    "plan_id": ids["plan"],
                    "item_id": ids["direct_item"],
                    "pool_id": ids["cohort"],
                },
            ),
            (
                "UPDATE allocation_workspace_groups SET assignee_mapping_id = :mapping_id "
                "WHERE id = :group_id",
                {"mapping_id": ids["mapping_b"], "group_id": ids["personal_group"]},
            ),
            (
                "UPDATE allocation_workspace_groups SET mode = 'shared', assignee_mapping_id = NULL "
                "WHERE id = :group_id",
                {"group_id": ids["personal_group"]},
            ),
            (
                "UPDATE allocation_dataset_groups SET assignee_mapping_id = :mapping_id "
                "WHERE id = :dataset_id",
                {"mapping_id": ids["mapping_b"], "dataset_id": ids["direct_dataset"]},
            ),
            (
                "UPDATE allocation_dataset_groups SET min_submitted = 1 WHERE id = :dataset_id",
                {"dataset_id": ids["pooled_dataset"]},
            ),
        ):
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(text(statement), params)

        peer = _build_peer_draft_plan(engine, ids)
        _confirm_plan(engine, ids)
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE allocation_assignments
                        SET plan_id = :draft_plan_id, item_id = :draft_item_id
                        WHERE id = :assignment_id
                        """
                    ),
                    {
                        "draft_plan_id": peer["plan"],
                        "draft_item_id": peer["item"],
                        "assignment_id": ids["direct_assignment"],
                    },
                )

        with Session(engine) as session, session.begin():
            quarantine_claims = [
                _claim(
                    session,
                    ids["workspace"],
                    ids["principal"],
                    f"receipt.quarantine.postgres.{uuid.uuid4()}",
                )
                for _ in range(2)
            ]
        for rejection_reason, claim_id in ((None, quarantine_claims[0]), ("", quarantine_claims[1])):
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            INSERT INTO allocation_collection_receipts (
                                id, workspace_id, plan_id, item_id, assignment_id,
                                connection_binding_id, argilla_dataset_id, argilla_record_id,
                                argilla_response_id, argilla_respondent_id, response_status,
                                disposition, rejection_reason, labels_hash, pulled_at,
                                actor_principal_id, caller_principal_id, channel,
                                idempotency_record_id
                            ) VALUES (
                                :id, :workspace_id, :plan_id, :item_id, NULL,
                                :binding_id, :dataset_id, :record_id,
                                :response_id, :respondent_id, 'submitted',
                                'quarantined', :rejection_reason, :labels_hash, :pulled_at,
                                :principal_id, :principal_id, 'worker', :idempotency_id
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "workspace_id": ids["workspace"],
                            "plan_id": ids["plan"],
                            "item_id": ids["direct_item"],
                            "binding_id": ids["binding"],
                            "dataset_id": ids["remote_direct_dataset"],
                            "record_id": ids["remote_direct_record"],
                            "response_id": uuid.uuid4(),
                            "respondent_id": ids["remote_user_a"],
                            "rejection_reason": rejection_reason,
                            "labels_hash": _hash("postgres-quarantine-labels"),
                            "pulled_at": datetime.now(timezone.utc),
                            "principal_id": ids["principal"],
                            "idempotency_id": claim_id,
                        },
                    )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE TABLE allocation_collection_receipts"))
    finally:
        engine.dispose()


def test_postgres_draft_plan_delete_cascades_without_allowing_direct_child_delete():
    engine = _upgrade_engine()
    try:
        ids = _build_graph(engine)
        for table_name, row_id in (
            ("allocation_workspace_groups", ids["personal_group"]),
            ("allocation_dataset_groups", ids["direct_dataset"]),
            ("allocation_assignment_items", ids["direct_item"]),
            ("allocation_assignments", ids["direct_assignment"]),
            ("allocation_dataset_group_states", ids["direct_dataset"]),
            ("allocation_record_bindings", ids["direct_item"]),
        ):
            key_column = {
                "allocation_workspace_groups": "id",
                "allocation_dataset_groups": "id",
                "allocation_assignment_items": "id",
                "allocation_assignments": "id",
                "allocation_dataset_group_states": "dataset_group_id",
                "allocation_record_bindings": "item_id",
            }[table_name]
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        text(f"DELETE FROM {table_name} WHERE {key_column} = :row_id"),
                        {"row_id": row_id},
                    )

        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM allocation_plans WHERE id = :plan_id"),
                {"plan_id": ids["plan"]},
            )
        with engine.connect() as connection:
            for table_name in (
                "allocation_plan_states",
                "allocation_workspace_groups",
                "allocation_dataset_groups",
                "allocation_dataset_group_states",
                "allocation_assignment_items",
                "allocation_record_bindings",
                "allocation_assignments",
            ):
                assert connection.scalar(
                    text(f"SELECT count(*) FROM {table_name} WHERE plan_id = :plan_id"),
                    {"plan_id": ids["plan"]},
                ) == 0
            assert connection.scalar(
                text("SELECT count(*) FROM argilla_connection_bindings WHERE id = :binding_id"),
                {"binding_id": ids["binding"]},
            ) == 1
    finally:
        engine.dispose()


def test_postgres_cohort_member_and_transition_created_at_are_frozen():
    engine = _upgrade_engine()
    try:
        ids = _build_graph(engine)
        unsealed = _build_unsealed_cohort_revision(engine, ids)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE annotator_cohort_members SET default_capacity = 11 WHERE id = :member_id"),
                {"member_id": unsealed["member"]},
            )
        for statement, value in (
            (
                "UPDATE annotator_cohort_members SET cohort_revision_id = :value "
                "WHERE id = :member_id",
                ids["cohort_revision"],
            ),
            (
                "UPDATE annotator_cohort_members SET workspace_id = :value WHERE id = :member_id",
                uuid.uuid4(),
            ),
            (
                "UPDATE annotator_cohort_members SET annotator_mapping_id = :value "
                "WHERE id = :member_id",
                ids["mapping_b"],
            ),
            (
                "UPDATE annotator_cohort_members SET created_at = :value WHERE id = :member_id",
                datetime(2000, 1, 1, tzinfo=timezone.utc),
            ),
        ):
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        text(statement),
                        {"value": value, "member_id": unsealed["member"]},
                    )

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE annotator_cohort_revisions
                        SET sealed_at = :sealed_at,
                            connection_binding_id = :binding_id,
                            created_at = :created_at
                        WHERE id = :revision_id
                        """
                    ),
                    {
                        "sealed_at": datetime.now(timezone.utc),
                        "binding_id": ids["binding"],
                        "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                        "revision_id": unsealed["revision"],
                    },
                )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE annotator_cohort_revisions
                    SET sealed_at = :sealed_at, connection_binding_id = :binding_id
                    WHERE id = :revision_id
                    """
                ),
                {
                    "sealed_at": datetime.now(timezone.utc),
                    "binding_id": ids["binding"],
                    "revision_id": unsealed["revision"],
                },
            )

        with Session(engine) as session, session.begin():
            confirmation_id = _claim(
                session,
                ids["workspace"],
                ids["principal"],
                f"allocation.confirm.created-at.postgres.{uuid.uuid4()}",
            )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE allocation_plan_states
                        SET lifecycle_state = 'confirmed',
                            confirmed_at = :confirmed_at,
                            confirmed_by_principal_id = :principal_id,
                            confirmed_via_caller_principal_id = :principal_id,
                            confirmation_idempotency_record_id = :confirmation_id,
                            created_at = :created_at
                        WHERE plan_id = :plan_id
                        """
                    ),
                    {
                        "confirmed_at": datetime.now(timezone.utc),
                        "principal_id": ids["principal"],
                        "confirmation_id": confirmation_id,
                        "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
                        "plan_id": ids["plan"],
                    },
                )
        _confirm_plan(engine, ids)
    finally:
        engine.dispose()


def test_postgres_confirmation_locks_old_and_new_state_before_child_move():
    engine = _upgrade_engine()
    try:
        ids = _build_graph(engine)
        peer = _build_peer_draft_plan(engine, ids)
        with Session(engine) as session, session.begin():
            confirmation_id = _claim(
                session,
                ids["workspace"],
                ids["principal"],
                f"allocation.confirm.concurrent.{uuid.uuid4()}",
            )

        state_updated = Event()

        def confirm_plan() -> None:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE allocation_plan_states
                        SET lifecycle_state = 'confirmed',
                            confirmed_at = :confirmed_at,
                            confirmed_by_principal_id = :principal_id,
                            confirmed_via_caller_principal_id = :principal_id,
                            confirmation_idempotency_record_id = :confirmation_id
                        WHERE plan_id = :plan_id
                        """
                    ),
                    {
                        "confirmed_at": datetime.now(timezone.utc),
                        "principal_id": ids["principal"],
                        "confirmation_id": confirmation_id,
                        "plan_id": ids["plan"],
                    },
                )
                state_updated.set()
                time.sleep(0.5)

        def move_child() -> BaseException | None:
            assert state_updated.wait(timeout=5)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            UPDATE allocation_assignments
                            SET plan_id = :draft_plan_id, item_id = :draft_item_id
                            WHERE id = :assignment_id
                            """
                        ),
                        {
                            "draft_plan_id": peer["plan"],
                            "draft_item_id": peer["item"],
                            "assignment_id": ids["direct_assignment"],
                        },
                    )
            except BaseException as exc:
                return exc
            return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            confirm_future = executor.submit(confirm_plan)
            move_future = executor.submit(move_child)
            confirm_future.result(timeout=10)
            move_error = move_future.result(timeout=10)

        assert isinstance(move_error, DBAPIError)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT plan_id, item_id FROM allocation_assignments WHERE id = :assignment_id"
                ),
                {"assignment_id": ids["direct_assignment"]},
            ).one()
        assert row.plan_id == ids["plan"]
        assert row.item_id == ids["direct_item"]
    finally:
        engine.dispose()


def test_postgres_confirmation_serializes_parent_update():
    engine = _upgrade_engine()
    try:
        ids = _build_graph(engine)
        with Session(engine) as session, session.begin():
            confirmation_id = _claim(
                session,
                ids["workspace"],
                ids["principal"],
                f"allocation.confirm.parent.concurrent.{uuid.uuid4()}",
            )

        state_updated = Event()

        def confirm_plan() -> None:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE allocation_plan_states
                        SET lifecycle_state = 'confirmed',
                            confirmed_at = :confirmed_at,
                            confirmed_by_principal_id = :principal_id,
                            confirmed_via_caller_principal_id = :principal_id,
                            confirmation_idempotency_record_id = :confirmation_id
                        WHERE plan_id = :plan_id
                        """
                    ),
                    {
                        "confirmed_at": datetime.now(timezone.utc),
                        "principal_id": ids["principal"],
                        "confirmation_id": confirmation_id,
                        "plan_id": ids["plan"],
                    },
                )
                state_updated.set()
                time.sleep(0.5)

        def update_parent() -> BaseException | None:
            assert state_updated.wait(timeout=5)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE allocation_dataset_groups "
                            "SET min_submitted = 1 WHERE id = :dataset_id"
                        ),
                        {"dataset_id": ids["pooled_dataset"]},
                    )
            except BaseException as exc:
                return exc
            return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            confirm_future = executor.submit(confirm_plan)
            update_future = executor.submit(update_parent)
            confirm_future.result(timeout=10)
            update_error = update_future.result(timeout=10)

        assert isinstance(update_error, DBAPIError)
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT min_submitted FROM allocation_dataset_groups WHERE id = :dataset_id"),
                {"dataset_id": ids["pooled_dataset"]},
            ) == 2
    finally:
        engine.dispose()


def test_postgres_cohort_seal_locks_revision_before_member_insert():
    engine = _upgrade_engine()
    try:
        ids = _build_graph(engine)
        with Session(engine) as session, session.begin():
            revision = AnnotatorCohortRevision(
                workspace_id=ids["workspace"],
                cohort_id=ids["cohort"],
                revision_number=2,
                fingerprint=_hash(f"cohort-concurrent-{uuid.uuid4()}"),
                member_count=1,
                actor_principal_id=ids["principal"],
                caller_principal_id=ids["principal"],
                channel=AuditChannel.API,
                idempotency_record_id=_claim(
                    session,
                    ids["workspace"],
                    ids["principal"],
                    f"cohort.revision.concurrent.{uuid.uuid4()}",
                ),
            )
            session.add(revision)
            session.flush()
            session.add(
                AnnotatorCohortMember(
                    workspace_id=ids["workspace"],
                    cohort_revision_id=revision.id,
                    annotator_mapping_id=ids["mapping_a"],
                    default_capacity=10,
                )
            )
            session.flush()
            revision_id = revision.id

        revision_locked = Event()

        def seal_revision() -> None:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE annotator_cohort_revisions
                        SET sealed_at = :sealed_at, connection_binding_id = :binding_id
                        WHERE id = :revision_id
                        """
                    ),
                    {
                        "sealed_at": datetime.now(timezone.utc),
                        "binding_id": ids["binding"],
                        "revision_id": revision_id,
                    },
                )
                revision_locked.set()
                time.sleep(0.5)

        def insert_member() -> BaseException | None:
            assert revision_locked.wait(timeout=5)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            INSERT INTO annotator_cohort_members (
                                id, workspace_id, cohort_revision_id,
                                annotator_mapping_id, default_capacity, created_at
                            ) VALUES (
                                :id, :workspace_id, :revision_id,
                                :mapping_id, 10, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "workspace_id": ids["workspace"],
                            "revision_id": revision_id,
                            "mapping_id": ids["mapping_b"],
                        },
                    )
            except BaseException as exc:
                return exc
            return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            seal_future = executor.submit(seal_revision)
            insert_future = executor.submit(insert_member)
            seal_future.result(timeout=10)
            insert_error = insert_future.result(timeout=10)

        assert isinstance(insert_error, DBAPIError)
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT sealed_at, connection_binding_id,
                           (SELECT count(*) FROM annotator_cohort_members member
                            WHERE member.cohort_revision_id = revision.id)
                    FROM annotator_cohort_revisions revision
                    WHERE revision.id = :revision_id
                    """
                ),
                {"revision_id": revision_id},
            ).one()
        assert row.sealed_at is not None
        assert row.connection_binding_id == ids["binding"]
        assert row[2] == 1
    finally:
        engine.dispose()


def test_postgres_allocation_migration_downgrade_and_reupgrade():
    database_url = os.environ["LLS_TEST_POSTGRES_URL"]
    config = build_alembic_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "20260714_0002")
    engine = create_database_engine(database_url)
    try:
        assert ALLOCATION_TABLES.isdisjoint(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT to_regtype('allocation_plan_lifecycle') IS NULL")
            ) is True
            assert connection.scalar(
                text("SELECT to_regprocedure('lls_confirmed_plan_child_guard()') IS NULL")
            ) is True
            assert connection.scalar(
                text("SELECT to_regprocedure('lls_validate_allocation_plan_graph(uuid)') IS NULL")
            ) is True
    finally:
        engine.dispose()
    command.upgrade(config, "head")
    engine = create_database_engine(database_url)
    try:
        assert ALLOCATION_TABLES <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT to_regprocedure('lls_validate_allocation_plan_graph(uuid)') IS NOT NULL")
            ) is True
    finally:
        engine.dispose()
