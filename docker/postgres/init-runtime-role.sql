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
        ('audit_events', 'r'),
        ('migration_runs', 'r'),
        ('alembic_version', 'r')
), expected_function(function_name, identity_arguments) AS (
    VALUES ('lls_reject_audit_event_mutation', '')
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
)
SELECT database_owner.is_expected
   AND schema_owner.is_expected
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

SELECT format('GRANT SELECT, INSERT ON TABLE public.principals TO %I', :'app_user')
WHERE to_regclass('public.principals') IS NOT NULL
\gexec
SELECT format(
    'GRANT UPDATE (display_name, email_snapshot, updated_at) ON TABLE public.principals TO %I',
    :'app_user'
)
WHERE to_regclass('public.principals') IS NOT NULL
\gexec

SELECT format('GRANT SELECT ON TABLE public.workspaces TO %I', :'app_user')
WHERE to_regclass('public.workspaces') IS NOT NULL
\gexec
SELECT format('GRANT SELECT ON TABLE public.tasks TO %I', :'app_user')
WHERE to_regclass('public.tasks') IS NOT NULL
\gexec
SELECT format('GRANT SELECT ON TABLE public.role_bindings TO %I', :'app_user')
WHERE to_regclass('public.role_bindings') IS NOT NULL
\gexec

SELECT format('GRANT SELECT, INSERT ON TABLE public.idempotency_records TO %I', :'app_user')
WHERE to_regclass('public.idempotency_records') IS NOT NULL
\gexec
SELECT format(
    'GRANT UPDATE (state, response_status, response_body, updated_at) '
    'ON TABLE public.idempotency_records TO %I',
    :'app_user'
)
WHERE to_regclass('public.idempotency_records') IS NOT NULL
\gexec

SELECT format('GRANT SELECT, INSERT ON TABLE public.workspace_settings TO %I', :'app_user')
WHERE to_regclass('public.workspace_settings') IS NOT NULL
\gexec
SELECT format('GRANT SELECT, INSERT ON TABLE public.audit_events TO %I', :'app_user')
WHERE to_regclass('public.audit_events') IS NOT NULL
\gexec
