from __future__ import annotations

import re
from typing import Any


REDACTED = "[REDACTED]"

_SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "key",
    "password",
    "passwd",
    "private_key",
    "secret",
    "secret_key",
    "token",
}
_NON_SECRET_KEYS = {"idempotency_key"}
_SECRET_TEXT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|client[_-]?secret|password|passwd|private[_-]?key|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def is_sensitive_key(value: Any) -> bool:
    key = _normalized_key(value)
    if not key or key in _NON_SECRET_KEYS:
        return False
    if key in _SENSITIVE_EXACT_KEYS:
        return True
    return (
        key.endswith("_api_key")
        or key.endswith("_password")
        or key.endswith("_passwd")
        or key.endswith("_token")
        or key.endswith("_authorization")
        or key.endswith("_secret")
        or key.endswith("_secret_key")
        or key.endswith("_private_key")
    )


def sensitive_paths(value: Any, *, prefix: str = "params") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_sensitive_key(key):
                paths.append(path)
            else:
                paths.extend(sensitive_paths(item, prefix=path))
    elif isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            paths.extend(sensitive_paths(item, prefix=f"{prefix}[{index}]"))
    return paths


def collect_sensitive_values(value: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if is_sensitive_key(key):
                if isinstance(item, (list, tuple, set)):
                    values.update(str(entry) for entry in item if str(entry))
                elif item not in (None, ""):
                    values.add(str(item))
            else:
                values.update(collect_sensitive_values(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            values.update(collect_sensitive_values(item))
    return values


def redact_text(value: Any, *, secrets: set[str] | None = None) -> str:
    text = str(value)
    for secret in sorted(secrets or set(), key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)
    text = _SECRET_TEXT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
    return _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)


def redact_sensitive(value: Any, *, secrets: set[str] | None = None) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED if is_sensitive_key(key) else redact_sensitive(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item, secrets=secrets) for item in value]
    if isinstance(value, set):
        return [redact_sensitive(item, secrets=secrets) for item in sorted(value, key=str)]
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    return value
