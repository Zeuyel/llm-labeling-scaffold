\getenv app_password SCAFFOLD_POSTGRES_APP_PASSWORD

SELECT current_user = :'owner_user' AND rolsuper AS cluster_admin_ok
FROM pg_roles
WHERE rolname = current_user
\gset

\if :cluster_admin_ok
\else
  \echo 'runtime role initialization requires the dedicated cluster superuser'
  SELECT 1 / 0;
\endif

WITH expected_relations(relation_name, relation_kind) AS (
    VALUES
        ('principals', 'r'),
        ('workspaces', 'r'),
        ('tasks', 'r'),
        ('role_bindings', 'r'),
        ('idempotency_records', 'r'),
        ('workspace_settings', 'r'),
        ('task_drafts', 'r'),
        ('task_revisions', 'r'),
        ('task_revision_materializations', 'r'),
        ('argilla_connection_bindings', 'r'),
        ('argilla_annotator_mappings', 'r'),
        ('annotator_cohorts', 'r'),
        ('annotator_cohort_revisions', 'r'),
        ('annotator_cohort_members', 'r'),
        ('allocation_plans', 'r'),
        ('allocation_plan_states', 'r'),
        ('allocation_workspace_groups', 'r'),
        ('allocation_dataset_groups', 'r'),
        ('allocation_dataset_group_states', 'r'),
        ('allocation_assignment_items', 'r'),
        ('allocation_record_bindings', 'r'),
        ('allocation_assignments', 'r'),
        ('allocation_collection_receipts', 'r'),
        ('annotation_jobs', 'r'),
        ('audit_events', 'r'),
        ('migration_runs', 'r'),
        ('alembic_version', 'r')
), expected_function(function_name, identity_arguments) AS (
    VALUES
        ('lls_allocation_plan_contract', ''),
        ('lls_allocation_plan_create_state', ''),
        ('lls_allocation_plan_state_guard', ''),
        ('lls_argilla_annotator_mapping_freeze', ''),
        ('lls_argilla_connection_binding_freeze', ''),
        ('lls_assignment_contract', ''),
        ('lls_assignment_item_create_binding', ''),
        ('lls_annotation_job_guard', ''),
        ('lls_canonical_sensitive_json_text', 'document json'),
        ('lls_cohort_member_guard', ''),
        ('lls_cohort_revision_guard', ''),
        ('lls_collection_receipt_guard', ''),
        ('lls_complete_idempotency', 'p_record_id uuid, p_workspace_id uuid, p_actor_principal_id uuid, p_caller_principal_id uuid, p_operation text, p_idempotency_key_hash text, p_request_fingerprint text, p_required_permission text, p_resource_type text, p_resource_id uuid, p_channel text, p_response_status integer, p_response_body text, p_succeeded boolean, p_request_id text'),
        ('lls_confirmed_plan_child_guard', ''),
        ('lls_dataset_group_contract', ''),
        ('lls_dataset_group_create_state', ''),
        ('lls_dataset_group_state_guard', ''),
        ('lls_enforce_last_workspace_admin', ''),
        ('lls_enforce_task_lifecycle_transition', ''),
        ('lls_idempotency_completion_gate_is_open', ''),
        ('lls_protect_idempotency_record_delete', ''),
        ('lls_protect_idempotency_record_update', ''),
        ('lls_record_binding_guard', ''),
        ('lls_reject_allocation_receipt_mutation', ''),
        ('lls_reject_allocation_truncate', ''),
        ('lls_reject_audit_event_mutation', ''),
        ('lls_reject_task_revision_mutation', ''),
        ('lls_sensitive_json_node_is_valid', 'document json, current_depth integer'),
        ('lls_sensitive_json_object_is_valid', 'document json'),
        ('lls_sensitive_json_string_is_safe', 'value text'),
        ('lls_validate_allocation_plan_graph', 'target_plan_id uuid'),
        ('lls_validate_audit_event_details', ''),
        ('lls_validate_idempotency_record_insert', ''),
        ('lls_validate_idempotency_response_body', ''),
        ('lls_validate_workspace_setting_value', ''),
        ('lls_workspace_group_contract', '')
), database_owner AS (
    SELECT EXISTS (
        SELECT 1
        FROM pg_database AS target_database
        WHERE target_database.datname = current_database()
          AND pg_get_userbyid(target_database.datdba)::text = :'owner_user'
    ) AS is_expected
), schema_owner AS (
    SELECT EXISTS (
        SELECT 1
        FROM pg_namespace AS namespace
        WHERE namespace.nspname = 'public'
          AND pg_get_userbyid(namespace.nspowner)::text IN (:'owner_user', 'pg_database_owner')
    ) AS is_expected
), sequence_owner AS (
    SELECT NOT EXISTS (
        SELECT 1
        FROM pg_class AS sequence
        JOIN pg_namespace AS namespace ON namespace.oid = sequence.relnamespace
        WHERE namespace.nspname = 'public'
          AND sequence.relname = 'lls_idempotency_completion_gate_seq'
          AND sequence.relkind = 'S'
          AND pg_get_userbyid(sequence.relowner)::text <> :'owner_user'
    ) AS is_expected
)
SELECT database_owner.is_expected
   AND schema_owner.is_expected
   AND sequence_owner.is_expected
   AND NOT EXISTS (
       SELECT 1
       FROM expected_relations
       JOIN pg_class AS relation
         ON relation.relname = expected_relations.relation_name
        AND relation.relkind::text = expected_relations.relation_kind
       JOIN pg_namespace AS namespace
         ON namespace.oid = relation.relnamespace
        AND namespace.nspname = 'public'
       WHERE pg_get_userbyid(relation.relowner)::text <> :'owner_user'
   )
   AND NOT EXISTS (
       SELECT 1
       FROM expected_function
       JOIN pg_proc AS procedure
         ON procedure.proname = expected_function.function_name
        AND pg_get_function_identity_arguments(procedure.oid)
            = expected_function.identity_arguments
       JOIN pg_namespace AS namespace
         ON namespace.oid = procedure.pronamespace
        AND namespace.nspname = 'public'
       WHERE pg_get_userbyid(procedure.proowner)::text <> :'owner_user'
   ) AS ownership_preflight_ok
FROM database_owner
CROSS JOIN schema_owner
CROSS JOIN sequence_owner
\gset

\if :ownership_preflight_ok
\else
  \echo 'runtime ownership preflight failed'
  SELECT 1 / 0;
\endif

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
\gexec

SELECT format('REVOKE %I FROM %I', parent_role.rolname, :'app_user')
FROM pg_auth_members AS membership
JOIN pg_roles AS parent_role ON parent_role.oid = membership.roleid
WHERE membership.member = (SELECT oid FROM pg_roles WHERE rolname = :'app_user')
\gexec

SELECT format('REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC', target_database.datname)
FROM pg_database AS target_database
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM %I',
    target_database.datname,
    :'app_user'
)
FROM pg_database AS target_database
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_user')
\gexec
SELECT format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', namespace.nspname)
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
SELECT format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, :'app_user')
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user')
\gexec

SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM PUBLIC',
    namespace.nspname
)
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM %I',
    namespace.nspname,
    :'app_user'
)
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
WITH relation_columns AS (
    SELECT namespace.nspname,
           relation.relname,
           string_agg(format('%I', attribute.attname), ', ' ORDER BY attribute.attnum) AS columns
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    JOIN pg_attribute AS attribute
      ON attribute.attrelid = relation.oid
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
    GROUP BY namespace.nspname, relation.relname
)
SELECT format(
    'REVOKE %s (%s) ON TABLE %I.%I FROM PUBLIC',
    privilege.privilege_type,
    relation_columns.columns,
    relation_columns.nspname,
    relation_columns.relname
)
FROM relation_columns
CROSS JOIN (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
) AS privilege(privilege_type)
\gexec

WITH relation_columns AS (
    SELECT namespace.nspname,
           relation.relname,
           string_agg(format('%I', attribute.attname), ', ' ORDER BY attribute.attnum) AS columns
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    JOIN pg_attribute AS attribute
      ON attribute.attrelid = relation.oid
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
    GROUP BY namespace.nspname, relation.relname
)
SELECT format(
    'REVOKE %s (%s) ON TABLE %I.%I FROM %I',
    privilege.privilege_type,
    relation_columns.columns,
    relation_columns.nspname,
    relation_columns.relname,
    :'app_user'
)
FROM relation_columns
CROSS JOIN (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
) AS privilege(privilege_type)
\gexec

SELECT format('REVOKE UPDATE, DELETE ON TABLE public.idempotency_records FROM %I', :'app_user')
WHERE to_regclass('public.idempotency_records') IS NOT NULL
\gexec
SELECT format('REVOKE UPDATE, DELETE ON TABLE public.audit_events FROM %I', :'app_user')
WHERE to_regclass('public.audit_events') IS NOT NULL
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC',
    namespace.nspname
)
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM %I',
    namespace.nspname,
    :'app_user'
)
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
SELECT format(
    'REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM PUBLIC',
    namespace.nspname
)
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
SELECT format(
    'REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
    namespace.nspname,
    :'app_user'
)
FROM pg_namespace AS namespace
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
\gexec
SELECT format(
    'REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
    namespace.nspname,
    role.rolname
)
FROM pg_namespace AS namespace
CROSS JOIN pg_roles AS role
WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
  AND role.rolname NOT IN (:'owner_user', :'app_user')
  AND NOT role.rolsuper
\gexec

SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM PUBLIC',
    :'owner_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM %I',
    :'owner_user',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON SEQUENCES FROM PUBLIC',
    :'owner_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON SEQUENCES FROM %I',
    :'owner_user',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON FUNCTIONS FROM PUBLIC',
    :'owner_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON FUNCTIONS FROM %I',
    :'owner_user',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC',
    :'owner_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM %I',
    :'owner_user',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC',
    :'owner_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',
    :'owner_user',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC',
    :'owner_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I',
    :'owner_user',
    :'app_user'
)
\gexec

WITH expected_relations(relation_name, can_select, can_insert, can_update, can_delete) AS (
    VALUES
        ('principals', true, true, false, false),
        ('workspaces', true, true, true, false),
        ('tasks', true, true, true, false),
        ('role_bindings', true, true, true, true),
        ('idempotency_records', true, true, false, false),
        ('workspace_settings', true, true, true, false),
        ('task_drafts', true, true, true, false),
        ('task_revisions', true, true, false, false),
        ('task_revision_materializations', true, true, true, false),
        ('argilla_connection_bindings', true, true, true, false),
        ('argilla_annotator_mappings', true, true, true, false),
        ('annotator_cohorts', true, true, true, false),
        ('annotator_cohort_revisions', true, true, true, false),
        ('annotator_cohort_members', true, true, true, true),
        ('allocation_plans', true, true, true, false),
        ('allocation_plan_states', true, false, true, false),
        ('allocation_workspace_groups', true, true, true, false),
        ('allocation_dataset_groups', true, true, true, false),
        ('allocation_dataset_group_states', true, false, true, false),
        ('allocation_assignment_items', true, true, false, false),
        ('allocation_record_bindings', true, false, true, false),
        ('allocation_assignments', true, true, true, false),
        ('allocation_collection_receipts', true, true, false, false),
        ('annotation_jobs', true, true, true, false),
        ('audit_events', true, true, false, false),
        ('migration_runs', false, false, false, false),
        ('alembic_version', false, false, false, false)
), privileges(privilege_type) AS (
    VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')
)
SELECT format('GRANT %s ON TABLE public.%I TO %I', privileges.privilege_type, expected_relations.relation_name, :'app_user')
FROM expected_relations
CROSS JOIN privileges
WHERE CASE privileges.privilege_type
    WHEN 'SELECT' THEN expected_relations.can_select
    WHEN 'INSERT' THEN expected_relations.can_insert
    WHEN 'UPDATE' THEN expected_relations.can_update
    WHEN 'DELETE' THEN expected_relations.can_delete
END
  AND to_regclass(format('public.%I', expected_relations.relation_name)) IS NOT NULL
\gexec

SELECT format(
    'GRANT UPDATE (display_name, email_snapshot, updated_at) ON TABLE public.principals TO %I',
    :'app_user'
)
WHERE to_regclass('public.principals') IS NOT NULL
\gexec

WITH expected_functions(function_name, argument_types) AS (
    VALUES
        ('lls_canonical_sensitive_json_text', 'json'),
        ('lls_sensitive_json_node_is_valid', 'json, integer'),
        ('lls_sensitive_json_object_is_valid', 'json'),
        ('lls_sensitive_json_string_is_safe', 'text'),
        ('lls_validate_allocation_plan_graph', 'uuid')
)
SELECT format(
    'GRANT EXECUTE ON FUNCTION public.%I(%s) TO %I',
    expected_functions.function_name,
    expected_functions.argument_types,
    :'app_user'
)
FROM expected_functions
WHERE to_regprocedure(format('public.%I(%s)', expected_functions.function_name, expected_functions.argument_types)) IS NOT NULL
\gexec

SELECT format(
    'GRANT EXECUTE ON FUNCTION public.lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, text, uuid, text, integer, text, boolean, text) TO %I',
    :'app_user'
)
WHERE to_regprocedure(
    'public.lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, text, uuid, text, integer, text, boolean, text)'
) IS NOT NULL
\gexec
