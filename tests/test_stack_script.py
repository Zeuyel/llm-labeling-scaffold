from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


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
        "printf 'MCP_BIND_HOST=%s;MLFLOW_TRACKING_URI=%s|%s\\n' "
        '"${MCP_BIND_HOST:-}" "${MLFLOW_TRACKING_URI:-}" "$*" >> "$DOCKER_LOG"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = os.environ.copy()
    for name in list(env):
        if name.startswith("LLS_") or name in {"PANEL_BIND_HOST", "MCP_BIND_HOST", "MLFLOW_TRACKING_URI"}:
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


def test_stack_requires_explicit_deployment_mode(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "LLS_TASK_SOURCE": "local",
        },
        "up",
    )

    assert result.returncode == 2
    assert "LLS_DEPLOYMENT_MODE 只能是 loopback 或 tunnel" in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


@pytest.mark.parametrize("command", ["up", "restart"])
def test_stack_rejects_basic_dev_tunnel_mode(tmp_path: Path, command: str):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "tunnel",
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "LLS_TASK_SOURCE": "local",
        },
        command,
    )

    assert result.returncode == 2
    assert "basic_dev 模式只能使用 LLS_DEPLOYMENT_MODE=loopback" in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


def test_stack_accepts_basic_dev_loopback_mode(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "loopback",
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": "owner-password",
            "SCAFFOLD_POSTGRES_APP_PASSWORD": "app-password",
            "LLS_TASK_SOURCE": "local",
        },
        "restart",
    )

    assert result.returncode == 0
    assert "compose -f docker-compose.yml -f docker-compose.loopback.yml" in calls[-1]
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
            "LLS_DEPLOYMENT_MODE": "loopback",
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": owner_password,
            "SCAFFOLD_POSTGRES_APP_PASSWORD": app_password,
            "LLS_TASK_SOURCE": "local",
        },
        command,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


@pytest.mark.parametrize("command", ["up", "restart"])
def test_stack_validates_cloudflare_configuration_for_up_and_restart(tmp_path: Path, command: str):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "tunnel",
            "LLS_PANEL_AUTH_MODE": "cloudflare_access",
            "LLS_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_TASK_SOURCE": "local",
        },
        command,
    )

    assert result.returncode == 2
    assert "必须设置 LLS_CF_ACCESS_ISSUER 和 LLS_CF_ACCESS_AUD" in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


@pytest.mark.parametrize("command", ["up", "restart"])
def test_stack_validates_static_dev_mcp_tokens_for_up_and_restart(tmp_path: Path, command: str):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "loopback",
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "LLS_MCP_AUTH_MODE": "static_dev",
            "LLS_TASK_SOURCE": "local",
        },
        command,
        "--mcp",
    )

    assert result.returncode == 2
    assert "LLS_MCP_INTERNAL_TOKEN" in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


def test_stack_static_dev_requires_separate_external_bearer(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "loopback",
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "LLS_MCP_AUTH_MODE": "static_dev",
            "LLS_MCP_INTERNAL_TOKEN": "internal-token-0123456789-abcdef-012",
            "LLS_TASK_SOURCE": "local",
        },
        "up",
        "--mcp",
    )

    assert result.returncode == 2
    assert "LLS_MCP_BEARER_TOKEN" in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


def test_stack_managed_mcp_does_not_fall_back_to_static_bearer(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "loopback",
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "LLS_MCP_BEARER_TOKEN": "legacy-static-token-0123456789-abcdef",
            "LLS_MCP_INTERNAL_TOKEN": "internal-token-0123456789-abcdef-012",
            "LLS_TASK_SOURCE": "local",
        },
        "up",
        "--mcp",
    )

    assert result.returncode == 2
    assert "LLS_MCP_CF_ACCESS_ISSUER" in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


def test_stack_rejects_reused_panel_and_mcp_audience(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "tunnel",
            "LLS_PANEL_AUTH_MODE": "cloudflare_access",
            "LLS_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_CF_ACCESS_AUD": "shared-audience",
            "LLS_MCP_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_MCP_CF_ACCESS_AUD": "shared-audience",
            "LLS_MCP_INTERNAL_TOKEN": "internal-token-0123456789-abcdef-012",
            "LLS_TASK_SOURCE": "local",
        },
        "restart",
        "--mcp",
    )

    assert result.returncode == 2
    assert "不同的 Access application AUD" in result.stderr
    assert "shared-audience" not in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


@pytest.mark.parametrize("host", ["0.0.0.0", "::1"])
@pytest.mark.parametrize("mode", ["cloudflare_access", "static_dev"])
def test_stack_rejects_unsupported_mcp_host_publish(tmp_path: Path, mode: str, host: str):
    values = {
        "LLS_DEPLOYMENT_MODE": "loopback",
        "LLS_PANEL_AUTH_MODE": "basic_dev",
        "LLS_PANEL_PASSWORD": "secret",
        "MCP_BIND_HOST": host,
        "LLS_MCP_AUTH_MODE": mode,
        "LLS_MCP_INTERNAL_TOKEN": "internal-token-0123456789-abcdef-012",
        "LLS_TASK_SOURCE": "local",
    }
    if mode == "cloudflare_access":
        values.update(
            {
                "LLS_MCP_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
                "LLS_MCP_CF_ACCESS_AUD": "mcp-audience",
            }
        )
    else:
        values["LLS_MCP_BEARER_TOKEN"] = "static-token-0123456789-abcdef-0123"

    result, calls = _run_stack(tmp_path, values, "up", "--mcp")

    assert result.returncode == 2
    assert "MCP_BIND_HOST" in result.stderr
    assert calls == ["MCP_BIND_HOST=;MLFLOW_TRACKING_URI=|compose version"]


def test_stack_accepts_managed_mcp_without_static_bearer(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "tunnel",
            "LLS_PANEL_AUTH_MODE": "cloudflare_access",
            "LLS_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_CF_ACCESS_AUD": "panel-audience",
            "LLS_MCP_AUTH_MODE": "cloudflare_access",
            "LLS_MCP_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_MCP_CF_ACCESS_AUD": "mcp-audience",
            "LLS_MCP_INTERNAL_TOKEN": "internal-token-0123456789-abcdef-012",
            "MCP_BIND_HOST": "localhost",
            "CLOUDFLARE_TUNNEL_TOKEN": "tunnel-token-0123456789-abcdef-0123456789",
            "ARGILLA_USERNAME": "argilla-owner",
            "ARGILLA_PASSWORD": "argilla-password-0123456789",
            "ARGILLA_API_KEY": "argilla-api-key-0123456789",
            "ARGILLA_POSTGRES_PASSWORD": "argilla-db-password-0123456789",
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": "owner-password",
            "SCAFFOLD_POSTGRES_APP_PASSWORD": "app-password",
            "LLS_TASK_SOURCE": "local",
        },
        "restart",
        "--mcp",
    )

    assert result.returncode == 0
    deploy_call = calls[-1]
    assert deploy_call.startswith("MCP_BIND_HOST=127.0.0.1;MLFLOW_TRACKING_URI=|")
    assert "compose -f docker-compose.yml -f docker-compose.production.yml -f docker-compose.tunnel.yml --profile mcp up -d --force-recreate" in deploy_call
    assert deploy_call.endswith("redis cloudflared mcp")


def test_stack_cloudflare_loopback_mode_uses_fixed_override(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "loopback",
            "LLS_PANEL_AUTH_MODE": "cloudflare_access",
            "LLS_CF_ACCESS_ISSUER": "https://team.cloudflareaccess.com",
            "LLS_CF_ACCESS_AUD": "configured-audience",
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": "owner-password",
            "SCAFFOLD_POSTGRES_APP_PASSWORD": "app-password",
            "LLS_TASK_SOURCE": "local",
        },
        "restart",
    )

    assert result.returncode == 0
    assert "compose -f docker-compose.yml -f docker-compose.loopback.yml" in calls[-1]


def test_stack_uses_published_ghcr_image_without_local_build(tmp_path: Path):
    result, calls = _run_stack(
        tmp_path,
        {
            "LLS_DEPLOYMENT_MODE": "loopback",
            "LLS_PANEL_AUTH_MODE": "basic_dev",
            "LLS_PANEL_PASSWORD": "secret",
            "PANEL_IMAGE": "ghcr.io/zeuyel/llm-labeling-scaffold/panel:sha-test",
            "SCAFFOLD_POSTGRES_OWNER_PASSWORD": "owner-password",
            "SCAFFOLD_POSTGRES_APP_PASSWORD": "app-password",
            "LLS_TASK_SOURCE": "local",
        },
        "up",
    )

    assert result.returncode == 0
    assert "pull panel" in calls[-2]
    assert "up -d --no-build" in calls[-1]
    assert "--build" not in calls[-1]


def test_compose_mcp_managed_oauth_contract():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    mcp = compose["services"]["mcp"]
    environment = set(mcp["environment"])

    assert mcp["ports"] == ["${MCP_BIND_HOST:-127.0.0.1}:${MCP_PORT:-8766}:8766"]
    assert "LLS_MCP_AUTH_MODE=${LLS_MCP_AUTH_MODE:-cloudflare_access}" in environment
    assert "LLS_MCP_CF_ACCESS_ISSUER=${LLS_MCP_CF_ACCESS_ISSUER:-}" in environment
    assert "LLS_MCP_CF_ACCESS_AUD=${LLS_MCP_CF_ACCESS_AUD:-}" in environment
    assert "LLS_CF_ACCESS_AUD=${LLS_CF_ACCESS_AUD:-}" in environment
    assert "LLS_MCP_INTERNAL_TOKEN=${LLS_MCP_INTERNAL_TOKEN:-}" in environment
    assert "--published-host" in mcp["command"]
    published_index = mcp["command"].index("--published-host")
    assert mcp["command"][published_index + 1] == "${MCP_BIND_HOST:-127.0.0.1}"


def test_production_tunnel_compose_keeps_services_internal():
    base = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    production = yaml.safe_load((REPO_ROOT / "docker-compose.production.yml").read_text(encoding="utf-8"))
    tunnel = yaml.safe_load((REPO_ROOT / "docker-compose.tunnel.yml").read_text(encoding="utf-8"))

    assert base["services"]["argilla"]["ports"] == [
        "${ARGILLA_BIND_HOST:-127.0.0.1}:${ARGILLA_PORT:-6900}:6900"
    ]
    assert production["services"]["panel"]["environment"]["LLS_DEPLOYMENT_MODE"] == "tunnel"
    assert production["services"]["panel"]["environment"]["LLS_PANEL_AUTH_MODE"] == "cloudflare_access"
    assert "ports" not in tunnel["services"]["cloudflared"]
    assert tunnel["services"]["cloudflared"]["environment"]["TUNNEL_TOKEN"].startswith(
        "${CLOUDFLARE_TUNNEL_TOKEN:?"
    )
