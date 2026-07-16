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

WITH expected(relation_name, relation_kind, can_select, can_insert) AS (
    VALUES
        ('principals', 'r', true, true),
        ('workspaces', 'r', true, false),
        ('tasks', 'r', true, false),
        ('role_bindings', 'r', true, false),
        ('idempotency_records', 'r', true, true),
        ('workspace_settings', 'r', true, true),
        ('audit_events', 'r', true, true),
        ('migration_runs', 'r', false, false),
        ('alembic_version', 'r', false, false)
)
SELECT coalesce(bool_and(
    has_table_privilege(:'app_user', relation.oid, 'SELECT') = expected.can_select
    AND has_table_privilege(:'app_user', relation.oid, 'INSERT') = expected.can_insert
    AND NOT has_table_privilege(:'app_user', relation.oid, 'UPDATE')
    AND NOT has_table_privilege(:'app_user', relation.oid, 'DELETE')
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
), expected(schema_name, relation_name, grantee, privilege_type, is_grantable) AS (
    SELECT 'public'::name,
           grants.relation_name,
           app_role.oid,
           grants.privilege_type,
           false
    FROM (
        VALUES
            ('principals', 'SELECT'),
            ('principals', 'INSERT'),
            ('workspaces', 'SELECT'),
            ('tasks', 'SELECT'),
            ('role_bindings', 'SELECT'),
            ('idempotency_records', 'SELECT'),
            ('idempotency_records', 'INSERT'),
            ('workspace_settings', 'SELECT'),
            ('workspace_settings', 'INSERT'),
            ('audit_events', 'SELECT'),
            ('audit_events', 'INSERT')
    ) AS grants(relation_name, privilege_type)
    CROSS JOIN app_role
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

WITH expected_relations(table_name, can_select, can_insert) AS (
    VALUES
        ('principals', true, true),
        ('workspaces', true, false),
        ('tasks', true, false),
        ('role_bindings', true, false),
        ('idempotency_records', true, true),
        ('workspace_settings', true, true),
        ('audit_events', true, true),
        ('migration_runs', false, false),
        ('alembic_version', false, false)
), allowed_updates(table_name, column_name) AS (
    VALUES
        ('principals', 'display_name'),
        ('principals', 'email_snapshot'),
        ('principals', 'updated_at'),
        ('idempotency_records', 'state'),
        ('idempotency_records', 'response_status'),
        ('idempotency_records', 'response_body'),
        ('idempotency_records', 'updated_at')
)
SELECT coalesce(bool_and(
    has_column_privilege(:'app_user', relation.oid, attribute.attnum, 'SELECT')
        = expected_relations.can_select
    AND has_column_privilege(:'app_user', relation.oid, attribute.attnum, 'INSERT')
        = expected_relations.can_insert
    AND has_column_privilege(:'app_user', relation.oid, attribute.attnum, 'UPDATE')
        = (allowed_updates.column_name IS NOT NULL)
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
            ('principals', 'updated_at'),
            ('idempotency_records', 'state'),
            ('idempotency_records', 'response_status'),
            ('idempotency_records', 'response_body'),
            ('idempotency_records', 'updated_at')
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

SELECT NOT EXISTS (
    SELECT 1
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND relation.relkind = 'S'
) AS sequence_catalog_ok
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

WITH expected(
    function_name,
    identity_arguments,
    function_kind,
    result_type,
    language_name,
    owner_name,
    returns_set,
    security_definer,
    leakproof,
    strict,
    volatility,
    parallel_safety,
    config_is_null,
    normalized_source
) AS (
    VALUES (
        'lls_reject_audit_event_mutation',
        '',
        'f',
        'trigger',
        'plpgsql',
        :'owner_user',
        false,
        false,
        false,
        false,
        'v',
        'u',
        true,
        'BEGIN RAISE EXCEPTION ''audit_events is append-only'' USING ERRCODE = ''55000''; END;'
    )
), actual AS (
    SELECT procedure.proname,
           pg_get_function_identity_arguments(procedure.oid),
           procedure.prokind::text,
           pg_get_function_result(procedure.oid),
           language.lanname,
           pg_get_userbyid(procedure.proowner)::text,
           procedure.proretset,
           procedure.prosecdef,
           procedure.proleakproof,
           procedure.proisstrict,
           procedure.provolatile::text,
           procedure.proparallel::text,
           procedure.proconfig IS NULL,
           btrim(regexp_replace(procedure.prosrc, '[[:space:]]+', ' ', 'g'))
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    JOIN pg_language AS language ON language.oid = procedure.prolang
    WHERE namespace.nspname = 'public'
)
SELECT NOT EXISTS (
    (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    UNION ALL
    (SELECT * FROM actual EXCEPT SELECT * FROM expected)
) AS function_catalog_ok
\gset

SELECT EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    WHERE namespace.nspname = 'public'
      AND procedure.proname = 'lls_reject_audit_event_mutation'
      AND pg_get_function_identity_arguments(procedure.oid) = ''
      AND pg_get_userbyid(procedure.proowner)::text = :'owner_user'
) AS function_owner_ok
\gset

WITH app_role AS (
    SELECT oid FROM pg_roles WHERE rolname = :'app_user'
)
SELECT NOT EXISTS (
    SELECT 1
    FROM pg_proc AS procedure
    JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
    CROSS JOIN app_role
    CROSS JOIN LATERAL aclexplode(
        coalesce(procedure.proacl, acldefault('f', procedure.proowner))
    ) AS privilege
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
      AND namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'
      AND privilege.grantee IN (0, app_role.oid)
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

\echo 'runtime role verification passed'
