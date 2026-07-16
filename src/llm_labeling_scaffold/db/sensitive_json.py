from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from typing import Any

from sqlalchemy import JSON, DDL, Engine, event
from sqlalchemy.types import TypeDecorator


MAX_CANONICAL_JSON_BYTES = 64 * 1024
MAX_JSON_CONTAINER_DEPTH = 8
SQLITE_VALIDATOR_FUNCTION = "lls_sensitive_json_object_is_valid"

_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z", re.ASCII)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,63}", re.ASCII)
_JWT = re.compile(
    r"(?:^|[^A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*[.][A-Za-z0-9_-]+[.][A-Za-z0-9_-]+"
    r"(?:$|[^A-Za-z0-9_-])",
    re.ASCII,
)
_BEARER = re.compile(
    r"(?:^|[^A-Za-z0-9_])bearer\s+[A-Za-z0-9._~+/-]+=*",
    re.ASCII | re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----",
    re.ASCII | re.IGNORECASE,
)
_EXPLICIT_SECRET = re.compile(
    r"(?:^|[^A-Za-z0-9_])"
    r"(?:secret|token|password|api_key|client_secret|access_token|refresh_token|id_token|"
    r"assertion|private_key|authorization)\s*[:=]\s*\S",
    re.ASCII | re.IGNORECASE,
)
_KNOWN_TOKEN = re.compile(
    r"(?:^|[^A-Za-z0-9_])"
    r"(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})"
    r"(?:$|[^A-Za-z0-9_-])",
    re.ASCII | re.IGNORECASE,
)

SENSITIVE_JSON_KEYS = frozenset(
    {
        "access_token",
        "actor_email_snapshot",
        "api_key",
        "api_token",
        "assertion",
        "auth_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "email",
        "email_address",
        "email_snapshot",
        "id_token",
        "passphrase",
        "password",
        "private_key",
        "private_key_pem",
        "refresh_token",
        "saml_assertion",
        "secret",
        "secret_key",
        "session_token",
        "token",
    }
)


class SensitiveJSONError(ValueError):
    pass


def normalize_sensitive_json_object(value: Any) -> dict[str, Any]:
    normalized, _ = _normalize_document(value, require_object=True)
    return normalized


def canonical_sensitive_json_bytes(value: Any, *, require_object: bool = True) -> bytes:
    _, canonical = _normalize_document(value, require_object=require_object)
    return canonical


def sqlite_sensitive_json_object_is_valid(raw_value: Any) -> int:
    try:
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode("utf-8", errors="strict")
        if not isinstance(raw_value, str):
            return 0
        parsed = json.loads(
            raw_value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        canonical_sensitive_json_bytes(parsed)
    except (SensitiveJSONError, TypeError, UnicodeError, ValueError):
        return 0
    return 1


class SensitiveJSONObject(TypeDecorator[Any]):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(JSON(none_as_null=True))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        return normalize_sensitive_json_object(value)


SENSITIVE_JSON_DOCUMENT = SensitiveJSONObject()

_POSTGRES_SENSITIVE_KEYS = ", ".join(f"'{key}'" for key in sorted(SENSITIVE_JSON_KEYS))

POSTGRES_CREATE_SENSITIVE_JSON_FUNCTIONS = (
    """
    CREATE OR REPLACE FUNCTION lls_canonical_sensitive_json_text(document json)
    RETURNS text
    LANGUAGE plpgsql
    IMMUTABLE
    STRICT
    PARALLEL SAFE
    AS $$
    DECLARE
        item_key text;
        item_value json;
        result text;
        first_item boolean;
    BEGIN
        CASE json_typeof(document)
            WHEN 'object' THEN
                result := '{';
                first_item := TRUE;
                FOR item_key, item_value IN
                    SELECT key, value
                    FROM json_each(document)
                    ORDER BY key COLLATE "C"
                LOOP
                    IF NOT first_item THEN
                        result := result || ',';
                    END IF;
                    result := result || to_json(item_key)::text || ':'
                        || lls_canonical_sensitive_json_text(item_value);
                    first_item := FALSE;
                END LOOP;
                RETURN result || '}';
            WHEN 'array' THEN
                result := '[';
                first_item := TRUE;
                FOR item_value IN SELECT value FROM json_array_elements(document)
                LOOP
                    IF NOT first_item THEN
                        result := result || ',';
                    END IF;
                    result := result || lls_canonical_sensitive_json_text(item_value);
                    first_item := FALSE;
                END LOOP;
                RETURN result || ']';
            WHEN 'string' THEN
                RETURN to_json(normalize(document #>> '{}', NFC))::text;
            ELSE
                RETURN document::text;
        END CASE;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_sensitive_json_string_is_safe(value text)
    RETURNS boolean
    LANGUAGE sql
    IMMUTABLE
    STRICT
    PARALLEL SAFE
    AS $$
        SELECT NOT (
            value ~ '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,63}'
            OR value ~ '(^|[^A-Za-z0-9_-])eyJ[A-Za-z0-9_-]*[.][A-Za-z0-9_-]+[.][A-Za-z0-9_-]+($|[^A-Za-z0-9_-])'
            OR value ~* '(^|[^A-Za-z0-9_])bearer[[:space:]]+[A-Za-z0-9._~+/-]+=*'
            OR value ~* '-----BEGIN( [A-Z0-9]+)* PRIVATE KEY-----'
            OR value ~* '(^|[^A-Za-z0-9_])(secret|token|password|api_key|client_secret|access_token|refresh_token|id_token|assertion|private_key|authorization)[[:space:]]*[:=][[:space:]]*[^[:space:]]'
            OR value ~* '(^|[^A-Za-z0-9_])(sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})($|[^A-Za-z0-9_-])'
        )
    $$
    """,
    f"""
    CREATE OR REPLACE FUNCTION lls_sensitive_json_node_is_valid(document json, current_depth integer)
    RETURNS boolean
    LANGUAGE plpgsql
    IMMUTABLE
    STRICT
    PARALLEL SAFE
    AS $$
    DECLARE
        item_key text;
        item_value json;
        node_type text;
    BEGIN
        node_type := json_typeof(document);
        IF node_type IN ('object', 'array') AND current_depth > {MAX_JSON_CONTAINER_DEPTH} THEN
            RETURN FALSE;
        END IF;
        IF node_type = 'object' THEN
            IF EXISTS (
                SELECT 1
                FROM json_each(document)
                GROUP BY key
                HAVING count(*) > 1
            ) THEN
                RETURN FALSE;
            END IF;
            FOR item_key, item_value IN SELECT key, value FROM json_each(document)
            LOOP
                IF item_key !~ '^[A-Za-z_][A-Za-z0-9_]*$'
                   OR lower(item_key) = ANY (ARRAY[{_POSTGRES_SENSITIVE_KEYS}]::text[])
                THEN
                    RETURN FALSE;
                END IF;
                IF NOT lls_sensitive_json_node_is_valid(item_value, current_depth + 1) THEN
                    RETURN FALSE;
                END IF;
            END LOOP;
        ELSIF node_type = 'array' THEN
            FOR item_value IN SELECT value FROM json_array_elements(document)
            LOOP
                IF NOT lls_sensitive_json_node_is_valid(item_value, current_depth + 1) THEN
                    RETURN FALSE;
                END IF;
            END LOOP;
        ELSIF node_type = 'string' THEN
            RETURN lls_sensitive_json_string_is_safe(normalize(document #>> '{{}}', NFC));
        END IF;
        RETURN TRUE;
    END;
    $$
    """,
    f"""
    CREATE OR REPLACE FUNCTION lls_sensitive_json_object_is_valid(document json)
    RETURNS boolean
    LANGUAGE plpgsql
    IMMUTABLE
    STRICT
    PARALLEL SAFE
    AS $$
    BEGIN
        IF json_typeof(document) <> 'object' THEN
            RETURN FALSE;
        END IF;
        IF NOT lls_sensitive_json_node_is_valid(document, 1) THEN
            RETURN FALSE;
        END IF;
        RETURN octet_length(convert_to(lls_canonical_sensitive_json_text(document), 'UTF8'))
            <= {MAX_CANONICAL_JSON_BYTES};
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_validate_idempotency_response_body()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF NEW.response_body IS NOT NULL
           AND NOT lls_sensitive_json_object_is_valid(NEW.response_body)
        THEN
            RAISE EXCEPTION 'invalid idempotency response_body'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_idempotency_records_response_body_sensitive_json';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_validate_workspace_setting_value()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF NEW.setting_value IS NULL
           OR NOT lls_sensitive_json_object_is_valid(NEW.setting_value)
        THEN
            RAISE EXCEPTION 'invalid workspace setting_value'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_workspace_settings_setting_value_sensitive_json';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_validate_audit_event_details()
    RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF NEW.details IS NULL
           OR NOT lls_sensitive_json_object_is_valid(NEW.details)
        THEN
            RAISE EXCEPTION 'invalid audit event details'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_audit_events_details_sensitive_json';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
)

POSTGRES_DROP_SENSITIVE_JSON_FUNCTIONS = (
    "DROP FUNCTION IF EXISTS lls_validate_audit_event_details()",
    "DROP FUNCTION IF EXISTS lls_validate_workspace_setting_value()",
    "DROP FUNCTION IF EXISTS lls_validate_idempotency_response_body()",
    "DROP FUNCTION IF EXISTS lls_sensitive_json_object_is_valid(json)",
    "DROP FUNCTION IF EXISTS lls_sensitive_json_node_is_valid(json, integer)",
    "DROP FUNCTION IF EXISTS lls_sensitive_json_object_is_valid(jsonb)",
    "DROP FUNCTION IF EXISTS lls_sensitive_json_node_is_valid(jsonb, integer)",
    "DROP FUNCTION IF EXISTS lls_sensitive_json_string_is_safe(text)",
    "DROP FUNCTION IF EXISTS lls_canonical_sensitive_json_text(json)",
    "DROP FUNCTION IF EXISTS lls_canonical_sensitive_json_text(jsonb)",
)

POSTGRES_SENSITIVE_JSON_TRIGGERS = (
    """
    CREATE TRIGGER trg_idempotency_records_sensitive_json
    BEFORE INSERT OR UPDATE OF response_body ON idempotency_records
    FOR EACH ROW EXECUTE FUNCTION lls_validate_idempotency_response_body()
    """,
    """
    CREATE TRIGGER trg_workspace_settings_sensitive_json
    BEFORE INSERT OR UPDATE OF setting_value ON workspace_settings
    FOR EACH ROW EXECUTE FUNCTION lls_validate_workspace_setting_value()
    """,
    """
    CREATE TRIGGER trg_audit_events_sensitive_json
    BEFORE INSERT OR UPDATE OF details ON audit_events
    FOR EACH ROW EXECUTE FUNCTION lls_validate_audit_event_details()
    """,
)

POSTGRES_DROP_SENSITIVE_JSON_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_audit_events_sensitive_json ON audit_events",
    "DROP TRIGGER IF EXISTS trg_workspace_settings_sensitive_json ON workspace_settings",
    "DROP TRIGGER IF EXISTS trg_idempotency_records_sensitive_json ON idempotency_records",
)


def sqlite_sensitive_json_trigger_statements(
    table_name: str,
    column_name: str,
    *,
    nullable: bool,
) -> tuple[str, str]:
    invalid = f"COALESCE({SQLITE_VALIDATOR_FUNCTION}(NEW.{column_name}), 0) <> 1"
    if nullable:
        invalid = f"NEW.{column_name} IS NOT NULL AND {invalid}"
    else:
        invalid = f"NEW.{column_name} IS NULL OR {invalid}"
    return tuple(
        f"""
        CREATE TRIGGER trg_{table_name}_sensitive_json_{operation.lower()}
        BEFORE {operation} ON {table_name}
        WHEN {invalid}
        BEGIN
            SELECT RAISE(ABORT, 'invalid {table_name}.{column_name} sensitive JSON');
        END
        """
        for operation in ("INSERT", "UPDATE")
    )


SQLITE_SENSITIVE_JSON_TRIGGERS = (
    *sqlite_sensitive_json_trigger_statements(
        "idempotency_records",
        "response_body",
        nullable=True,
    ),
    *sqlite_sensitive_json_trigger_statements(
        "workspace_settings",
        "setting_value",
        nullable=False,
    ),
    *sqlite_sensitive_json_trigger_statements(
        "audit_events",
        "details",
        nullable=False,
    ),
)

SQLITE_DROP_SENSITIVE_JSON_TRIGGERS = tuple(
    f"DROP TRIGGER IF EXISTS trg_{table_name}_sensitive_json_{operation}"
    for table_name in ("audit_events", "workspace_settings", "idempotency_records")
    for operation in ("insert", "update")
)


def install_sensitive_json_schema_events(metadata, tables: tuple[Any, Any, Any]) -> None:
    event.listen(metadata, "before_create", _ensure_sqlite_function_on_connection)
    event.listen(metadata, "before_create", _create_postgres_sensitive_json_functions)
    for table, postgres_statement, sqlite_statements in zip(
        tables,
        POSTGRES_SENSITIVE_JSON_TRIGGERS,
        (
            SQLITE_SENSITIVE_JSON_TRIGGERS[0:2],
            SQLITE_SENSITIVE_JSON_TRIGGERS[2:4],
            SQLITE_SENSITIVE_JSON_TRIGGERS[4:6],
        ),
        strict=True,
    ):
        event.listen(
            table,
            "after_create",
            DDL(postgres_statement).execute_if(dialect="postgresql"),
        )
        for statement in sqlite_statements:
            event.listen(
                table,
                "after_create",
                DDL(statement).execute_if(dialect="sqlite"),
            )
    for statement in POSTGRES_DROP_SENSITIVE_JSON_FUNCTIONS:
        event.listen(
            metadata,
            "after_drop",
            DDL(statement).execute_if(dialect="postgresql"),
        )


def _create_postgres_sensitive_json_functions(metadata, connection, **kwargs) -> None:
    if connection.dialect.name != "postgresql":
        return
    for statement in POSTGRES_CREATE_SENSITIVE_JSON_FUNCTIONS:
        connection.exec_driver_sql(statement.replace("%", "%%"))


def _normalize_document(value: Any, *, require_object: bool) -> tuple[Any, bytes]:
    normalized = _normalize_node(value, container_depth=0, active_containers=set())
    if require_object and type(normalized) is not dict:
        raise SensitiveJSONError("sensitive JSON must be an object")
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(canonical) > MAX_CANONICAL_JSON_BYTES:
        raise SensitiveJSONError("canonical sensitive JSON exceeds 64 KiB")
    return normalized, canonical


def _normalize_node(value: Any, *, container_depth: int, active_containers: set[int]) -> Any:
    value_type = type(value)
    if value_type is dict:
        return _normalize_object(value, container_depth, active_containers)
    if value_type is list:
        return _normalize_array(value, container_depth, active_containers)
    if value_type is str:
        normalized = unicodedata.normalize("NFC", value)
        if _contains_sensitive_string(normalized):
            raise SensitiveJSONError("sensitive string values are not allowed")
        return normalized
    if value is None or value_type is bool or value_type is int:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise SensitiveJSONError("non-finite numbers are not valid JSON")
        return value
    raise SensitiveJSONError(f"unsupported JSON value type: {value_type.__name__}")


def _normalize_object(
    value: dict[Any, Any],
    container_depth: int,
    active_containers: set[int],
) -> dict[str, Any]:
    depth = container_depth + 1
    if depth > MAX_JSON_CONTAINER_DEPTH:
        raise SensitiveJSONError("sensitive JSON exceeds 8 container levels")
    identity = id(value)
    if identity in active_containers:
        raise SensitiveJSONError("cyclic JSON data is not allowed")
    active_containers.add(identity)
    try:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise SensitiveJSONError("JSON object keys must be strings")
            if _SAFE_KEY.fullmatch(key) is None:
                raise SensitiveJSONError("JSON object keys must be safe ASCII identifiers")
            if key.lower() in SENSITIVE_JSON_KEYS:
                raise SensitiveJSONError("sensitive JSON object keys are not allowed")
            normalized[key] = _normalize_node(
                item,
                container_depth=depth,
                active_containers=active_containers,
            )
        return normalized
    finally:
        active_containers.remove(identity)


def _normalize_array(
    value: list[Any],
    container_depth: int,
    active_containers: set[int],
) -> list[Any]:
    depth = container_depth + 1
    if depth > MAX_JSON_CONTAINER_DEPTH:
        raise SensitiveJSONError("sensitive JSON exceeds 8 container levels")
    identity = id(value)
    if identity in active_containers:
        raise SensitiveJSONError("cyclic JSON data is not allowed")
    active_containers.add(identity)
    try:
        return [
            _normalize_node(
                item,
                container_depth=depth,
                active_containers=active_containers,
            )
            for item in value
        ]
    finally:
        active_containers.remove(identity)


def _contains_sensitive_string(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (_EMAIL, _JWT, _BEARER, _PRIVATE_KEY, _EXPLICIT_SECRET, _KNOWN_TOKEN)
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SensitiveJSONError("duplicate JSON object keys are not allowed")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise SensitiveJSONError(f"invalid JSON number: {value}")


def _register_sqlite_function(dbapi_connection: sqlite3.Connection) -> None:
    dbapi_connection.create_function(
        SQLITE_VALIDATOR_FUNCTION,
        1,
        sqlite_sensitive_json_object_is_valid,
        deterministic=True,
    )


def _ensure_sqlite_function_on_connection(metadata, connection, **kwargs) -> None:
    if connection.dialect.name != "sqlite":
        return
    dbapi_connection = connection.connection.driver_connection
    if isinstance(dbapi_connection, sqlite3.Connection):
        _register_sqlite_function(dbapi_connection)


@event.listens_for(Engine, "connect")
def _register_sqlite_sensitive_json_function(dbapi_connection, connection_record) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        _register_sqlite_function(dbapi_connection)
