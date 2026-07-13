from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_stack(tmp_path: Path, values: dict[str, str], *args: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    root = tmp_path / "repo"
    scripts_dir = root / "scripts"
    bin_dir = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    bin_dir.mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "stack", scripts_dir / "stack")
    (root / ".env").write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )
    (root / ".env.example").write_text("", encoding="utf-8")
    docker_log = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'PANEL_BIND_HOST=%s;MLFLOW_TRACKING_URI=%s|%s\\n' "
        "\"${PANEL_BIND_HOST:-}\" \"${MLFLOW_TRACKING_URI:-}\" \"$*\" >> \"$DOCKER_LOG\"\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = os.environ.copy()
    for name in list(env):
        if name.startswith("LLS_") or name in {"PANEL_BIND_HOST", "MLFLOW_TRACKING_URI"}:
            env.pop(name)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DOCKER_LOG"] = str(docker_log)
    result = subprocess.run(
        [str(scripts_dir / "stack"), *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    lines = docker_log.read_text(encoding="utf-8").splitlines() if docker_log.exists() else []
    return result, lines


@pytest.mark.parametrize("command", ["up", "restart"])
def test_stack_rejects_non_loopback_basic_dev_for_config_applying_commands(tmp_path: Path, command: str):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "PANEL_BIND_HOST": "0.0.0.0",
            "LLS_TASK_SOURCE": "local",
        },
        command,
    )

    assert result.returncode == 2
    assert "basic_dev 模式只允许 PANEL_BIND_HOST" in result.stderr
    assert calls == ["PANEL_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_stack_accepts_basic_dev_loopback_bind_hosts(tmp_path: Path, host: str):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "PANEL_BIND_HOST": host,
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": "owner-password",
            "SCAFFOLD_POSTGRES_APP_PASSWORD": "app-password",
            "LLS_TASK_SOURCE": "local",
        },
        "restart",
    )

    assert result.returncode == 0
    expected_host = "127.0.0.1" if host == "localhost" else host
    assert calls[-1].startswith(f"PANEL_BIND_HOST={expected_host};")
    assert "up -d --force-recreate" in calls[-1]


@pytest.mark.parametrize("command", ["up", "restart"])
@pytest.mark.parametrize(
    ("owner_password", "app_password", "message"),
    [
        ("", "app-password", "必须设置 SCAFFOLD_POSTGRES_OWNER_PASSWORD"),
        ("owner-password", "", "必须设置 SCAFFOLD_POSTGRES_OWNER_PASSWORD"),
        ("shared-password", "shared-password", "必须使用不同密码"),
    ],
)
def test_stack_validates_database_passwords_for_up_and_restart(
    tmp_path: Path,
    command: str,
    owner_password: str,
    app_password: str,
    message: str,
):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "PANEL_BIND_HOST": "127.0.0.1",
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": owner_password,
            "SCAFFOLD_POSTGRES_APP_PASSWORD": app_password,
            "LLS_TASK_SOURCE": "local",
        },
        command,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert calls == ["PANEL_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


@pytest.mark.parametrize("command", ["up", "restart"])
def test_stack_validates_cloudflare_configuration_for_up_and_restart(tmp_path: Path, command: str):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_PANEL_AUTH_MODE": "cloudflare_access",
            "LLS_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_TASK_SOURCE": "local",
        },
        command,
    )

    assert result.returncode == 2
    assert "必须设置 LLS_CF_ACCESS_ISSUER 和 LLS_CF_ACCESS_AUD" in result.stderr
    assert calls == ["PANEL_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


@pytest.mark.parametrize("command", ["up", "restart"])
def test_stack_validates_mcp_tokens_for_up_and_restart(tmp_path: Path, command: str):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "PANEL_BIND_HOST": "127.0.0.1",
            "LLS_TASK_SOURCE": "local",
        },
        command,
        "--mcp",
    )

    assert result.returncode == 2
    assert "启用 MCP 前必须设置彼此不同" in result.stderr
    assert calls == ["PANEL_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


def test_stack_restart_force_recreates_services_and_preserves_mlflow_profile(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_PANEL_AUTH_MODE": "cloudflare_access",
            "LLS_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_CF_ACCESS_AUD": "configured-audience",
            "PANEL_BIND_HOST": "0.0.0.0",
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": "owner-password",
            "SCAFFOLD_POSTGRES_APP_PASSWORD": "app-password",
            "LLS_TASK_SOURCE": "local",
        },
        "restart",
        "--mlflow",
    )

    assert result.returncode == 0
    deploy_call = calls[-1]
    assert deploy_call.startswith("PANEL_BIND_HOST=0.0.0.0;MLFLOW_TRACKING_URI=http://mlflow:5000|")
    assert "compose -f docker-compose.yml --profile mlflow up -d --force-recreate" in deploy_call
    assert deploy_call.endswith(
        "scaffold-postgres db-role-init migrate panel argilla argilla-worker "
        "argilla-postgres elasticsearch redis mlflow"
    )
    assert " restart " not in deploy_call
