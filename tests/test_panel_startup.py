from __future__ import annotations

import socket

import pytest

from llm_labeling_scaffold import cli, panel


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.42.0.1", "localhost", "localhost.", "::1", "::ffff:127.0.0.1"],
)
def test_host_deployment_accepts_ipv4_and_ipv6_loopback(host: str):
    panel._validate_panel_startup(
        host=host,
        auth_mode="basic_dev",
        deployment_mode="host",
        containerized=False,
    )


def test_localhost_bind_is_normalized_without_dns_resolution():
    assert panel._normalize_bind_host("localhost") == "127.0.0.1"
    assert panel._normalize_bind_host("LOCALHOST.") == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "panel", ""])
def test_host_deployment_rejects_non_loopback_basic_dev(host: str):
    with pytest.raises(ValueError, match="只允许绑定 IPv4/IPv6 loopback"):
        panel._validate_panel_startup(
            host=host,
            auth_mode="basic_dev",
            deployment_mode="host",
            containerized=False,
        )


def test_host_deployment_rejects_non_loopback_cloudflare_access():
    with pytest.raises(ValueError, match="只允许绑定 IPv4/IPv6 loopback"):
        panel._validate_panel_startup(
            host="0.0.0.0",
            auth_mode="cloudflare_access",
            deployment_mode="host",
            containerized=False,
        )


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_docker_loopback_allows_container_wildcard_bind(host: str):
    panel._validate_panel_startup(
        host=host,
        auth_mode="basic_dev",
        deployment_mode="docker_loopback",
        containerized=True,
    )


def test_docker_tunnel_requires_cloudflare_access():
    with pytest.raises(ValueError, match="只允许 cloudflare_access"):
        panel._validate_panel_startup(
            host="0.0.0.0",
            auth_mode="basic_dev",
            deployment_mode="docker_tunnel",
            containerized=True,
        )

    panel._validate_panel_startup(
        host="0.0.0.0",
        auth_mode="cloudflare_access",
        deployment_mode="docker_tunnel",
        containerized=True,
    )


def test_docker_modes_require_container_runtime_and_wildcard_bind():
    with pytest.raises(ValueError, match="只能在容器内使用"):
        panel._validate_panel_startup(
            host="0.0.0.0",
            auth_mode="basic_dev",
            deployment_mode="docker_loopback",
            containerized=False,
        )
    with pytest.raises(ValueError, match="要求容器进程绑定"):
        panel._validate_panel_startup(
            host="127.0.0.1",
            auth_mode="basic_dev",
            deployment_mode="docker_loopback",
            containerized=True,
        )


def test_ipv6_bind_uses_ipv6_http_server():
    assert panel._panel_server_type("::1").address_family == socket.AF_INET6
    assert panel._panel_server_type("::").address_family == socket.AF_INET6
    assert panel._panel_server_type("127.0.0.1").address_family == socket.AF_INET


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_direct_cli_rejects_basic_dev_wildcard_bind(monkeypatch: pytest.MonkeyPatch, host: str):
    monkeypatch.delenv("LLS_PANEL_DEPLOYMENT_MODE", raising=False)

    with pytest.raises(SystemExit, match="只允许绑定 IPv4/IPv6 loopback"):
        cli.main(
            [
                "panel",
                "--auth-mode",
                "basic_dev",
                "--password",
                "secret",
                "--host",
                host,
            ]
        )
