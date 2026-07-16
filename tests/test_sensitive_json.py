from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from llm_labeling_scaffold.db.base import Base
from llm_labeling_scaffold.db.database import create_database_engine
from llm_labeling_scaffold.db.enums import PrincipalType
from llm_labeling_scaffold.db.models import Principal, Task, Workspace

from llm_labeling_scaffold.db.sensitive_json import (
    MAX_CANONICAL_JSON_BYTES,
    SensitiveJSONError,
    canonical_sensitive_json_bytes,
    normalize_sensitive_json_object,
    sqlite_sensitive_json_object_is_valid,
)


def test_sensitive_json_canonicalizes_nfc_and_object_order() -> None:
    first = canonical_sensitive_json_bytes({"label": "e\u0301", "count": 2})
    second = canonical_sensitive_json_bytes({"count": 2, "label": "é"})

    assert first == second == '{"count":2,"label":"é"}'.encode()
    assert normalize_sensitive_json_object({"label": "e\u0301"}) == {"label": "é"}


@pytest.mark.parametrize(
    "key",
    [
        "EMAIL",
        "Password",
        "TOKEN",
        "access_token",
        "Refresh_Token",
        "assertion",
        "private_key",
        "client_secret",
    ],
)
def test_sensitive_json_rejects_exact_sensitive_keys_case_insensitively(key: str) -> None:
    with pytest.raises(SensitiveJSONError):
        canonical_sensitive_json_bytes({key: "redacted"})


def test_sensitive_json_does_not_reject_non_sensitive_key_names() -> None:
    value = {
        "email_templates": {"enabled": True},
        "password_hint": "Use at least twelve characters",
        "tokenizer": "wordpiece",
    }

    assert normalize_sensitive_json_object(value) == value


@pytest.mark.parametrize("key", ["tоken", "ｅmail", "safe-key", "9value", ""])
def test_sensitive_json_rejects_non_ascii_or_non_identifier_keys(key: str) -> None:
    with pytest.raises(SensitiveJSONError):
        canonical_sensitive_json_bytes({key: "value"})


@pytest.mark.parametrize(
    "value",
    [
        "contact admin@example.test for access",
        "prefix eyJhbGciOiJIUzI1NiJ9.e30.signature suffix",
        "Authorization: Bearer abc.def-123",
        "-----BEGIN RSA PRIVATE KEY-----\nredacted",
        "client_secret = redacted-value",
        "token: redacted-value",
        "credential sk-1234567890abcdef",
    ],
)
def test_sensitive_json_rejects_sensitive_strings_at_any_position(value: str) -> None:
    with pytest.raises(SensitiveJSONError):
        canonical_sensitive_json_bytes({"items": ["safe", {"message": value}]})


def test_sensitive_json_enforces_canonical_utf8_size_boundary() -> None:
    empty_size = len(canonical_sensitive_json_bytes({"payload": ""}))
    exact = {"payload": "a" * (MAX_CANONICAL_JSON_BYTES - empty_size)}

    assert len(canonical_sensitive_json_bytes(exact)) == MAX_CANONICAL_JSON_BYTES
    with pytest.raises(SensitiveJSONError, match="64 KiB"):
        canonical_sensitive_json_bytes({"payload": exact["payload"] + "a"})


def test_sensitive_json_enforces_eight_container_levels() -> None:
    value: dict[str, object] = {}
    for _ in range(7):
        value = {"level": value}

    canonical_sensitive_json_bytes(value)
    with pytest.raises(SensitiveJSONError, match="8 container levels"):
        canonical_sensitive_json_bytes({"level": value})


def test_sensitive_json_rejects_cycles_non_string_keys_and_non_json_numbers() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    for value in (
        cyclic,
        {1: "not-a-string-key"},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": (1, 2)},
    ):
        with pytest.raises(SensitiveJSONError):
            canonical_sensitive_json_bytes(value)


def test_sensitive_json_requires_an_object_root() -> None:
    for value in (False, 0, "", []):
        with pytest.raises(SensitiveJSONError, match="must be an object"):
            canonical_sensitive_json_bytes(value)


def test_canonical_json_preserves_boolean_integer_and_float_types() -> None:
    assert canonical_sensitive_json_bytes({"value": True}) != canonical_sensitive_json_bytes({"value": 1})
    assert canonical_sensitive_json_bytes({"value": 1}) != canonical_sensitive_json_bytes({"value": 1.0})


def test_sqlite_validator_fails_closed_for_invalid_and_duplicate_json() -> None:
    valid = json.dumps({"email_templates": {"enabled": True}})

    assert sqlite_sensitive_json_object_is_valid(valid) == 1
    assert sqlite_sensitive_json_object_is_valid('{"tokenizer":"safe","token":"redacted"}') == 0
    assert sqlite_sensitive_json_object_is_valid('{"safe":1,"safe":2}') == 0
    assert sqlite_sensitive_json_object_is_valid('{"value":NaN}') == 0
    assert sqlite_sensitive_json_object_is_valid(b"\xff") == 0


def test_sqlite_create_all_triggers_block_raw_core_sensitive_json() -> None:
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session, session.begin():
            principal = Principal(
                issuer="https://sensitive-json.example",
                subject="admin",
                principal_type=PrincipalType.USER,
            )
            workspace = Workspace(slug="sensitive-json", name="Sensitive JSON")
            session.add_all([principal, workspace])
            session.flush()
            task = Task(
                workspace_id=workspace.id,
                task_key="sensitive-json-task",
                name="Sensitive JSON Task",
                created_by_principal_id=principal.id,
            )
            session.add(task)
            session.flush()
            principal_id = principal.id
            workspace_id = workspace.id
            task_id = task.id

        with engine.connect() as connection:
            triggers = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).scalars()
            )
        assert {
            "trg_idempotency_records_sensitive_json_insert",
            "trg_idempotency_records_sensitive_json_update",
            "trg_workspace_settings_sensitive_json_insert",
            "trg_workspace_settings_sensitive_json_update",
            "trg_audit_events_sensitive_json_insert",
            "trg_audit_events_sensitive_json_update",
        } <= triggers

        exact_size = len(canonical_sensitive_json_bytes({"payload": ""}))
        exact_document = canonical_sensitive_json_bytes(
            {"payload": "a" * (MAX_CANONICAL_JSON_BYTES - exact_size)}
        ).decode()
        depth_eight: dict[str, object] = {}
        for _ in range(7):
            depth_eight = {"level": depth_eight}
        depth_nine = {"level": depth_eight}

        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO workspace_settings (
                    id, workspace_id, setting_key, setting_value, updated_by_principal_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    workspace_id.hex,
                    "email_templates",
                    '{"email_templates":{"enabled":true}}',
                    principal_id.hex,
                ),
            )
            connection.exec_driver_sql(
                """
                INSERT INTO workspace_settings (
                    id, workspace_id, setting_key, setting_value, updated_by_principal_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    workspace_id.hex,
                    "exact_size",
                    exact_document,
                    principal_id.hex,
                ),
            )
            connection.exec_driver_sql(
                """
                INSERT INTO workspace_settings (
                    id, workspace_id, setting_key, setting_value, updated_by_principal_id
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    workspace_id.hex,
                    "depth_eight",
                    json.dumps(depth_eight, separators=(",", ":")),
                    principal_id.hex,
                ),
            )

        invalid_documents = (
            json.dumps({"tоken": "value"}, ensure_ascii=False),
            json.dumps({"message": "contact admin@example.test"}),
            json.dumps({"payload": "a" * (MAX_CANONICAL_JSON_BYTES - exact_size + 1)}),
            json.dumps(depth_nine, separators=(",", ":")),
        )
        for index, document in enumerate(invalid_documents):
            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        """
                        INSERT INTO workspace_settings (
                            id, workspace_id, setting_key, setting_value, updated_by_principal_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            workspace_id.hex,
                            f"invalid_{index}",
                            document,
                            principal_id.hex,
                        ),
                    )

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    INSERT INTO audit_events (
                        id, workspace_id, actor_type, channel, actor_issuer,
                        actor_subject, event_type, details
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        workspace_id.hex,
                        "system",
                        "cli",
                        "llm-labeling-scaffold",
                        "system",
                        "raw.invalid",
                        '{"message":"Bearer secret-token"}',
                    ),
                )

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    INSERT INTO idempotency_records (
                        id, workspace_id, actor_principal_id, caller_principal_id,
                        operation, idempotency_key_hash, request_fingerprint,
                        required_permission, resource_type, resource_id, channel,
                        state, response_status, response_body
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        workspace_id.hex,
                        principal_id.hex,
                        principal_id.hex,
                        "task.publish",
                        hashlib.sha256(b"raw-key").hexdigest(),
                        hashlib.sha256(b"raw-request").hexdigest(),
                        "task:publish",
                        "task",
                        task_id.hex,
                        "api",
                        "succeeded",
                        200,
                        '{"assertion":"redacted"}',
                    ),
                )

        record_id = uuid.uuid4().hex
        with engine.begin() as connection:
            connection.exec_driver_sql(
                """
                INSERT INTO idempotency_records (
                    id, workspace_id, actor_principal_id, caller_principal_id,
                    operation, idempotency_key_hash, request_fingerprint,
                    required_permission, resource_type, resource_id, channel, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    workspace_id.hex,
                    principal_id.hex,
                    principal_id.hex,
                    "task.publish",
                    hashlib.sha256(b"controlled-key").hexdigest(),
                    hashlib.sha256(b"controlled-request").hexdigest(),
                    "task:publish",
                    "task",
                    task_id.hex,
                    "api",
                    "pending",
                ),
            )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE idempotency_records SET state = 'succeeded', response_status = 200 "
                    "WHERE id = ?",
                    (record_id,),
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE idempotency_records SET actor_principal_id = ? WHERE id = ?",
                    (uuid.uuid4().hex, record_id),
                )
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT state, response_status FROM idempotency_records WHERE id = ?",
                (record_id,),
            ).one() == ("pending", None)
    finally:
        engine.dispose()
