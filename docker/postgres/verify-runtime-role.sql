\set ON_ERROR_STOP on

SELECT current_user = :'owner_user' AND rolsuper AS cluster_admin_ok
FROM pg_roles
WHERE rolname = current_user
\gset

SELECT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = :'app_user'
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolreplication
      AND NOT rolbypassrls
) AS role_attributes_ok
\gset

SELECT NOT EXISTS (
    SELECT 1
    FROM pg_auth_members
    WHERE member = (SELECT oid FROM pg_roles WHERE rolname = :'app_user')
) AS role_memberships_ok
\gset

SELECT coalesce(bool_and(
    has_database_privilege(:'app_user', target_database.oid, 'CONNECT')
        = (target_database.datname = current_database())
    AND NOT has_database_privilege(:'app_user', target_database.oid, 'CREATE')
    AND NOT has_database_privilege(:'app_user', target_database.oid, 'TEMP')
), false) AS database_privileges_ok
FROM pg_database AS target_database
\gset

WITH app_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'app_user'
), expected(database_name, grantee, privilege_type, is_grantable) AS (
    SELECT current_database(), app_role.oid, 'CONNECT'::text, false
    FROM app_role
), actual AS (
    SELECT target_database.datname,
           privilege.grantee,
           privilege.privilege_type,
           privilege.is_grantable
    FROM pg_database AS target_database
    CROSS JOIN app_role
    CROSS JOIN LATERAL aclexplode(
        coalesce(target_database.datacl, acldefault('d', target_database.datdba))
    ) AS privilege
    WHERE privilege.grantee IN (0, app_role.oid)
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS database_acl_catalog_ok
\gset

SELECT EXISTS (
    SELECT 1
    FROM pg_database AS target_database
    WHERE target_database.datname = current_database()
      AND pg_get_userbyid(target_database.datdba)::text = :'owner_user'
) AS database_owner_ok
\gset

WITH expected(schema_name) AS (
    VALUES ('public'::name)
), actual AS (
    SELECT namespace.nspname
    FROM pg_namespace AS namespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS user_schema_catalog_ok
\gset

SELECT has_schema_privilege(:'app_user', 'public', 'USAGE')
   AND NOT has_schema_privilege(:'app_user', 'public', 'CREATE') AS schema_privileges_ok
\gset

WITH app_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'app_user'
), expected(schema_name, grantee, privilege_type, is_grantable) AS (
    SELECT 'public'::name, app_role.oid, 'USAGE'::text, false
    FROM app_role
), actual AS (
    SELECT namespace.nspname,
           privilege.grantee,
           privilege.privilege_type,
           privilege.is_grantable
    FROM pg_namespace AS namespace
    CROSS JOIN app_role
    CROSS JOIN LATERAL aclexplode(
        coalesce(namespace.nspacl, acldefault('n', namespace.nspowner))
    ) AS privilege
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND privilege.grantee IN (0, app_role.oid)
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS schema_acl_catalog_ok
\gset

SELECT EXISTS (
    SELECT 1
    FROM pg_namespace AS namespace
    WHERE namespace.nspname = 'public'
      AND pg_get_userbyid(namespace.nspowner)::text IN (:'owner_user', 'pg_database_owner')
) AS schema_owner_ok
\gset

WITH expected(relation_name, relation_kind) AS (
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
        ('audit_events', 'r'),
        ('migration_runs', 'r'),
        ('alembic_version', 'r')
), actual AS (
    SELECT relation.relname, relation.relkind::text
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS relation_catalog_ok
\gset

WITH expected(relation_name, relation_kind) AS (
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
        ('audit_events', 'r'),
        ('migration_runs', 'r'),
        ('alembic_version', 'r')
)
SELECT coalesce(bool_and(
    pg_get_userbyid(relation.relowner)::text = :'owner_user'
), true) AS relation_owner_ok
FROM expected
LEFT JOIN pg_class AS relation
  ON relation.relname = expected.relation_name
 AND relation.relkind::text = expected.relation_kind
LEFT JOIN pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
 AND namespace.nspname = 'public'
WHERE relation.oid IS NOT NULL
  AND namespace.oid IS NOT NULL
\gset

WITH expected(relation_name, relation_kind, can_select, can_insert, can_update, can_delete) AS (
    VALUES
        ('principals', 'r', true, true, false, false),
        ('workspaces', 'r', true, true, true, false),
        ('tasks', 'r', true, true, true, false),
        ('role_bindings', 'r', true, true, true, true),
        ('idempotency_records', 'r', true, true, false, false),
        ('workspace_settings', 'r', true, true, true, false),
        ('task_drafts', 'r', true, true, true, false),
        ('task_revisions', 'r', true, true, false, false),
        ('task_revision_materializations', 'r', true, true, true, false),
        ('argilla_connection_bindings', 'r', true, true, true, false),
        ('argilla_annotator_mappings', 'r', true, true, true, false),
        ('annotator_cohorts', 'r', true, true, true, false),
        ('annotator_cohort_revisions', 'r', true, true, true, false),
        ('annotator_cohort_members', 'r', true, true, true, true),
        ('allocation_plans', 'r', true, true, true, false),
        ('allocation_plan_states', 'r', true, false, true, false),
        ('allocation_workspace_groups', 'r', true, true, true, false),
        ('allocation_dataset_groups', 'r', true, true, true, false),
        ('allocation_dataset_group_states', 'r', true, false, true, false),
        ('allocation_assignment_items', 'r', true, true, false, false),
        ('allocation_record_bindings', 'r', true, false, true, false),
        ('allocation_assignments', 'r', true, true, true, false),
        ('allocation_collection_receipts', 'r', true, true, false, false),
        ('audit_events', 'r', true, true, false, false),
        ('migration_runs', 'r', false, false, false, false),
        ('alembic_version', 'r', false, false, false, false)
)
SELECT coalesce(bool_and(
    has_table_privilege(:'app_user', relation.oid, 'SELECT') = expected.can_select
    AND has_table_privilege(:'app_user', relation.oid, 'INSERT') = expected.can_insert
    AND has_table_privilege(:'app_user', relation.oid, 'UPDATE') = expected.can_update
    AND has_table_privilege(:'app_user', relation.oid, 'DELETE') = expected.can_delete
    AND NOT has_table_privilege(:'app_user', relation.oid, 'TRUNCATE')
    AND NOT has_table_privilege(:'app_user', relation.oid, 'REFERENCES')
    AND NOT has_table_privilege(:'app_user', relation.oid, 'TRIGGER')
), false) AS relation_privileges_ok
FROM expected
JOIN pg_class AS relation
  ON relation.relname = expected.relation_name
 AND relation.relkind::text = expected.relation_kind
JOIN pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
 AND namespace.nspname = 'public'
\gset

WITH app_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'app_user'
), expected_relations(relation_name, can_select, can_insert, can_update, can_delete) AS (
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
        ('audit_events', true, true, false, false),
        ('migration_runs', false, false, false, false),
        ('alembic_version', false, false, false, false)
), expected(schema_name, relation_name, grantee, privilege_type, is_grantable) AS (
    SELECT 'public'::name,
           expected_relations.relation_name,
           app_role.oid,
           grants.privilege_type,
           false
    FROM expected_relations
    CROSS JOIN LATERAL (
        VALUES
            ('SELECT', expected_relations.can_select),
            ('INSERT', expected_relations.can_insert),
            ('UPDATE', expected_relations.can_update),
            ('DELETE', expected_relations.can_delete)
    ) AS grants(privilege_type, is_allowed)
    CROSS JOIN app_role
    WHERE grants.is_allowed
), actual AS (
    SELECT namespace.nspname,
           relation.relname,
           privilege.grantee,
           privilege.privilege_type,
           privilege.is_grantable
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN app_role
    CROSS JOIN LATERAL aclexplode(
        coalesce(relation.relacl, acldefault('r', relation.relowner))
    ) AS privilege
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND privilege.grantee IN (0, app_role.oid)
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS relation_acl_catalog_ok
\gset

WITH expected_relations(table_name, can_select, can_insert, can_update) AS (
    VALUES
        ('principals', true, true, false),
        ('workspaces', true, true, true),
        ('tasks', true, true, true),
        ('role_bindings', true, true, true),
        ('idempotency_records', true, true, false),
        ('workspace_settings', true, true, true),
        ('task_drafts', true, true, true),
        ('task_revisions', true, true, false),
        ('task_revision_materializations', true, true, true),
        ('argilla_connection_bindings', true, true, true),
        ('argilla_annotator_mappings', true, true, true),
        ('annotator_cohorts', true, true, true),
        ('annotator_cohort_revisions', true, true, true),
        ('annotator_cohort_members', true, true, true),
        ('allocation_plans', true, true, true),
        ('allocation_plan_states', true, false, true),
        ('allocation_workspace_groups', true, true, true),
        ('allocation_dataset_groups', true, true, true),
        ('allocation_dataset_group_states', true, false, true),
        ('allocation_assignment_items', true, true, false),
        ('allocation_record_bindings', true, false, true),
        ('allocation_assignments', true, true, true),
        ('allocation_collection_receipts', true, true, false),
        ('audit_events', true, true, false),
        ('migration_runs', false, false, false),
        ('alembic_version', false, false, false)
), allowed_updates(table_name, column_name) AS (
    VALUES
        ('principals', 'display_name'),
        ('principals', 'email_snapshot'),
        ('principals', 'updated_at')
)
SELECT coalesce(bool_and(
    has_column_privilege(:'app_user', relation.oid, attribute.attnum, 'SELECT')
        = expected_relations.can_select
    AND has_column_privilege(:'app_user', relation.oid, attribute.attnum, 'INSERT')
        = expected_relations.can_insert
    AND has_column_privilege(:'app_user', relation.oid, attribute.attnum, 'UPDATE')
        = (expected_relations.can_update OR allowed_updates.column_name IS NOT NULL)
    AND NOT has_column_privilege(:'app_user', relation.oid, attribute.attnum, 'REFERENCES')
), false) AS column_privileges_ok
FROM expected_relations
JOIN pg_class AS relation
  ON relation.relname = expected_relations.table_name
 AND relation.relkind = 'r'
JOIN pg_namespace AS namespace
  ON namespace.oid = relation.relnamespace
 AND namespace.nspname = 'public'
JOIN pg_attribute AS attribute
  ON attribute.attrelid = relation.oid
 AND attribute.attnum > 0
 AND NOT attribute.attisdropped
LEFT JOIN allowed_updates
  ON allowed_updates.table_name = expected_relations.table_name
 AND allowed_updates.column_name = attribute.attname
\gset

WITH app_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'app_user'
), expected(schema_name, table_name, column_name, grantee, privilege_type, is_grantable) AS (
    SELECT 'public'::name,
           allowed_updates.table_name,
           allowed_updates.column_name,
           app_role.oid,
           'UPDATE'::text,
           false
    FROM (
        VALUES
            ('principals', 'display_name'),
            ('principals', 'email_snapshot'),
            ('principals', 'updated_at')
    ) AS allowed_updates(table_name, column_name)
    CROSS JOIN app_role
), actual AS (
    SELECT namespace.nspname,
           relation.relname,
           attribute.attname,
           privilege.grantee,
           privilege.privilege_type,
           privilege.is_grantable
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    JOIN pg_attribute AS attribute
      ON attribute.attrelid = relation.oid
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    CROSS JOIN app_role
    CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND privilege.grantee IN (0, app_role.oid)
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS column_acl_catalog_ok
\gset

WITH expected(sequence_name) AS (
    VALUES ('lls_idempotency_completion_gate_seq')
), actual AS (
    SELECT relation.relname
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relkind = 'S'
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS sequence_catalog_ok
\gset

SELECT EXISTS (
    SELECT 1
    FROM pg_class AS sequence
    JOIN pg_namespace AS namespace ON namespace.oid = sequence.relnamespace
    WHERE namespace.nspname = 'public'
      AND sequence.relname = 'lls_idempotency_completion_gate_seq'
      AND sequence.relkind = 'S'
      AND pg_get_userbyid(sequence.relowner)::text = :'owner_user'
) AS sequence_owner_ok
\gset

WITH app_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'app_user'
)
SELECT NOT EXISTS (
    SELECT 1
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    CROSS JOIN app_role
    CROSS JOIN LATERAL aclexplode(
        coalesce(relation.relacl, acldefault('S', relation.relowner))
    ) AS privilege
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND relation.relkind = 'S'
      AND privilege.grantee IN (0, app_role.oid)
) AS sequence_acl_catalog_ok
\gset

WITH expected(function_name, identity_arguments) AS (
    VALUES
        ('lls_allocation_plan_contract', ''),
        ('lls_allocation_plan_create_state', ''),
        ('lls_allocation_plan_state_guard', ''),
        ('lls_argilla_annotator_mapping_freeze', ''),
        ('lls_argilla_connection_binding_freeze', ''),
        ('lls_assignment_contract', ''),
        ('lls_assignment_item_create_binding', ''),
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
), actual AS (
    SELECT procedure.proname, pg_get_function_identity_arguments(procedure.oid)
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS function_catalog_ok
\gset

WITH expected(function_name, identity_arguments) AS (
    VALUES
        ('lls_allocation_plan_contract', ''),
        ('lls_allocation_plan_create_state', ''),
        ('lls_allocation_plan_state_guard', ''),
        ('lls_argilla_annotator_mapping_freeze', ''),
        ('lls_argilla_connection_binding_freeze', ''),
        ('lls_assignment_contract', ''),
        ('lls_assignment_item_create_binding', ''),
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
)
SELECT coalesce(bool_and(
    pg_get_userbyid(procedure.proowner)::text = :'owner_user'
), false) AS function_owner_ok
FROM expected
JOIN pg_proc AS procedure
  ON procedure.proname = expected.function_name
 AND pg_get_function_identity_arguments(procedure.oid) = expected.identity_arguments
JOIN pg_namespace AS namespace
  ON namespace.oid = procedure.pronamespace
 AND namespace.nspname = 'public'
\gset

SELECT EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    JOIN pg_language AS language ON language.oid = procedure.prolang
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_reject_audit_event_mutation'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
      AND procedure.prokind = 'f'
      AND pg_get_function_result(procedure.oid) = 'trigger'
      AND language.lanname = 'plpgsql'
      AND NOT procedure.prosecdef
      AND procedure.proconfig IS NULL
      AND btrim(regexp_replace(procedure.prosrc, '[[:space:]]+', ' ', 'g'))
          = 'BEGIN RAISE EXCEPTION ''audit_events is append-only'' USING ERRCODE = ''55000''; END;'
) AS audit_function_guard_ok
\gset

SELECT EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    JOIN pg_language AS language ON language.oid = procedure.prolang
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_complete_idempotency'
      AND pg_get_function_identity_arguments(procedure.oid)
          = 'p_record_id uuid, p_workspace_id uuid, p_actor_principal_id uuid, p_caller_principal_id uuid, p_operation text, p_idempotency_key_hash text, p_request_fingerprint text, p_required_permission text, p_resource_type text, p_resource_id uuid, p_channel text, p_response_status integer, p_response_body text, p_succeeded boolean, p_request_id text'
      AND procedure.prokind = 'f'
      AND pg_get_function_result(procedure.oid)
          = 'TABLE(outcome text, state text, response_status integer, response_body json, reason text)'
      AND language.lanname = 'plpgsql'
      AND procedure.prosecdef
      AND procedure.proconfig = ARRAY['search_path=pg_catalog, public']
) AS completion_function_guard_ok
\gset

WITH app_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'app_user'
), expected(schema_name, function_name, identity_arguments, grantee, privilege_type, is_grantable) AS (
    SELECT 'public'::name,
           allowed_functions.function_name,
           allowed_functions.identity_arguments,
           app_role.oid,
           'EXECUTE'::text,
           false
    FROM app_role
    CROSS JOIN (
        VALUES
            ('lls_canonical_sensitive_json_text'::name, 'document json'::text),
            ('lls_complete_idempotency'::name, 'p_record_id uuid, p_workspace_id uuid, p_actor_principal_id uuid, p_caller_principal_id uuid, p_operation text, p_idempotency_key_hash text, p_request_fingerprint text, p_required_permission text, p_resource_type text, p_resource_id uuid, p_channel text, p_response_status integer, p_response_body text, p_succeeded boolean, p_request_id text'::text),
            ('lls_sensitive_json_node_is_valid'::name, 'document json, current_depth integer'::text),
            ('lls_sensitive_json_object_is_valid'::name, 'document json'::text),
            ('lls_sensitive_json_string_is_safe'::name, 'value text'::text),
            ('lls_validate_allocation_plan_graph'::name, 'target_plan_id uuid'::text)
    ) AS allowed_functions(function_name, identity_arguments)
), actual AS (
    SELECT namespace.nspname,
           procedure.proname,
           pg_get_function_identity_arguments(procedure.oid),
           privilege.grantee,
           privilege.privilege_type,
           privilege.is_grantable
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    CROSS JOIN app_role
    CROSS JOIN LATERAL aclexplode(
        coalesce(procedure.proacl, acldefault('f', procedure.proowner))
    ) AS privilege
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND privilege.grantee <> procedure.proowner
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS function_acl_catalog_ok
\gset

WITH owner_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'owner_user'
), public_namespace AS (
    SELECT oid FROM pg_namespace WHERE nspname = 'public'
), object_types(object_type) AS (
    VALUES ('r'::"char"), ('S'::"char"), ('f'::"char")
), global_defaults AS (
    SELECT 'global'::text AS scope_name,
           object_types.object_type,
           owner_role.oid AS owner_oid,
           coalesce(default_acl.defaclacl, acldefault(object_types.object_type, owner_role.oid)) AS acl
    FROM owner_role
    CROSS JOIN object_types
    LEFT JOIN pg_default_acl AS default_acl
      ON default_acl.defaclrole = owner_role.oid
     AND default_acl.defaclnamespace = 0
     AND default_acl.defaclobjtype = object_types.object_type
), schema_defaults AS (
    SELECT 'public'::text AS scope_name,
           object_types.object_type,
           owner_role.oid AS owner_oid,
           default_acl.defaclacl AS acl
    FROM owner_role
    CROSS JOIN public_namespace
    CROSS JOIN object_types
    LEFT JOIN pg_default_acl AS default_acl
      ON default_acl.defaclrole = owner_role.oid
     AND default_acl.defaclnamespace = public_namespace.oid
     AND default_acl.defaclobjtype = object_types.object_type
), actual AS (
    SELECT defaults.scope_name,
           defaults.object_type,
           privilege.grantee,
           privilege.privilege_type,
           privilege.is_grantable
    FROM (
        SELECT * FROM global_defaults
        UNION ALL
        SELECT * FROM schema_defaults
    ) AS defaults
    CROSS JOIN LATERAL aclexplode(defaults.acl) AS privilege
    WHERE privilege.grantee <> defaults.owner_oid
)
SELECT NOT EXISTS (SELECT 1 FROM actual) AS default_acl_catalog_ok
\gset

WITH audit_table AS (
    SELECT relation.oid
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relname = 'audit_events'
      AND relation.relkind = 'r'
), audit_function AS (
    SELECT procedure.oid
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_reject_audit_event_mutation'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
), audit_sensitive_function AS (
    SELECT procedure.oid
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_validate_audit_event_details'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
), expected(trigger_name, trigger_enabled, trigger_type, function_oid) AS (
    VALUES
        (
            'trg_audit_events_append_only',
            'O'::text,
            27,
            (SELECT oid FROM audit_function)
        ),
        (
            'trg_audit_events_append_only_truncate',
            'O'::text,
            34,
            (SELECT oid FROM audit_function)
        ),
        (
            'trg_audit_events_sensitive_json',
            'O'::text,
            23,
            (SELECT oid FROM audit_sensitive_function)
        )
), actual AS (
    SELECT trigger.tgname::text,
           trigger.tgenabled::text,
           trigger.tgtype::integer,
           trigger.tgfoid
    FROM pg_trigger AS trigger
    JOIN audit_table ON audit_table.oid = trigger.tgrelid
    WHERE NOT trigger.tgisinternal
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS database_guards_ok
\gset

WITH idempotency_table AS (
    SELECT relation.oid
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relname = 'idempotency_records'
      AND relation.relkind = 'r'
), insert_function AS (
    SELECT procedure.oid
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_validate_idempotency_record_insert'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
), update_function AS (
    SELECT procedure.oid
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_protect_idempotency_record_update'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
), delete_function AS (
    SELECT procedure.oid
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_protect_idempotency_record_delete'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
), sensitive_function AS (
    SELECT procedure.oid
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_validate_idempotency_response_body'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
), expected(trigger_name, trigger_enabled, trigger_type, function_oid) AS (
    VALUES
        ('trg_idempotency_records_controlled_delete', 'O'::text, 11, (SELECT oid FROM delete_function)),
        ('trg_idempotency_records_controlled_insert', 'O'::text, 7, (SELECT oid FROM insert_function)),
        ('trg_idempotency_records_controlled_update', 'O'::text, 19, (SELECT oid FROM update_function)),
        ('trg_idempotency_records_sensitive_json', 'O'::text, 23, (SELECT oid FROM sensitive_function))
), actual AS (
    SELECT trigger.tgname::text,
           trigger.tgenabled::text,
           trigger.tgtype::integer,
           trigger.tgfoid
    FROM pg_trigger AS trigger
    JOIN idempotency_table ON idempotency_table.oid = trigger.tgrelid
    WHERE NOT trigger.tgisinternal
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS idempotency_guards_ok
\gset

\if :cluster_admin_ok
\else
  \echo 'runtime cluster administrator verification failed'
  SELECT 1 / 0;
\endif
\if :database_owner_ok
\else
  \echo 'runtime database ownership verification failed'
  SELECT 1 / 0;
\endif
\if :schema_owner_ok
\else
  \echo 'runtime schema ownership verification failed'
  SELECT 1 / 0;
\endif
\if :role_attributes_ok
\else
  \echo 'runtime role attribute verification failed'
  SELECT 1 / 0;
\endif
\if :role_memberships_ok
\else
  \echo 'runtime role membership verification failed'
  SELECT 1 / 0;
\endif
\if :database_privileges_ok
\else
  \echo 'runtime database privilege verification failed'
  SELECT 1 / 0;
\endif
\if :database_acl_catalog_ok
\else
  \echo 'runtime database ACL catalog verification failed'
  SELECT 1 / 0;
\endif
\if :schema_privileges_ok
\else
  \echo 'runtime schema privilege verification failed'
  SELECT 1 / 0;
\endif
\if :schema_acl_catalog_ok
\else
  \echo 'runtime schema ACL catalog verification failed'
  SELECT 1 / 0;
\endif
\if :relation_owner_ok
\else
  \echo 'runtime relation ownership verification failed'
  SELECT 1 / 0;
\endif
\if :relation_catalog_ok
\else
  \echo 'runtime relation catalog verification failed'
  SELECT 1 / 0;
\endif
\if :relation_privileges_ok
\else
  \echo 'runtime relation privilege verification failed'
  SELECT 1 / 0;
\endif
\if :relation_acl_catalog_ok
\else
  \echo 'runtime relation ACL catalog verification failed'
  SELECT 1 / 0;
\endif
\if :column_privileges_ok
\else
  \echo 'runtime column privilege verification failed'
  SELECT 1 / 0;
\endif
\if :column_acl_catalog_ok
\else
  \echo 'runtime column ACL catalog verification failed'
  SELECT 1 / 0;
\endif
\if :sequence_catalog_ok
\else
  \echo 'runtime sequence catalog verification failed'
  SELECT 1 / 0;
\endif
\if :sequence_owner_ok
\else
  \echo 'runtime sequence ownership verification failed'
  SELECT 1 / 0;
\endif
\if :sequence_acl_catalog_ok
\else
  \echo 'runtime sequence ACL catalog verification failed'
  SELECT 1 / 0;
\endif
\if :function_owner_ok
\else
  \echo 'runtime function ownership verification failed'
  SELECT 1 / 0;
\endif
\if :function_catalog_ok
\else
  \echo 'runtime function catalog verification failed'
  SELECT 1 / 0;
\endif
\if :audit_function_guard_ok
\else
  \echo 'runtime audit function guard verification failed'
  SELECT 1 / 0;
\endif
\if :completion_function_guard_ok
\else
  \echo 'runtime completion function guard verification failed'
  SELECT 1 / 0;
\endif
\if :function_acl_catalog_ok
\else
  \echo 'runtime function ACL catalog verification failed'
  SELECT 1 / 0;
\endif
\if :user_schema_catalog_ok
\else
  \echo 'runtime user schema catalog verification failed'
  SELECT 1 / 0;
\endif
\if :default_acl_catalog_ok
\else
  \echo 'runtime default ACL catalog verification failed'
  SELECT 1 / 0;
\endif
\if :database_guards_ok
\else
  \echo 'runtime database guard verification failed'
  SELECT 1 / 0;
\endif
\if :idempotency_guards_ok
\else
  \echo 'runtime idempotency guard verification failed'
  SELECT 1 / 0;
\endif

\echo 'runtime role verification passed'
