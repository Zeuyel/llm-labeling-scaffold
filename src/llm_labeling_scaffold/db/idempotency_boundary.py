from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

from sqlalchemy import DDL, Engine, event
from sqlalchemy.orm import Session


POSTGRES_IDEMPOTENCY_CREATE_FUNCTIONS = (
    """
    CREATE SEQUENCE IF NOT EXISTS lls_idempotency_completion_gate_seq
    """,
    """
    CREATE OR REPLACE FUNCTION lls_idempotency_completion_gate_is_open()
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        RETURN current_setting('lls.idempotency_completion_gate', true)
            = currval('public.lls_idempotency_completion_gate_seq')::text;
    EXCEPTION WHEN object_not_in_prerequisite_state THEN
        RETURN FALSE;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_protect_idempotency_record_update()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        IF NOT lls_idempotency_completion_gate_is_open() THEN
            RAISE EXCEPTION 'idempotency records require controlled completion'
                USING ERRCODE = '42501';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_protect_idempotency_record_delete()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        RAISE EXCEPTION 'idempotency records are not directly deletable'
            USING ERRCODE = '42501';
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_validate_idempotency_record_insert()
    RETURNS trigger
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    BEGIN
        IF NEW.state::text <> 'pending'
           OR NEW.response_status IS NOT NULL
           OR NEW.response_body IS NOT NULL
           OR NEW.required_permission IS NULL
           OR NEW.resource_type IS NULL
           OR NEW.resource_id IS NULL
           OR NEW.channel IS NULL
        THEN
            RAISE EXCEPTION 'idempotency records must be inserted as authorized pending claims'
                USING ERRCODE = '42501';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION lls_complete_idempotency(
        p_record_id uuid,
        p_workspace_id uuid,
        p_actor_principal_id uuid,
        p_caller_principal_id uuid,
        p_operation text,
        p_idempotency_key_hash text,
        p_request_fingerprint text,
        p_required_permission text,
        p_resource_type text,
        p_resource_id uuid,
        p_channel text,
        p_response_status integer,
        p_response_body text,
        p_succeeded boolean,
        p_request_id text
    )
    RETURNS TABLE (
        outcome text,
        state text,
        response_status integer,
        response_body json,
        reason text
    )
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $$
    DECLARE
        record_row idempotency_records%ROWTYPE;
        workspace_row workspaces%ROWTYPE;
        actor_row principals%ROWTYPE;
        caller_row principals%ROWTYPE;
        task_row tasks%ROWTYPE;
        response_document json;
        target_state text;
        gate_token text;
        has_resource_binding boolean;
        has_permission boolean;
    BEGIN
        SELECT * INTO record_row
        FROM idempotency_records
        WHERE id = p_record_id;
        IF NOT FOUND
           OR record_row.workspace_id <> p_workspace_id
           OR record_row.actor_principal_id <> p_actor_principal_id
           OR record_row.caller_principal_id <> p_caller_principal_id
           OR record_row.operation <> p_operation
           OR record_row.idempotency_key_hash <> p_idempotency_key_hash
           OR record_row.request_fingerprint <> p_request_fingerprint
           OR record_row.required_permission <> p_required_permission
           OR record_row.resource_type <> p_resource_type
           OR record_row.resource_id <> p_resource_id
           OR record_row.channel::text <> p_channel
        THEN
            RETURN QUERY SELECT 'conflict'::text, NULL::text, NULL::integer, NULL::json, NULL::text;
            RETURN;
        END IF;

        IF p_response_body IS NOT NULL THEN
            BEGIN
                response_document := p_response_body::json;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'invalid idempotency response_body'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_idempotency_records_response_body_sensitive_json';
            END;
            IF NOT lls_sensitive_json_object_is_valid(response_document) THEN
                RAISE EXCEPTION 'invalid idempotency response_body'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_idempotency_records_response_body_sensitive_json';
            END IF;
        END IF;

        SELECT * INTO workspace_row
        FROM workspaces
        WHERE id = record_row.workspace_id
        FOR UPDATE;
        IF NOT FOUND THEN
            RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                'resource_not_visible'::text;
            RETURN;
        END IF;

        PERFORM 1
        FROM principals
        WHERE id IN (record_row.actor_principal_id, record_row.caller_principal_id)
        ORDER BY id
        FOR UPDATE;
        SELECT * INTO actor_row FROM principals WHERE id = record_row.actor_principal_id;
        SELECT * INTO caller_row FROM principals WHERE id = record_row.caller_principal_id;

        PERFORM 1
        FROM role_bindings
        WHERE workspace_id = record_row.workspace_id
          AND principal_id IN (record_row.actor_principal_id, record_row.caller_principal_id)
          AND (
              task_id IS NULL
              OR (
                  record_row.resource_type = 'task'
                  AND task_id = record_row.resource_id
              )
          )
        ORDER BY id
        FOR UPDATE;

        IF record_row.resource_type = 'task' THEN
            SELECT * INTO task_row
            FROM tasks
            WHERE id = record_row.resource_id
              AND workspace_id = record_row.workspace_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                    'resource_not_visible'::text;
                RETURN;
            END IF;
        END IF;

        SELECT * INTO record_row
        FROM idempotency_records
        WHERE id = p_record_id
        FOR UPDATE;
        IF NOT FOUND
           OR record_row.workspace_id <> p_workspace_id
           OR record_row.actor_principal_id <> p_actor_principal_id
           OR record_row.caller_principal_id <> p_caller_principal_id
           OR record_row.operation <> p_operation
           OR record_row.idempotency_key_hash <> p_idempotency_key_hash
           OR record_row.request_fingerprint <> p_request_fingerprint
           OR record_row.required_permission <> p_required_permission
           OR record_row.resource_type <> p_resource_type
           OR record_row.resource_id <> p_resource_id
           OR record_row.channel::text <> p_channel
        THEN
            RETURN QUERY SELECT 'conflict'::text, NULL::text, NULL::integer, NULL::json, NULL::text;
            RETURN;
        END IF;

        IF actor_row.id IS NULL OR NOT actor_row.is_active THEN
            RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                'inactive_principal'::text;
            RETURN;
        END IF;
        IF caller_row.id IS NULL OR NOT caller_row.is_active
           OR (
               caller_row.id <> actor_row.id
               AND caller_row.principal_type::text <> 'service'
           )
        THEN
            RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                'invalid_audit_context'::text;
            RETURN;
        END IF;
        IF NOT workspace_row.is_active THEN
            RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                'inactive_workspace'::text;
            RETURN;
        END IF;
        IF record_row.channel::text = 'system' THEN
            RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                'invalid_audit_context'::text;
            RETURN;
        END IF;

        IF record_row.resource_type = 'workspace' THEN
            IF record_row.resource_id <> record_row.workspace_id
               OR record_row.required_permission NOT IN ('task:create', 'audit:view', 'workspace:manage')
            THEN
                RETURN QUERY SELECT 'conflict'::text, NULL::text, NULL::integer, NULL::json, NULL::text;
                RETURN;
            END IF;
            SELECT EXISTS (
                SELECT 1 FROM role_bindings
                WHERE workspace_id = record_row.workspace_id
                  AND principal_id = record_row.actor_principal_id
                  AND task_id IS NULL
            ) INTO has_resource_binding;
            SELECT EXISTS (
                SELECT 1
                FROM role_bindings
                WHERE workspace_id = record_row.workspace_id
                  AND principal_id = record_row.actor_principal_id
                  AND task_id IS NULL
                  AND CASE record_row.required_permission
                      WHEN 'task:create' THEN role::text IN ('experimenter', 'admin')
                      WHEN 'audit:view' THEN role::text IN ('experimenter', 'admin')
                      WHEN 'workspace:manage' THEN role::text = 'admin'
                      ELSE FALSE
                  END
            ) INTO has_permission;
        ELSIF record_row.resource_type = 'task' THEN
            IF record_row.required_permission NOT IN (
                'task:read', 'task:edit', 'task:publish',
                'import:submit', 'annotation:work', 'annotation:review'
            ) THEN
                RETURN QUERY SELECT 'conflict'::text, NULL::text, NULL::integer, NULL::json, NULL::text;
                RETURN;
            END IF;
            SELECT EXISTS (
                SELECT 1
                FROM role_bindings
                WHERE workspace_id = record_row.workspace_id
                  AND principal_id = record_row.actor_principal_id
                  AND (task_id IS NULL OR task_id = record_row.resource_id)
            ) INTO has_resource_binding;
            SELECT EXISTS (
                SELECT 1
                FROM role_bindings
                WHERE workspace_id = record_row.workspace_id
                  AND principal_id = record_row.actor_principal_id
                  AND (task_id IS NULL OR task_id = record_row.resource_id)
                  AND CASE record_row.required_permission
                      WHEN 'task:read' THEN role::text IN ('viewer', 'annotator', 'experimenter', 'admin')
                      WHEN 'task:edit' THEN role::text IN ('experimenter', 'admin')
                      WHEN 'task:publish' THEN role::text IN ('experimenter', 'admin')
                      WHEN 'import:submit' THEN role::text IN ('experimenter', 'admin')
                      WHEN 'annotation:work' THEN role::text IN ('annotator', 'experimenter', 'admin')
                      WHEN 'annotation:review' THEN role::text IN ('experimenter', 'admin')
                      ELSE FALSE
                  END
            ) INTO has_permission;
        ELSE
            RETURN QUERY SELECT 'conflict'::text, NULL::text, NULL::integer, NULL::json, NULL::text;
            RETURN;
        END IF;

        IF NOT has_resource_binding THEN
            RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                'resource_not_visible'::text;
            RETURN;
        END IF;
        IF NOT has_permission THEN
            RETURN QUERY SELECT 'denied'::text, NULL::text, NULL::integer, NULL::json,
                'role_denied'::text;
            RETURN;
        END IF;

        target_state := CASE WHEN p_succeeded THEN 'succeeded' ELSE 'failed' END;
        IF record_row.state::text <> 'pending' THEN
            IF record_row.state::text <> target_state
               OR record_row.response_status IS DISTINCT FROM p_response_status
               OR (
                   CASE WHEN record_row.response_body IS NULL THEN 'null'::text
                        ELSE lls_canonical_sensitive_json_text(record_row.response_body)
                   END
                   IS DISTINCT FROM
                   CASE WHEN response_document IS NULL THEN 'null'::text
                        ELSE lls_canonical_sensitive_json_text(response_document)
                   END
               )
            THEN
                RETURN QUERY SELECT 'conflict'::text, NULL::text, NULL::integer, NULL::json, NULL::text;
                RETURN;
            END IF;
            RETURN QUERY SELECT 'replay'::text, record_row.state::text,
                record_row.response_status, record_row.response_body, NULL::text;
            RETURN;
        END IF;

        gate_token := nextval('public.lls_idempotency_completion_gate_seq')::text;
        PERFORM set_config('lls.idempotency_completion_gate', gate_token, true);
        UPDATE idempotency_records
        SET state = target_state::idempotency_state,
            response_status = p_response_status,
            response_body = response_document,
            updated_at = now()
        WHERE id = record_row.id;
        INSERT INTO audit_events (
            id,
            workspace_id,
            actor_type,
            actor_principal_id,
            caller_principal_id,
            channel,
            actor_issuer,
            actor_subject,
            actor_display_name,
            event_type,
            resource_type,
            resource_id,
            request_id,
            details
        ) VALUES (
            md5(random()::text || clock_timestamp()::text || record_row.id::text)::uuid,
            record_row.workspace_id,
            'principal'::audit_actor_type,
            actor_row.id,
            caller_row.id,
            record_row.channel,
            actor_row.issuer,
            actor_row.subject,
            actor_row.display_name,
            'idempotency.completed',
            record_row.resource_type,
            record_row.resource_id::text,
            p_request_id,
            json_build_object(
                'idempotency_record_id', record_row.id::text,
                'operation', record_row.operation,
                'response_status', p_response_status,
                'state', target_state
            )
        );
        PERFORM set_config('lls.idempotency_completion_gate', '', true);
        RETURN QUERY SELECT 'completed'::text, target_state, p_response_status,
            response_document, NULL::text;
    END;
    $$
    """,
    """
    REVOKE EXECUTE ON FUNCTION lls_complete_idempotency(
        uuid, uuid, uuid, uuid, text, text, text, text, text, uuid, text, integer, text, boolean, text
    ) FROM PUBLIC;
    REVOKE EXECUTE ON FUNCTION lls_idempotency_completion_gate_is_open() FROM PUBLIC;
    REVOKE EXECUTE ON FUNCTION lls_protect_idempotency_record_update() FROM PUBLIC;
    REVOKE EXECUTE ON FUNCTION lls_protect_idempotency_record_delete() FROM PUBLIC;
    REVOKE EXECUTE ON FUNCTION lls_validate_idempotency_record_insert() FROM PUBLIC;
    REVOKE ALL ON SEQUENCE lls_idempotency_completion_gate_seq FROM PUBLIC;
    """,
)

POSTGRES_IDEMPOTENCY_CREATE_TRIGGERS = (
    """
    CREATE TRIGGER trg_idempotency_records_controlled_insert
    BEFORE INSERT ON idempotency_records
    FOR EACH ROW EXECUTE FUNCTION lls_validate_idempotency_record_insert()
    """,
    """
    CREATE TRIGGER trg_idempotency_records_controlled_update
    BEFORE UPDATE ON idempotency_records
    FOR EACH ROW EXECUTE FUNCTION lls_protect_idempotency_record_update()
    """,
    """
    CREATE TRIGGER trg_idempotency_records_controlled_delete
    BEFORE DELETE ON idempotency_records
    FOR EACH ROW EXECUTE FUNCTION lls_protect_idempotency_record_delete()
    """,
)

POSTGRES_IDEMPOTENCY_DROP_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_idempotency_records_controlled_delete ON idempotency_records",
    "DROP TRIGGER IF EXISTS trg_idempotency_records_controlled_update ON idempotency_records",
    "DROP TRIGGER IF EXISTS trg_idempotency_records_controlled_insert ON idempotency_records",
)

POSTGRES_IDEMPOTENCY_DROP_FUNCTIONS = (
    "DROP FUNCTION IF EXISTS lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, text, uuid, text, integer, text, boolean, text)",
    "DROP FUNCTION IF EXISTS lls_validate_idempotency_record_insert()",
    "DROP FUNCTION IF EXISTS lls_protect_idempotency_record_delete()",
    "DROP FUNCTION IF EXISTS lls_protect_idempotency_record_update()",
    "DROP FUNCTION IF EXISTS lls_idempotency_completion_gate_is_open()",
    "DROP SEQUENCE IF EXISTS lls_idempotency_completion_gate_seq",
)

SQLITE_IDEMPOTENCY_CREATE_TRIGGERS = (
    """
    CREATE TRIGGER trg_idempotency_records_controlled_insert
    BEFORE INSERT ON idempotency_records
    WHEN NEW.state <> 'pending'
      OR NEW.response_status IS NOT NULL
      OR NEW.response_body IS NOT NULL
      OR NEW.required_permission IS NULL
      OR NEW.resource_type IS NULL
      OR NEW.resource_id IS NULL
      OR NEW.channel IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'idempotency records must be inserted as authorized pending claims');
    END
    """,
    """
    CREATE TRIGGER trg_idempotency_records_controlled_update
    BEFORE UPDATE ON idempotency_records
    WHEN lls_idempotency_completion_gate_open() <> 1
    BEGIN
        SELECT RAISE(ABORT, 'idempotency records require controlled completion');
    END
    """,
    """
    CREATE TRIGGER trg_idempotency_records_controlled_delete
    BEFORE DELETE ON idempotency_records
    BEGIN
        SELECT RAISE(ABORT, 'idempotency records are not directly deletable');
    END
    """,
)

SQLITE_IDEMPOTENCY_DROP_TRIGGERS = (
    "DROP TRIGGER IF EXISTS trg_idempotency_records_controlled_delete",
    "DROP TRIGGER IF EXISTS trg_idempotency_records_controlled_update",
    "DROP TRIGGER IF EXISTS trg_idempotency_records_controlled_insert",
)


def install_idempotency_boundary_events(metadata, table) -> None:
    event.listen(metadata, "before_create", _create_postgres_idempotency_pre_boundary)
    event.listen(
        metadata,
        "after_create",
        _create_postgres_idempotency_boundary,
    )
    for statement in POSTGRES_IDEMPOTENCY_DROP_FUNCTIONS:
        event.listen(metadata, "after_drop", DDL(statement).execute_if(dialect="postgresql"))
    for statement in SQLITE_IDEMPOTENCY_CREATE_TRIGGERS:
        event.listen(table, "after_create", DDL(statement).execute_if(dialect="sqlite"))


def _create_postgres_idempotency_boundary(metadata, connection, **kwargs) -> None:
    if connection.dialect.name != "postgresql":
        return
    for statement in POSTGRES_IDEMPOTENCY_CREATE_FUNCTIONS[5:]:
        connection.exec_driver_sql(statement.replace("%", "%%"))
    for statement in POSTGRES_IDEMPOTENCY_CREATE_TRIGGERS:
        connection.exec_driver_sql(statement)


def _create_postgres_idempotency_pre_boundary(metadata, connection, **kwargs) -> None:
    if connection.dialect.name != "postgresql":
        return
    for statement in POSTGRES_IDEMPOTENCY_CREATE_FUNCTIONS[:5]:
        connection.exec_driver_sql(statement.replace("%", "%%"))


@event.listens_for(Engine, "connect")
def _register_sqlite_idempotency_gate(dbapi_connection, connection_record) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    state = {"open": False}
    connection_record.info["lls_idempotency_completion_gate"] = state
    dbapi_connection.create_function(
        "lls_idempotency_completion_gate_open",
        0,
        lambda: int(state["open"]),
        deterministic=True,
    )


@contextmanager
def sqlite_idempotency_completion_gate(session: Session) -> Iterator[None]:
    if session.get_bind().dialect.name != "sqlite":
        yield
        return
    state = session.connection().info.get("lls_idempotency_completion_gate")
    if state is None:
        raise RuntimeError("SQLite idempotency completion gate is not installed")
    previous = state["open"]
    state["open"] = True
    try:
        yield
    finally:
        state["open"] = previous
