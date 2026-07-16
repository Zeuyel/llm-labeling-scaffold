from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg import sql
from sqlalchemy.engine import URL, make_url


ROOT = Path(__file__).resolve().parents[1]
ROLE_SCRIPT = ROOT / "docker" / "postgres" / "init-runtime-role.sh"
ROLE_SQL = ROOT / "docker" / "postgres" / "init-runtime-role.sql"
LOOPBACK_COMPOSE = ROOT / "docker-compose.loopback.yml"
VERIFY_ROLE_SQL = ROOT / "docker" / "postgres" / "verify-runtime-role.sql"


@dataclass(frozen=True)
class _RuntimeDatabase:
    owner_url: URL
    app_url: URL
    app_user: str
    helper_role: str
    non_app_user: str = ""


def _render_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _render_psycopg_url(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _psql_env(url: URL) -> dict[str, str]:
    return {**os.environ, "PGPASSWORD": url.password or ""}


def _url_host(url: URL) -> str:
    return url.host or str(url.query.get("host") or "127.0.0.1")


def _url_port(url: URL) -> int:
    return url.port or int(url.query.get("port") or 5432)


def _run_sql(url: URL, statement: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "--set",
            "ON_ERROR_STOP=1",
            "--host",
            _url_host(url),
            "--port",
            str(_url_port(url)),
            "--username",
            url.username or "",
            "--dbname",
            url.database or "",
            "--command",
            statement,
        ],
        env=_psql_env(url),
        text=True,
        capture_output=True,
        check=check,
    )


def _runtime_role_env(database: _RuntimeDatabase, *, verify: bool = False) -> dict[str, str]:
    env = {
        **_psql_env(database.owner_url),
        "PGHOST": _url_host(database.owner_url),
        "PGPORT": str(_url_port(database.owner_url)),
        "POSTGRES_USER": database.owner_url.username or "",
        "POSTGRES_DB": database.owner_url.database or "",
        "SCAFFOLD_POSTGRES_APP_USER": database.app_user,
        "SCAFFOLD_POSTGRES_APP_PASSWORD": database.app_url.password or "",
    }
    if verify:
        env["LLS_RUNTIME_ROLE_VERIFY_SQL_PATH"] = str(VERIFY_ROLE_SQL)
    return env


def _run_role_script(
    database: _RuntimeDatabase, *, verify: bool = False, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ROLE_SCRIPT)],
        cwd=ROOT,
        env=_runtime_role_env(database, verify=verify),
        text=True,
        capture_output=True,
        check=check,
    )


def _run_verifier(
    database: _RuntimeDatabase, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "psql",
            "--set",
            "ON_ERROR_STOP=1",
            "--host",
            _url_host(database.owner_url),
            "--port",
            str(_url_port(database.owner_url)),
            "--username",
            database.owner_url.username or "",
            "--dbname",
            database.owner_url.database or "",
            "--set",
            f"owner_user={database.owner_url.username or ''}",
            "--set",
            f"app_user={database.app_user}",
            "--file",
            str(VERIFY_ROLE_SQL),
        ],
        env=_psql_env(database.owner_url),
        text=True,
        capture_output=True,
        check=check,
    )


@pytest.fixture(scope="module")
def postgres_runtime_database() -> _RuntimeDatabase:
    owner_url_value = os.environ.get("LLS_TEST_POSTGRES_URL")
    app_url_value = os.environ.get("LLS_TEST_POSTGRES_APP_URL")
    if not owner_url_value or not app_url_value:
        pytest.skip("PostgreSQL owner/app test URLs are not set")

    token = uuid.uuid4().hex[:12]
    database_name = f"lls73_{token}"
    app_user = f"lls73_app_{token}"
    helper_role = f"lls73_parent_{token}"
    non_app_user = f"lls73_nonapp_{token}"
    source_owner_url = make_url(owner_url_value)
    source_app_url = make_url(app_url_value)
    owner_url = source_owner_url.set(database=database_name)
    app_url = source_owner_url.set(
        username=app_user,
        password=source_app_url.password,
        database=database_name,
    )
    database = _RuntimeDatabase(
        owner_url=owner_url,
        app_url=app_url,
        app_user=app_user,
        helper_role=helper_role,
        non_app_user=non_app_user,
    )
    maintenance_url = source_owner_url.set(database="postgres")

    with psycopg.connect(_render_psycopg_url(maintenance_url), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(helper_role)))
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(non_app_user),
                sql.Literal(source_app_url.password or "non-app-test-password"),
            )
        )

    try:
        _run_role_script(database)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={
                **os.environ,
                "LLS_DATABASE_URL": _render_url(owner_url),
                "SCAFFOLD_POSTGRES_APP_USER": app_user,
            },
            text=True,
            capture_output=True,
            check=True,
        )
        _run_role_script(database, verify=True)
        yield database
    finally:
        with psycopg.connect(_render_psycopg_url(maintenance_url), autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(app_user)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(helper_role)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(non_app_user)))


def _fake_psql(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "psql-args"
    executable = bin_dir / "psql"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$PSQL_ARGS_FILE"\n', encoding="ascii")
    executable.chmod(0o755)
    return bin_dir, args_file


def _role_script_env(bin_dir: Path, args_file: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PSQL_ARGS_FILE": str(args_file),
        "PGPASSWORD": "owner-test-password",
        "POSTGRES_USER": "scaffold_owner",
        "POSTGRES_DB": "scaffold",
        "SCAFFOLD_POSTGRES_APP_USER": "scaffold_app",
        "SCAFFOLD_POSTGRES_APP_PASSWORD": "app-test-password",
    }


def _quoted_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def test_runtime_role_script_defaults_sql_to_its_own_directory(tmp_path: Path):
    bin_dir, args_file = _fake_psql(tmp_path)

    subprocess.run(
        [str(ROLE_SCRIPT)],
        cwd=tmp_path,
        env=_role_script_env(bin_dir, args_file),
        check=True,
    )

    args_text = args_file.read_text(encoding="utf-8")
    args = args_text.splitlines()
    assert args[args.index("--file") + 1] == str(ROLE_SQL)
    assert "app-test-password" not in args_text
    assert "app_password=" not in args_text


def test_compose_role_initialization_paths_are_explicit():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    panel = services["panel"]
    panel_environment = panel["environment"]
    assert "ports" not in panel
    assert panel["expose"] == ["8765"]
    assert panel["networks"] == ["lls", "scaffold-db"]
    assert panel_environment["LLS_PANEL_DEPLOYMENT_MODE"] == "docker_tunnel"
    assert panel_environment["LLS_PANEL_AUTH_MODE"] == "${LLS_PANEL_AUTH_MODE:-cloudflare_access}"
    assert panel_environment["LLS_CF_ACCESS_ISSUER"] == "${LLS_CF_ACCESS_ISSUER:-}"
    assert panel_environment["LLS_CF_ACCESS_AUD"] == "${LLS_CF_ACCESS_AUD:-}"
    assert panel_environment["LLS_MCP_CF_ACCESS_ISSUER"] == "${LLS_MCP_CF_ACCESS_ISSUER:-}"
    assert panel_environment["LLS_MCP_CF_ACCESS_AUD"] == "${LLS_MCP_CF_ACCESS_AUD:-}"
    assert panel_environment["LLS_DATABASE_USER"] == "${SCAFFOLD_POSTGRES_APP_USER:-scaffold_app}"
    assert panel_environment["LLS_DATABASE_PASSWORD"] == (
        "${SCAFFOLD_POSTGRES_APP_PASSWORD:?Set SCAFFOLD_POSTGRES_APP_PASSWORD}"
    )

    loopback_compose = yaml.safe_load(LOOPBACK_COMPOSE.read_text(encoding="utf-8"))
    loopback_panel = loopback_compose["services"]["panel"]
    assert loopback_panel["ports"] == ["127.0.0.1:${PANEL_PORT:-8765}:8765"]
    assert loopback_panel["environment"]["LLS_PANEL_DEPLOYMENT_MODE"] == "docker_loopback"
    assert "PANEL_BIND_HOST" not in LOOPBACK_COMPOSE.read_text(encoding="utf-8")
    assert '"--host", "0.0.0.0"' in (ROOT / "docker" / "panel" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    migrate_environment = set(services["migrate"]["environment"])
    assert "LLS_DATABASE_USER=${SCAFFOLD_POSTGRES_OWNER_USER:-scaffold_owner}" in migrate_environment
    assert (
        "LLS_DATABASE_PASSWORD=${SCAFFOLD_POSTGRES_OWNER_PASSWORD:?Set SCAFFOLD_POSTGRES_OWNER_PASSWORD}"
        in migrate_environment
    )
    assert "SCAFFOLD_POSTGRES_APP_USER=${SCAFFOLD_POSTGRES_APP_USER:-scaffold_app}" in migrate_environment

    materializer = services["materializer"]
    materializer_environment = set(materializer["environment"])
    assert materializer["restart"] == "unless-stopped"
    assert "LLS_DATABASE_USER=${SCAFFOLD_POSTGRES_APP_USER:-scaffold_app}" in materializer_environment
    assert (
        "LLS_DATABASE_PASSWORD=${SCAFFOLD_POSTGRES_APP_PASSWORD:?Set SCAFFOLD_POSTGRES_APP_PASSWORD}"
        in materializer_environment
    )
    assert materializer["command"] == [
        "python",
        "-m",
        "llm_labeling_scaffold.cli",
        "db",
        "materialize",
        "--runs-root",
        "/app/runs",
        "--tasks-root",
        "/app/tasks",
    ]
    assert materializer["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "./runs:/app/runs" in materializer["volumes"]
    assert "./tasks:/app/tasks" in materializer["volumes"]

    first_init = services["scaffold-postgres"]
    assert first_init["environment"]["LLS_RUNTIME_ROLE_SQL_PATH"] == (
        "/usr/local/share/lls/init-runtime-role.sql"
    )
    assert "./docker/postgres/init-runtime-role.sh:/docker-entrypoint-initdb.d/10-init-runtime-role.sh:ro" in (
        first_init["volumes"]
    )
    assert "./docker/postgres/init-runtime-role.sql:/usr/local/share/lls/init-runtime-role.sql:ro" in (
        first_init["volumes"]
    )

    existing_database_init = services["db-role-init"]
    assert existing_database_init["environment"]["LLS_RUNTIME_ROLE_SQL_PATH"] == (
        "/usr/local/share/lls/init-runtime-role.sql"
    )
    assert "./docker/postgres/init-runtime-role.sh:/usr/local/bin/lls-init-runtime-role:ro" in (
        existing_database_init["volumes"]
    )
    assert "./docker/postgres/init-runtime-role.sql:/usr/local/share/lls/init-runtime-role.sql:ro" in (
        existing_database_init["volumes"]
    )

    verification = services["db-role-verify"]
    assert verification["depends_on"] == {
        "migrate": {"condition": "service_completed_successfully"}
    }
    assert verification["environment"]["LLS_RUNTIME_ROLE_VERIFY_SQL_PATH"] == (
        "/usr/local/share/lls/verify-runtime-role.sql"
    )
    assert "./docker/postgres/verify-runtime-role.sql:/usr/local/share/lls/verify-runtime-role.sql:ro" in (
        verification["volumes"]
    )
    assert services["panel"]["depends_on"]["db-role-verify"] == {
        "condition": "service_completed_successfully"
    }


def test_compose_database_network_is_internal_and_owner_secret_is_not_in_panel():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert compose["networks"]["scaffold-db"]["internal"] is True
    assert services["scaffold-postgres"]["networks"] == ["scaffold-db"]
    assert set(services["panel"]["networks"]) == {"lls", "scaffold-db"}
    assert "scaffold-db" not in services["mcp"]["networks"]
    assert all("OWNER_PASSWORD" not in value for value in services["panel"]["environment"])


def test_runtime_role_sql_uses_explicit_fail_closed_privileges():
    sql = ROLE_SQL.read_text(encoding="utf-8")
    verifier_sql = VERIFY_ROLE_SQL.read_text(encoding="utf-8")

    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES" not in sql
    assert "REVOKE ALL PRIVILEGES ON DATABASE" in sql
    assert "pg_auth_members" in sql
    assert "relkind IN ('r', 'p', 'v', 'm', 'f')" in sql
    assert "REVOKE EXECUTE ON ALL FUNCTIONS" in sql
    assert "GRANT UPDATE (role)" not in sql
    assert "('audit_events', true, true, false, false)" in sql
    assert "('workspace_settings', true, true, true, false)" in sql
    assert "('allocation_collection_receipts', true, true, false, false)" in sql
    assert "GRANT %s ON TABLE public.%I TO %I" in sql
    assert "IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO" not in sql
    assert "lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, text, uuid, text, integer, text, boolean, text)" in sql
    assert "lls_sensitive_json_object_is_valid" in sql
    assert "\\getenv app_password SCAFFOLD_POSTGRES_APP_PASSWORD" in sql
    assert "AND rolsuper AS cluster_admin_ok" in sql
    assert "FROM pg_database AS target_database" in sql
    assert "FOR ROLE %I REVOKE ALL ON FUNCTIONS FROM PUBLIC" in sql
    assert "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA %I FROM %I" in sql
    assert "namespace.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')" in sql
    assert "namespace.nspname !~ '^pg_(toast_)?temp_[0-9]+$'" in verifier_sql
    assert "AS user_schema_catalog_ok" in verifier_sql
    assert "pg_get_userbyid(target_database.datdba)" in verifier_sql
    assert "pg_get_userbyid(namespace.nspowner)" in verifier_sql
    assert "pg_get_userbyid(relation.relowner)" in verifier_sql
    assert "pg_get_userbyid(procedure.proowner)" in verifier_sql
    assert "ownership_preflight_ok" in ROLE_SQL.read_text(encoding="utf-8")
    assert "proiswindow" not in verifier_sql
    assert "procedure.prokind = 'f'" in verifier_sql
    assert "'lls_complete_idempotency'" in verifier_sql
    assert "'lls_workspace_group_contract'" in verifier_sql
    assert "trigger.tgtype" in verifier_sql
    assert "trigger.tgenabled" in verifier_sql
    assert "trigger.tgfoid" in verifier_sql
    assert "procedure.prosrc" in verifier_sql
    assert "--set app_password" not in ROLE_SCRIPT.read_text(encoding="utf-8")
    assert "'{}'::aclitem[]" not in verifier_sql
    assert "aclexplode(attribute.attacl)" in verifier_sql
    function_acl_section = verifier_sql[
        verifier_sql.index("), actual AS (\n    SELECT namespace.nspname,\n           procedure.proname")
        : verifier_sql.index(") AS function_acl_catalog_ok")
    ]
    assert "privilege.grantee IN (0, app_role.oid)" not in function_acl_section
    assert "privilege.grantee <> procedure.proowner" in function_acl_section


def test_runtime_role_script_runs_optional_verification(tmp_path: Path):
    bin_dir, args_file = _fake_psql(tmp_path)
    env = {
        **_role_script_env(bin_dir, args_file),
        "LLS_RUNTIME_ROLE_VERIFY_SQL_PATH": str(VERIFY_ROLE_SQL),
    }

    subprocess.run([str(ROLE_SCRIPT)], cwd=tmp_path, env=env, check=True)

    args_text = args_file.read_text(encoding="utf-8")
    args = args_text.splitlines()
    assert args[args.index("--file") + 1] == str(VERIFY_ROLE_SQL)
    assert "app-test-password" not in args_text


def test_runtime_role_cannot_create_schema(postgres_runtime_database: _RuntimeDatabase):
    result = _run_sql(
        postgres_runtime_database.app_url,
        "CREATE SCHEMA runtime_forbidden",
        check=False,
    )

    assert result.returncode != 0
    assert "permission denied" in result.stderr


def test_runtime_role_final_schema_acl_matches_service_dependencies(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    expected_relations = {
        "principals": (True, True, False, False),
        "workspaces": (True, True, True, False),
        "tasks": (True, True, True, False),
        "role_bindings": (True, True, True, True),
        "idempotency_records": (True, True, False, False),
        "workspace_settings": (True, True, True, False),
        "task_drafts": (True, True, True, False),
        "task_revisions": (True, True, False, False),
        "task_revision_materializations": (True, True, True, False),
        "argilla_connection_bindings": (True, True, True, False),
        "argilla_annotator_mappings": (True, True, True, False),
        "annotator_cohorts": (True, True, True, False),
        "annotator_cohort_revisions": (True, True, True, False),
        "annotator_cohort_members": (True, True, True, True),
        "allocation_plans": (True, True, True, False),
        "allocation_plan_states": (True, False, True, False),
        "allocation_workspace_groups": (True, True, True, False),
        "allocation_dataset_groups": (True, True, True, False),
        "allocation_dataset_group_states": (True, False, True, False),
        "allocation_assignment_items": (True, True, False, False),
        "allocation_record_bindings": (True, False, True, False),
        "allocation_assignments": (True, True, True, False),
        "allocation_collection_receipts": (True, True, False, False),
        "audit_events": (True, True, False, False),
        "migration_runs": (False, False, False, False),
        "alembic_version": (False, False, False, False),
    }
    allowed_functions = {
        "lls_canonical_sensitive_json_text(document json)",
        "lls_complete_idempotency(p_record_id uuid, p_workspace_id uuid, p_actor_principal_id uuid, p_caller_principal_id uuid, p_operation text, p_idempotency_key_hash text, p_request_fingerprint text, p_required_permission text, p_resource_type text, p_resource_id uuid, p_channel text, p_response_status integer, p_response_body text, p_succeeded boolean, p_request_id text)",
        "lls_sensitive_json_node_is_valid(document json, current_depth integer)",
        "lls_sensitive_json_object_is_valid(document json)",
        "lls_sensitive_json_string_is_safe(value text)",
        "lls_validate_allocation_plan_graph(target_plan_id uuid)",
    }

    with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
        for relation_name, expected_privileges in expected_relations.items():
            actual_privileges = connection.execute(
                """
                SELECT has_table_privilege(%s, %s, 'SELECT'),
                       has_table_privilege(%s, %s, 'INSERT'),
                       has_table_privilege(%s, %s, 'UPDATE'),
                       has_table_privilege(%s, %s, 'DELETE')
                """,
                (database.app_user, relation_name) * 4,
            ).fetchone()
            assert actual_privileges == expected_privileges

        sequence_privileges = connection.execute(
            """
            SELECT has_sequence_privilege(%s, 'lls_idempotency_completion_gate_seq', 'USAGE'),
                   has_sequence_privilege(%s, 'lls_idempotency_completion_gate_seq', 'SELECT')
            """,
            (database.app_user, database.app_user),
        ).fetchone()
        assert sequence_privileges == (False, False)

        function_rows = connection.execute(
            """
            SELECT pg_get_function_identity_arguments(procedure.oid),
                   procedure.proname,
                   has_function_privilege(%s, procedure.oid, 'EXECUTE')
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = 'public'
            ORDER BY procedure.proname, pg_get_function_identity_arguments(procedure.oid)
            """,
            (database.app_user,),
        ).fetchall()
        executable_functions = {
            f"{function_name}({identity_arguments})"
            for identity_arguments, function_name, can_execute in function_rows
            if can_execute
        }
        assert executable_functions == allowed_functions
        assert connection.execute(
            """
            SELECT count(*)
            FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
            CROSS JOIN LATERAL aclexplode(
                coalesce(procedure.proacl, acldefault('f', procedure.proowner))
            ) AS privilege
            WHERE namespace.nspname = 'public'
              AND privilege.grantee = 0
            """
        ).fetchone()[0] == 0

        non_app_completion_privileges = connection.execute(
            """
            SELECT has_table_privilege(%s, 'idempotency_records', 'INSERT'),
                   has_function_privilege(
                       %s,
                       'lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, text, uuid, text, integer, text, boolean, text)',
                       'EXECUTE'
                   )
            """,
            (database.non_app_user, database.non_app_user),
        ).fetchone()
        assert non_app_completion_privileges == (False, False)


def test_runtime_role_verifier_rejects_completion_execute_grant_to_insert_capable_non_app(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    non_app_identifier = _quoted_identifier(database.non_app_user)
    completion_function = (
        "public.lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, "
        "text, uuid, text, integer, text, boolean, text)"
    )
    _run_sql(
        database.owner_url,
        f"GRANT INSERT ON TABLE public.idempotency_records TO {non_app_identifier}; "
        f"GRANT EXECUTE ON FUNCTION {completion_function} TO {non_app_identifier}",
    )
    try:
        with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
            assert connection.execute(
                """
                SELECT has_table_privilege(%s, 'idempotency_records', 'INSERT'),
                       has_function_privilege(%s, %s, 'EXECUTE')
                """,
                (database.non_app_user, database.non_app_user, completion_function),
            ).fetchone() == (True, True)

        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime function ACL catalog verification failed" in drift.stdout

        _run_role_script(database, verify=True)
        with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
            assert connection.execute(
                """
                SELECT has_table_privilege(%s, 'idempotency_records', 'INSERT'),
                       has_function_privilege(%s, %s, 'EXECUTE'),
                       has_function_privilege(%s, %s, 'EXECUTE')
                """,
                (
                    database.non_app_user,
                    database.non_app_user,
                    completion_function,
                    database.app_user,
                    completion_function,
                ),
            ).fetchone() == (True, False, True)
    finally:
        _run_sql(
            database.owner_url,
            f"REVOKE INSERT ON TABLE public.idempotency_records FROM {non_app_identifier}; "
            f"REVOKE EXECUTE ON FUNCTION {completion_function} FROM {non_app_identifier}",
            check=False,
        )

    _run_verifier(database)


@pytest.mark.skipif(
    not os.environ.get("LLS_TEST_POSTGRES_URL") or not os.environ.get("LLS_TEST_POSTGRES_APP_URL"),
    reason="PostgreSQL owner/app test URLs are not set",
)
def test_sensitive_json_migration_completion_execute_acl_uses_explicit_app_role():
    source_owner_url = make_url(os.environ["LLS_TEST_POSTGRES_URL"])
    source_app_url = make_url(os.environ["LLS_TEST_POSTGRES_APP_URL"])
    token = uuid.uuid4().hex[:12]
    database_name = f"lls75_acl_{token}"
    app_user = f"lls75_app_{token}"
    non_app_user = f"lls75_nonapp_{token}"
    owner_url = source_owner_url.set(database=database_name)
    app_url = source_owner_url.set(
        username=app_user,
        password=source_app_url.password,
        database=database_name,
    )
    database = _RuntimeDatabase(
        owner_url=owner_url,
        app_url=app_url,
        app_user=app_user,
        helper_role="",
        non_app_user=non_app_user,
    )
    maintenance_url = source_owner_url.set(database="postgres")
    completion_function = (
        "public.lls_complete_idempotency(uuid, uuid, uuid, uuid, text, text, text, text, "
        "text, uuid, text, integer, text, boolean, text)"
    )

    with psycopg.connect(_render_psycopg_url(maintenance_url), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(non_app_user),
                sql.Literal(source_app_url.password or "non-app-test-password"),
            )
        )

    alembic_env = {
        **os.environ,
        "LLS_DATABASE_URL": _render_url(owner_url),
        "SCAFFOLD_POSTGRES_APP_USER": app_user,
    }
    try:
        _run_role_script(database)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "20260716_0004"],
            cwd=ROOT,
            env=alembic_env,
            text=True,
            capture_output=True,
            check=True,
        )
        _run_sql(
            owner_url,
            f"GRANT INSERT ON TABLE public.idempotency_records TO {_quoted_identifier(non_app_user)}",
        )
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=alembic_env,
            text=True,
            capture_output=True,
            check=True,
        )
        with psycopg.connect(_render_psycopg_url(owner_url)) as connection:
            assert connection.execute(
                """
                SELECT has_table_privilege(%s, 'idempotency_records', 'INSERT'),
                       has_function_privilege(%s, %s, 'EXECUTE'),
                       has_function_privilege(%s, %s, 'EXECUTE'),
                       EXISTS (
                           SELECT 1
                           FROM pg_proc AS procedure
                           JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace
                           CROSS JOIN LATERAL aclexplode(
                               coalesce(procedure.proacl, acldefault('f', procedure.proowner))
                           ) AS privilege
                           WHERE namespace.nspname = 'public'
                             AND procedure.proname = 'lls_complete_idempotency'
                             AND pg_get_function_identity_arguments(procedure.oid)
                                 = 'p_record_id uuid, p_workspace_id uuid, p_actor_principal_id uuid, p_caller_principal_id uuid, p_operation text, p_idempotency_key_hash text, p_request_fingerprint text, p_required_permission text, p_resource_type text, p_resource_id uuid, p_channel text, p_response_status integer, p_response_body text, p_succeeded boolean, p_request_id text'
                             AND privilege.grantee = 0
                       )
                """,
                (
                    non_app_user,
                    non_app_user,
                    completion_function,
                    app_user,
                    completion_function,
                ),
            ).fetchone() == (True, False, True, False)
    finally:
        with psycopg.connect(_render_psycopg_url(maintenance_url), autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(app_user)))
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(non_app_user)))


def test_runtime_role_verifier_accepts_absent_acl_catalog_entries(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
        null_column_acl_count = connection.execute(
            """
            SELECT count(*)
            FROM pg_attribute AS attribute
            JOIN pg_class AS relation ON relation.oid = attribute.attrelid
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND relation.relkind = 'r'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND attribute.attacl IS NULL
            """
        ).fetchone()[0]
        missing_schema_default_count = connection.execute(
            """
            WITH object_types(object_type) AS (
                VALUES ('r'::"char"), ('S'::"char"), ('f'::"char")
            )
            SELECT count(*)
            FROM object_types
            CROSS JOIN pg_roles AS owner_role
            CROSS JOIN pg_namespace AS namespace
            LEFT JOIN pg_default_acl AS default_acl
              ON default_acl.defaclrole = owner_role.oid
             AND default_acl.defaclnamespace = namespace.oid
             AND default_acl.defaclobjtype = object_types.object_type
            WHERE owner_role.rolname = %s
              AND namespace.nspname = 'public'
              AND default_acl.oid IS NULL
            """,
            (database.owner_url.username,),
        ).fetchone()[0]

    assert null_column_acl_count > 0
    assert missing_schema_default_count > 0
    result = _run_verifier(database)
    assert "runtime role verification passed" in result.stdout


def test_runtime_role_verifier_ignores_system_and_temporary_schemas(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    with psycopg.connect(_render_psycopg_url(database.owner_url), autocommit=True) as connection:
        connection.execute(
            "CREATE TEMP TABLE runtime_temp_boundary "
            "(id bigint GENERATED ALWAYS AS IDENTITY)"
        )
        temp_schema = connection.execute(
            "SELECT nspname FROM pg_namespace WHERE oid = pg_my_temp_schema()"
        ).fetchone()[0]

        assert temp_schema.startswith("pg_temp_")
        _run_role_script(database, verify=True)


def test_runtime_role_init_cleans_unknown_schema_acl_and_catalog_remains_closed(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    schema_name = f"runtime_unknown_{uuid.uuid4().hex[:12]}"
    schema_identifier = _quoted_identifier(schema_name)
    app_identifier = _quoted_identifier(database.app_user)
    table_name = f"{schema_name}.runtime_table"
    sequence_name = f"{schema_name}.runtime_sequence"
    function_name = f"{schema_name}.runtime_function()"

    _run_sql(
        database.owner_url,
        f"CREATE SCHEMA {schema_identifier}; "
        f"CREATE TABLE {schema_identifier}.runtime_table (id integer); "
        f"CREATE SEQUENCE {schema_identifier}.runtime_sequence; "
        f"CREATE FUNCTION {schema_identifier}.runtime_function() "
        "RETURNS integer LANGUAGE sql AS 'SELECT 1'",
    )
    try:
        _run_sql(
            database.owner_url,
            f"GRANT SELECT ON TABLE {schema_identifier}.runtime_table TO {app_identifier}",
        )
        object_drift = _run_verifier(database, check=False)
        assert object_drift.returncode == 3
        assert "runtime relation ACL catalog verification failed" in object_drift.stdout

        _run_sql(
            database.owner_url,
            f"GRANT USAGE ON SCHEMA {schema_identifier} TO {app_identifier}; "
            f"GRANT UPDATE (id) ON TABLE {schema_identifier}.runtime_table TO PUBLIC; "
            f"GRANT USAGE ON SEQUENCE {schema_identifier}.runtime_sequence TO {app_identifier}; "
            f"GRANT EXECUTE ON FUNCTION {schema_identifier}.runtime_function() TO PUBLIC",
        )
        schema_drift = _run_verifier(database, check=False)
        assert schema_drift.returncode == 3
        assert "runtime schema ACL catalog verification failed" in schema_drift.stdout

        _run_role_script(database)
        with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
            cleaned_privileges = connection.execute(
                """
                SELECT has_schema_privilege(%s, %s, 'USAGE'),
                       has_table_privilege(%s, %s, 'SELECT'),
                       has_column_privilege(%s, %s, 'id', 'UPDATE'),
                       has_sequence_privilege(%s, %s, 'USAGE'),
                       has_function_privilege(%s, %s, 'EXECUTE')
                """,
                (
                    database.app_user,
                    schema_name,
                    database.app_user,
                    table_name,
                    database.app_user,
                    table_name,
                    database.app_user,
                    sequence_name,
                    database.app_user,
                    function_name,
                ),
            ).fetchone()
        assert cleaned_privileges == (False, False, False, False, False)

        _run_sql(
            database.owner_url,
            f"DROP TABLE {schema_identifier}.runtime_table; "
            f"DROP SEQUENCE {schema_identifier}.runtime_sequence; "
            f"DROP FUNCTION {schema_identifier}.runtime_function()",
        )
        catalog_drift = _run_verifier(database, check=False)
        assert catalog_drift.returncode == 3
        assert "runtime user schema catalog verification failed" in catalog_drift.stdout
    finally:
        _run_sql(database.owner_url, f"DROP SCHEMA IF EXISTS {schema_identifier} CASCADE")

    _run_verifier(database)


def test_runtime_role_membership_is_rejected_and_cleaned(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        f'GRANT "{database.helper_role}" TO "{database.app_user}"',
    )

    drift = _run_verifier(database, check=False)
    assert drift.returncode == 3
    assert "runtime role membership verification failed" in drift.stdout

    _run_role_script(database, verify=True)
    set_role = _run_sql(database.app_url, f'SET ROLE "{database.helper_role}"', check=False)
    assert set_role.returncode != 0
    assert "permission denied to set role" in set_role.stderr


def test_runtime_role_cannot_update_unlisted_column(
    postgres_runtime_database: _RuntimeDatabase,
):
    result = _run_sql(
        postgres_runtime_database.app_url,
        "UPDATE public.principals SET is_active = false WHERE false",
        check=False,
    )

    assert result.returncode != 0
    assert "permission denied" in result.stderr


@pytest.mark.parametrize(
    ("grant_sql", "revoke_sql"),
    [
        (
            'GRANT SELECT (id) ON TABLE public.principals TO "{app_user}"',
            'REVOKE SELECT (id) ON TABLE public.principals FROM "{app_user}"',
        ),
        (
            'GRANT INSERT (id) ON TABLE public.principals TO "{app_user}"',
            'REVOKE INSERT (id) ON TABLE public.principals FROM "{app_user}"',
        ),
        (
            'GRANT UPDATE (is_active) ON TABLE public.principals TO "{app_user}"',
            'REVOKE UPDATE (is_active) ON TABLE public.principals FROM "{app_user}"',
        ),
        (
            'GRANT REFERENCES (id) ON TABLE public.principals TO "{app_user}"',
            'REVOKE REFERENCES (id) ON TABLE public.principals FROM "{app_user}"',
        ),
        (
            "GRANT UPDATE (is_active) ON TABLE public.principals TO PUBLIC",
            "REVOKE UPDATE (is_active) ON TABLE public.principals FROM PUBLIC",
        ),
    ],
)
def test_runtime_role_verifier_rejects_column_acl_drift(
    postgres_runtime_database: _RuntimeDatabase,
    grant_sql: str,
    revoke_sql: str,
):
    database = postgres_runtime_database
    _run_sql(database.owner_url, grant_sql.format(app_user=database.app_user))
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime column" in drift.stdout
    finally:
        _run_sql(database.owner_url, revoke_sql.format(app_user=database.app_user))

    _run_verifier(database)


def test_runtime_role_initialization_repairs_column_acl_drift(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        f'GRANT SELECT (id) ON TABLE public.principals TO "{database.app_user}"',
    )
    _run_sql(
        database.owner_url,
        "GRANT UPDATE (is_active) ON TABLE public.principals TO PUBLIC",
    )

    _run_role_script(database, verify=True)


def test_runtime_role_allows_owner_column_acl_across_repeated_initialization(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    owner_identifier = _quoted_identifier(database.owner_url.username or "")
    _run_sql(
        database.owner_url,
        f"REVOKE SELECT ON TABLE public.migration_runs FROM {owner_identifier}; "
        f"GRANT SELECT (id) ON TABLE public.migration_runs TO {owner_identifier}",
    )
    try:
        with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
            owner_acl_count = connection.execute(
                """
                SELECT count(*)
                FROM pg_attribute AS attribute
                JOIN pg_class AS relation ON relation.oid = attribute.attrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                CROSS JOIN LATERAL aclexplode(attribute.attacl) AS privilege
                WHERE namespace.nspname = 'public'
                  AND relation.relname = 'migration_runs'
                  AND attribute.attname = 'id'
                  AND privilege.grantee = relation.relowner
                """
            ).fetchone()[0]
        assert owner_acl_count > 0

        _run_role_script(database, verify=True)
        _run_role_script(database, verify=True)
    finally:
        _run_sql(
            database.owner_url,
            f"REVOKE SELECT (id) ON TABLE public.migration_runs FROM {owner_identifier}; "
            f"GRANT ALL PRIVILEGES ON TABLE public.migration_runs TO {owner_identifier}",
        )

    _run_verifier(database)


def test_runtime_role_verifier_rejects_unknown_relation_replacement(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "ALTER TABLE public.migration_runs RENAME TO runtime_unknown_relation",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime relation catalog verification failed" in drift.stdout
    finally:
        _run_sql(
            database.owner_url,
            "ALTER TABLE public.runtime_unknown_relation RENAME TO migration_runs",
        )

    _run_verifier(database)


def test_runtime_role_verifier_rejects_unknown_sequence_without_privileges(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(database.owner_url, "CREATE SEQUENCE public.runtime_drift_sequence")
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime sequence catalog verification failed" in drift.stdout
    finally:
        _run_sql(database.owner_url, "DROP SEQUENCE public.runtime_drift_sequence")

    _run_verifier(database)


def test_runtime_role_verifier_rejects_unknown_function_without_privileges(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "CREATE FUNCTION public.runtime_unknown_function() "
        "RETURNS integer LANGUAGE sql AS 'SELECT 1'",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime function catalog verification failed" in drift.stdout
    finally:
        _run_sql(database.owner_url, "DROP FUNCTION public.runtime_unknown_function()")

    _run_verifier(database)


@pytest.mark.parametrize("object_kind", ["database", "schema", "relation", "function"])
def test_runtime_role_ownership_drift_fails_closed_without_init_repair(
    postgres_runtime_database: _RuntimeDatabase,
    object_kind: str,
):
    database = postgres_runtime_database
    owner_identifier = _quoted_identifier(database.owner_url.username or "")
    app_identifier = _quoted_identifier(database.app_user)
    database_identifier = _quoted_identifier(database.owner_url.database or "")

    if object_kind == "database":
        drift_sql = f"ALTER DATABASE {database_identifier} OWNER TO {app_identifier}"
        restore_sql = f"ALTER DATABASE {database_identifier} OWNER TO {owner_identifier}"
        owner_query = (
            "SELECT pg_get_userbyid(datdba) FROM pg_database "
            "WHERE datname = current_database()"
        )
    elif object_kind == "schema":
        drift_sql = f"ALTER SCHEMA public OWNER TO {app_identifier}"
        restore_sql = "ALTER SCHEMA public OWNER TO pg_database_owner"
        owner_query = (
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace "
            "WHERE nspname = 'public'"
        )
    elif object_kind == "relation":
        drift_sql = f"ALTER TABLE public.principals OWNER TO {app_identifier}"
        restore_sql = f"ALTER TABLE public.principals OWNER TO {owner_identifier}"
        owner_query = (
            "SELECT pg_get_userbyid(relowner) FROM pg_class AS relation "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'public' AND relation.relname = 'principals'"
        )
    else:
        drift_sql = (
            "ALTER FUNCTION public.lls_reject_audit_event_mutation() "
            f"OWNER TO {app_identifier}"
        )
        restore_sql = (
            "ALTER FUNCTION public.lls_reject_audit_event_mutation() "
            f"OWNER TO {owner_identifier}"
        )
        owner_query = (
            "SELECT pg_get_userbyid(proowner) FROM pg_proc AS procedure "
            "JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace "
            "WHERE namespace.nspname = 'public' "
            "AND procedure.proname = 'lls_reject_audit_event_mutation'"
        )

    _run_sql(database.owner_url, drift_sql)
    try:
        verifier = _run_verifier(database, check=False)
        assert verifier.returncode == 3
        assert "ownership" in verifier.stdout

        initialization = _run_role_script(database, check=False)
        assert initialization.returncode == 3
        assert "ownership preflight" in initialization.stdout

        with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
            assert connection.execute(owner_query).fetchone()[0] == database.app_user
    finally:
        _run_sql(database.owner_url, restore_sql)

    _run_role_script(database, verify=True)


def test_runtime_role_verifier_rejects_audit_function_body_drift_without_repair(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "CREATE OR REPLACE FUNCTION public.lls_reject_audit_event_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'audit body drift' USING ERRCODE = '55000'; END; "
        "$$",
    )
    try:
        verifier = _run_verifier(database, check=False)
        assert verifier.returncode == 3
        assert "runtime audit function guard verification failed" in verifier.stdout

        initialization = _run_role_script(database, verify=True, check=False)
        assert initialization.returncode == 3
        with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
            source = connection.execute(
                "SELECT prosrc FROM pg_proc AS procedure "
                "JOIN pg_namespace AS namespace ON namespace.oid = procedure.pronamespace "
                "WHERE namespace.nspname = 'public' "
                "AND procedure.proname = 'lls_reject_audit_event_mutation'"
            ).fetchone()[0]
        assert "audit body drift" in source
    finally:
        _run_sql(
            database.owner_url,
            "CREATE OR REPLACE FUNCTION public.lls_reject_audit_event_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = '55000'; END; "
            "$$",
        )

    _run_role_script(database, verify=True)


def test_runtime_role_verifier_rejects_audit_function_security_drift(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "ALTER FUNCTION public.lls_reject_audit_event_mutation() SECURITY DEFINER",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime audit function guard verification failed" in drift.stdout
    finally:
        _run_sql(
            database.owner_url,
            "ALTER FUNCTION public.lls_reject_audit_event_mutation() SECURITY INVOKER",
        )

    _run_verifier(database)


def test_runtime_role_verifier_rejects_extra_audit_trigger_without_init_removal(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "CREATE TRIGGER trg_audit_events_extra "
        "BEFORE INSERT ON public.audit_events FOR EACH ROW "
        "EXECUTE FUNCTION public.lls_reject_audit_event_mutation()",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime database guard verification failed" in drift.stdout

        initialization = _run_role_script(database, verify=True, check=False)
        assert initialization.returncode == 3
        with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
            assert connection.execute(
                "SELECT count(*) FROM pg_trigger AS trigger "
                "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                "WHERE relation.relname = 'audit_events' "
                "AND trigger.tgname = 'trg_audit_events_extra'"
            ).fetchone()[0] == 1
    finally:
        _run_sql(database.owner_url, "DROP TRIGGER trg_audit_events_extra ON public.audit_events")

    _run_verifier(database)


def test_runtime_role_verifier_rejects_wrong_audit_trigger_event_and_timing(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "DROP TRIGGER trg_audit_events_append_only ON public.audit_events; "
        "CREATE TRIGGER trg_audit_events_append_only "
        "AFTER UPDATE OR DELETE ON public.audit_events FOR EACH ROW "
        "EXECUTE FUNCTION public.lls_reject_audit_event_mutation()",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime database guard verification failed" in drift.stdout
    finally:
        _run_sql(
            database.owner_url,
            "DROP TRIGGER trg_audit_events_append_only ON public.audit_events; "
            "CREATE TRIGGER trg_audit_events_append_only "
            "BEFORE UPDATE OR DELETE ON public.audit_events FOR EACH ROW "
            "EXECUTE FUNCTION public.lls_reject_audit_event_mutation()",
        )

    _run_verifier(database)


def test_runtime_role_verifier_rejects_disabled_audit_trigger(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "ALTER TABLE public.audit_events DISABLE TRIGGER trg_audit_events_append_only",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime database guard verification failed" in drift.stdout
    finally:
        _run_sql(
            database.owner_url,
            "ALTER TABLE public.audit_events ENABLE TRIGGER trg_audit_events_append_only",
        )

    _run_verifier(database)


def test_runtime_role_verifier_rejects_allowlisted_function_overload(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    _run_sql(
        database.owner_url,
        "CREATE FUNCTION public.lls_reject_audit_event_mutation(integer) "
        "RETURNS integer LANGUAGE sql AS 'SELECT $1'",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime function catalog verification failed" in drift.stdout
    finally:
        _run_sql(
            database.owner_url,
            "DROP FUNCTION public.lls_reject_audit_event_mutation(integer)",
        )

    _run_verifier(database)


@pytest.mark.parametrize("grantee", ["app", "PUBLIC"])
def test_runtime_role_verifier_rejects_function_execute_drift(
    postgres_runtime_database: _RuntimeDatabase,
    grantee: str,
):
    database = postgres_runtime_database
    role = _quoted_identifier(database.app_user) if grantee == "app" else "PUBLIC"
    grant = (
        "GRANT EXECUTE ON FUNCTION public.lls_reject_audit_event_mutation() "
        f"TO {role}"
    )
    revoke = (
        "REVOKE EXECUTE ON FUNCTION public.lls_reject_audit_event_mutation() "
        f"FROM {role}"
    )
    _run_sql(database.owner_url, grant)
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime function ACL catalog verification failed" in drift.stdout
    finally:
        _run_sql(database.owner_url, revoke)

    _run_verifier(database)


@pytest.mark.parametrize(
    ("scope", "privilege", "object_type", "grantee"),
    [
        ("IN SCHEMA public ", "SELECT", "TABLES", "app"),
        ("", "USAGE", "SEQUENCES", "app"),
        ("", "EXECUTE", "FUNCTIONS", "PUBLIC"),
    ],
)
def test_runtime_role_verifier_rejects_default_acl_drift(
    postgres_runtime_database: _RuntimeDatabase,
    scope: str,
    privilege: str,
    object_type: str,
    grantee: str,
):
    database = postgres_runtime_database
    owner = _quoted_identifier(database.owner_url.username or "")
    role = _quoted_identifier(database.app_user) if grantee == "app" else "PUBLIC"
    _run_sql(
        database.owner_url,
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} {scope}"
        f"GRANT {privilege} ON {object_type} TO {role}",
    )

    drift = _run_verifier(database, check=False)
    assert drift.returncode == 3
    assert "runtime default ACL catalog verification failed" in drift.stdout

    _run_role_script(database, verify=True)


@pytest.mark.parametrize(
    ("privilege", "grantee"),
    [("CONNECT", "PUBLIC"), ("TEMPORARY", "app")],
)
def test_runtime_role_verifier_rejects_non_target_database_privilege_drift(
    postgres_runtime_database: _RuntimeDatabase,
    privilege: str,
    grantee: str,
):
    database = postgres_runtime_database
    role = _quoted_identifier(database.app_user) if grantee == "app" else "PUBLIC"
    _run_sql(
        database.owner_url,
        f"GRANT {privilege} ON DATABASE postgres TO {role}",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime database" in drift.stdout
    finally:
        _run_sql(
            database.owner_url,
            f"REVOKE {privilege} ON DATABASE postgres FROM {role}",
        )

    _run_verifier(database)


def test_runtime_role_cannot_connect_to_non_target_database(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    result = _run_sql(database.app_url.set(database="postgres"), "SELECT 1", check=False)

    assert result.returncode != 0
    assert "permission denied for database" in result.stderr


def test_runtime_role_verifier_rejects_maintain_privilege_when_supported(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    with psycopg.connect(_render_psycopg_url(database.owner_url)) as connection:
        if connection.info.server_version < 170000:
            pytest.skip("PostgreSQL does not support MAINTAIN")

    app_role = _quoted_identifier(database.app_user)
    _run_sql(
        database.owner_url,
        f"GRANT MAINTAIN ON TABLE public.principals TO {app_role}",
    )
    try:
        drift = _run_verifier(database, check=False)
        assert drift.returncode == 3
        assert "runtime relation ACL catalog verification failed" in drift.stdout
    finally:
        _run_sql(
            database.owner_url,
            f"REVOKE MAINTAIN ON TABLE public.principals FROM {app_role}",
        )

    _run_verifier(database)


def test_runtime_role_initialization_rejects_non_super_owner(
    postgres_runtime_database: _RuntimeDatabase,
):
    database = postgres_runtime_database
    token = uuid.uuid4().hex[:12]
    limited_owner = f"lls73_owner_{token}"
    limited_app = f"lls73_limited_app_{token}"
    limited_database_name = f"lls73_limited_{token}"
    owner_password = f"owner-{token}"
    app_password = f"app-{token}"
    maintenance_url = database.owner_url.set(database="postgres")

    with psycopg.connect(_render_psycopg_url(maintenance_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEDB CREATEROLE PASSWORD {}").format(
                sql.Identifier(limited_owner),
                sql.Literal(owner_password),
            )
        )
        connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(limited_database_name),
                sql.Identifier(limited_owner),
            )
        )

    limited_owner_url = database.owner_url.set(
        username=limited_owner,
        password=owner_password,
        database=limited_database_name,
    )
    limited_database = _RuntimeDatabase(
        owner_url=limited_owner_url,
        app_url=limited_owner_url.set(username=limited_app, password=app_password),
        app_user=limited_app,
        helper_role="",
    )
    try:
        result = _run_role_script(limited_database, check=False)
        assert result.returncode == 3
        assert "requires the dedicated cluster superuser" in result.stdout
    finally:
        with psycopg.connect(_render_psycopg_url(maintenance_url), autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (limited_database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(limited_database_name)
                )
            )
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(limited_app)))
            connection.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(limited_owner))
            )


def test_runtime_role_script_rejects_shared_owner_identity_or_password(tmp_path: Path):
    bin_dir, args_file = _fake_psql(tmp_path)
    base_env = _role_script_env(bin_dir, args_file)

    same_role = subprocess.run(
        [str(ROLE_SCRIPT)],
        env={**base_env, "SCAFFOLD_POSTGRES_APP_USER": "scaffold_owner"},
        text=True,
        capture_output=True,
    )
    same_password = subprocess.run(
        [str(ROLE_SCRIPT)],
        env={**base_env, "SCAFFOLD_POSTGRES_APP_PASSWORD": "owner-test-password"},
        text=True,
        capture_output=True,
    )

    assert same_role.returncode == 2
    assert "must be different" in same_role.stderr
    assert same_password.returncode == 2
    assert "must be different" in same_password.stderr
    assert not args_file.exists()
