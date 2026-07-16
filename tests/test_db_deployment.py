from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE_SCRIPT = ROOT / "docker" / "postgres" / "init-runtime-role.sh"
ROLE_SQL = ROOT / "docker" / "postgres" / "init-runtime-role.sql"
LOOPBACK_COMPOSE = ROOT / "docker-compose.loopback.yml"


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


def test_runtime_role_script_defaults_sql_to_its_own_directory(tmp_path: Path):
    bin_dir, args_file = _fake_psql(tmp_path)

    subprocess.run(
        [str(ROLE_SCRIPT)],
        cwd=tmp_path,
        env=_role_script_env(bin_dir, args_file),
        check=True,
    )

    args = args_file.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--file") + 1] == str(ROLE_SQL)


def test_compose_role_initialization_paths_are_explicit():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    panel = services["panel"]
    panel_environment = panel["environment"]
    assert "ports" not in panel
    assert panel["expose"] == ["8765"]
    assert panel["networks"] == ["lls"]
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
