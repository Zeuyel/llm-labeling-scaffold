from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib import error, parse, request


DEFAULT_TASK_ID = "patent_boundary_v0_1"
DEFAULT_IMPORT_ID = "patent_boundary_manual_seed_500_2026_06_27"
DISCOVERY_ENDPOINTS = (
    ("health", "GET", "/api/health"),
    ("version", "GET", "/api/version"),
    ("capabilities", "GET", "/api/capabilities"),
    ("settings_public", "GET", "/api/settings/public"),
)

SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|pwd|api[_-]?key|apikey|authorization|cookie|"
    r"rclone[_-]?config|database[_-]?url)",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PWD|API_KEY|APIKEY|DATABASE_URL)"
    r"|RCLONE_CONFIG(?:_PATH)?|ARGILLA_API_KEY|SECRET_PATH"
    r")\s*=\s*([^\s,;]+)"
)
SENSITIVE_PARAM_RE = re.compile(
    r"(?i)(^|[?&\s,;])"
    r"([A-Z0-9_.-]*(?:token|secret|password|passwd|pwd|api[_-]?key|apikey)[A-Z0-9_.-]*)"
    r"(=)([^&#\s,;]+)"
)
AUTH_RE = re.compile(r"(?i)\b(authorization:\s*(?:bearer|basic)\s+)[^\s,;]+")
URL_PASSWORD_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@\s/]+(@)", re.IGNORECASE)
RCLONE_PATH_RE = re.compile(r"(?i)(?:[A-Za-z]:)?[^\s,;]*rclone[^\s,;]*\.conf")
SECRET_PATH_RE = re.compile(r"(?i)(?:/[^,\s;]+)*(?:/run/secrets|/var/run/secrets|/secrets)/[^,\s;]+")


@dataclass(frozen=True)
class SmokeConfig:
    server_url: str
    token: str | None = None
    basic_user: str | None = None
    basic_password: str | None = None
    task_id: str = DEFAULT_TASK_ID
    import_id: str = DEFAULT_IMPORT_ID
    timeout: float = 10.0


@dataclass(frozen=True)
class SmokeResponse:
    status: int
    body: Any
    text: str = ""


Transport = Callable[[str, str, dict[str, str], bytes | None, float], SmokeResponse]


def config_from_env(
    *,
    server_url: str | None = None,
    token: str | None = None,
    token_env: str = "LLS_SMOKE_TOKEN",
    basic_user: str | None = None,
    basic_password: str | None = None,
    basic_password_env: str = "LLS_SMOKE_BASIC_PASSWORD",
    task_id: str = DEFAULT_TASK_ID,
    import_id: str = DEFAULT_IMPORT_ID,
    timeout: float = 10.0,
) -> SmokeConfig:
    resolved_server_url = server_url or os.environ.get("LLS_SMOKE_SERVER_URL") or os.environ.get("LLS_PANEL_URL")
    if not resolved_server_url:
        raise ValueError("requires --server-url or LLS_SMOKE_SERVER_URL")

    resolved_token = token if token is not None else os.environ.get(token_env)
    explicit_basic = (
        basic_user is not None
        or basic_password is not None
        or bool(os.environ.get("LLS_SMOKE_BASIC_USER"))
        or bool(os.environ.get(basic_password_env))
    )
    if resolved_token:
        if explicit_basic:
            raise ValueError("use either token auth or basic auth, not both")
        return SmokeConfig(
            server_url=resolved_server_url,
            token=resolved_token,
            task_id=task_id,
            import_id=import_id,
            timeout=timeout,
        )

    resolved_password = basic_password if basic_password is not None else (
        os.environ.get(basic_password_env) or os.environ.get("LLS_PANEL_PASSWORD")
    )
    resolved_user = basic_user if basic_user is not None else (
        os.environ.get("LLS_SMOKE_BASIC_USER") or os.environ.get("LLS_PANEL_USER") or ("admin" if resolved_password else None)
    )

    if bool(resolved_user) ^ bool(resolved_password):
        raise ValueError("basic auth requires both user and password")

    return SmokeConfig(
        server_url=resolved_server_url,
        token=resolved_token,
        basic_user=resolved_user,
        basic_password=resolved_password,
        task_id=task_id,
        import_id=import_id,
        timeout=timeout,
    )


def auth_type(config: SmokeConfig) -> str:
    if config.token:
        return "token"
    if config.basic_user is not None and config.basic_password is not None:
        return "basic"
    return "none"


def redact_text(value: str) -> str:
    out = URL_PASSWORD_RE.sub(r"\1<redacted>\2", value)
    out = AUTH_RE.sub(r"\1<redacted>", out)
    out = SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", out)
    out = SENSITIVE_PARAM_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}<redacted>", out)
    out = RCLONE_PATH_RE.sub("<redacted-rclone-config>", out)
    out = SECRET_PATH_RE.sub("<redacted-secret-path>", out)
    return out


def redact_secrets(value: Any, *, key: str | None = None) -> Any:
    if key and SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact_secrets(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class SmokeClient:
    def __init__(self, config: SmokeConfig, transport: Transport | None = None):
        self.config = config
        self.transport = transport or _urllib_transport

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> SmokeResponse:
        url = parse.urljoin(self.config.server_url.rstrip("/") + "/", path.lstrip("/"))
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        elif self.config.basic_user is not None and self.config.basic_password is not None:
            raw = f"{self.config.basic_user}:{self.config.basic_password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return self.transport(method, url, headers, data, self.config.timeout)


def _decode_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:500]}


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> SmokeResponse:
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return SmokeResponse(status=response.status, body=_decode_json(text), text=text)
    except error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        return SmokeResponse(status=exc.code, body=_decode_json(text), text=text)


def run_smoke(config: SmokeConfig, transport: Transport | None = None) -> dict[str, Any]:
    client = SmokeClient(config, transport=transport)
    checks: list[dict[str, Any]] = []
    capabilities: dict[str, Any] | None = None

    for name, method, path in DISCOVERY_ENDPOINTS:
        check = _run_json_check(client, name, method, path)
        checks.append(check)
        if name == "capabilities" and check.get("response"):
            capabilities = check["response"]

    task_path = f"/api/tasks/{parse.quote(config.task_id, safe='')}/check"
    checks.append(
        _run_json_check(
            client,
            "task_check",
            "POST",
            task_path,
            body={},
            expect_ok_field=True,
            extra={"task_id": config.task_id},
        )
    )
    checks.append(_run_import_dry_run_check(client, capabilities))

    summary = {
        "service": "llm-labeling-scaffold",
        "server_url": config.server_url,
        "auth": auth_type(config),
        "task_id": config.task_id,
        "import_id": config.import_id,
        "ok": all(item.get("status") == "ok" for item in checks),
        "checks": [_compact_check(item) for item in checks],
    }
    return redact_secrets(summary)


def _run_json_check(
    client: SmokeClient,
    name: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    expect_ok_field: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = {"name": name, "method": method, "path": path, **(extra or {})}
    try:
        response = client.request(method, path, body=body)
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "error", "error": f"{exc.__class__.__name__}: {exc}"}

    check = {**base, "http_status": response.status, "response": response.body}
    if response.status == 404:
        return {**check, "status": "missing", "message": _message_from_body(response.body)}
    if response.status in {405, 501}:
        return {**check, "status": "not_supported", "message": _message_from_body(response.body)}
    if not 200 <= response.status < 300:
        return {**check, "status": "failed", "message": _message_from_body(response.body)}
    if expect_ok_field and isinstance(response.body, dict) and response.body.get("ok") is False:
        return {**check, "status": "failed", "message": _message_from_body(response.body)}
    return {**check, "status": "ok"}


def _run_import_dry_run_check(client: SmokeClient, capabilities: dict[str, Any] | None) -> dict[str, Any]:
    endpoint = _find_import_dry_run_endpoint(capabilities)
    fallback = {
        "name": "import_dry_run",
        "method": "POST",
        "path": "/api/import/data_lake",
        "task_id": client.config.task_id,
        "import_id": client.config.import_id,
    }
    if endpoint is None:
        return {
            **fallback,
            "status": "not_supported",
            "message": "capabilities did not advertise a side-effect-free import dry-run endpoint",
        }

    path = _fill_import_endpoint_path(endpoint["path"], client.config.task_id, client.config.import_id)
    payload = {"task_id": client.config.task_id, "import_id": client.config.import_id, "dry_run": True}
    return _run_json_check(
        client,
        "import_dry_run",
        endpoint["method"],
        path,
        body=payload,
        expect_ok_field=True,
        extra={"task_id": client.config.task_id, "import_id": client.config.import_id},
    )


def _find_import_dry_run_endpoint(capabilities: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(capabilities, dict):
        return None
    endpoints = capabilities.get("endpoints")
    if not isinstance(endpoints, list):
        return None
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        method = str(endpoint.get("method") or "").upper()
        path = str(endpoint.get("path") or "")
        action = str(endpoint.get("action") or "")
        schema = endpoint.get("request_schema") if isinstance(endpoint.get("request_schema"), dict) else {}
        properties = schema.get("properties") if isinstance(schema, dict) else {}
        dry_run_declared = (
            "dry_run" in properties
            or "dry-run" in path
            or "dry_run" in path
            or action.endswith("_dry_run")
            or action == "import_dry_run"
        )
        import_like = "import" in action or "/import" in path
        if method == "POST" and path and import_like and dry_run_declared and endpoint.get("side_effects") is False:
            return {"method": method, "path": path}
    return None


def _fill_import_endpoint_path(path: str, task_id: str, import_id: str) -> str:
    return (
        path.replace("{task_id}", parse.quote(task_id, safe=""))
        .replace("{import_id}", parse.quote(import_id, safe=""))
    )


def _message_from_body(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "detail"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        if body.get("errors"):
            return json.dumps(body["errors"], ensure_ascii=False)[:500]
    if isinstance(body, str):
        return body[:500]
    return ""


def _compact_check(check: dict[str, Any]) -> dict[str, Any]:
    response = check.get("response")
    out = {
        "name": check.get("name"),
        "status": check.get("status"),
        "method": check.get("method"),
        "path": check.get("path"),
    }
    for key in ("http_status", "task_id", "import_id", "message", "error"):
        if key in check and check[key] not in (None, ""):
            out[key] = check[key]
    if isinstance(response, dict):
        out["response_keys"] = sorted(str(key) for key in response.keys())
        if check.get("name") == "version":
            for key in ("version", "api_contract_version"):
                if key in response:
                    out[key] = response[key]
        elif check.get("name") == "capabilities":
            endpoints = response.get("endpoints")
            if isinstance(endpoints, list):
                out["endpoint_count"] = len(endpoints)
        elif check.get("name") == "task_check":
            out["task_ok"] = response.get("ok")
            if isinstance(response.get("checks"), list):
                out["task_check_count"] = len(response["checks"])
            if isinstance(response.get("warnings"), list):
                out["warning_count"] = len(response["warnings"])
            if isinstance(response.get("errors"), list):
                out["error_count"] = len(response["errors"])
        elif check.get("name") == "import_dry_run":
            if "ok" in response:
                out["ok"] = response["ok"]
            if "dry_run" in response:
                dry_run = response["dry_run"]
                out["dry_run"] = True if isinstance(dry_run, dict) else bool(dry_run)
                if isinstance(dry_run, dict):
                    out["dry_run_keys"] = sorted(str(key) for key in dry_run.keys())
                    _copy_summary_fields(dry_run, out, prefix="dry_run")
            if isinstance(response.get("job"), dict):
                out["job"] = {
                    key: response["job"].get(key)
                    for key in ("id", "kind", "status")
                    if response["job"].get(key) not in (None, "")
                }
            if isinstance(response.get("result"), dict):
                out["result_keys"] = sorted(str(key) for key in response["result"].keys())
                _copy_summary_fields(response["result"], out, prefix="result")
    return redact_secrets(out)


def _copy_summary_fields(source: dict[str, Any], target: dict[str, Any], *, prefix: str) -> None:
    for key in ("import_id", "validation_ok", "valid", "ok", "would_import"):
        value = source.get(key)
        if value not in (None, "") and isinstance(value, (str, int, float, bool)):
            target[f"{prefix}_{key}"] = value


def render_summary(summary: dict[str, Any], output_format: str = "json") -> str:
    safe_summary = redact_secrets(summary)
    if output_format == "json":
        return json.dumps(safe_summary, ensure_ascii=False, indent=2)
    if output_format != "markdown":
        raise ValueError(f"unknown summary format: {output_format}")
    return _render_markdown(safe_summary)


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Scaffold SaaS smoke summary",
        "",
        f"- Overall: {'ok' if summary.get('ok') else 'failed'}",
        f"- Server: {summary.get('server_url', '')}",
        f"- Auth: {summary.get('auth', 'none')}",
        f"- Task: {summary.get('task_id', '')}",
        f"- Import: {summary.get('import_id', '')}",
        "",
        "| Check | Status | HTTP | Path | Note |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in summary.get("checks", []):
        note = check.get("message") or check.get("error") or ""
        if check.get("name") == "capabilities" and check.get("endpoint_count") is not None:
            note = f"{check['endpoint_count']} endpoints"
        if check.get("name") == "task_check" and check.get("error_count") is not None:
            note = f"errors={check.get('error_count', 0)}, warnings={check.get('warning_count', 0)}"
        lines.append(
            "| "
            + " | ".join(
                _md_cell(str(value))
                for value in (
                    check.get("name", ""),
                    check.get("status", ""),
                    check.get("http_status", ""),
                    check.get("path", ""),
                    note,
                )
            )
            + " |"
        )
    return "\n".join(lines)


def _md_cell(value: str) -> str:
    return redact_text(value).replace("|", "\\|").replace("\n", " ")
