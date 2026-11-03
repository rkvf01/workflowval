"""Remove secrets from anything written to logs, reports or generated code (NFR-7)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "[REDACTED]"
_MIN_ENV_SECRET_LENGTH = 8

_SECRET_KEY = re.compile(
    r"(^|[_-])(token|api[_-]?key|secret|password|passwd|authorization|cookie|credentials?)$",
    re.IGNORECASE,
)
_SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$")
_PATTERNS = (
    re.compile(r"(?i)\b(authorization|proxy-authorization)(\"?\s*[:=]\s*\"?)[^\"'\r\n]+"),
    re.compile(r"(?i)\b(set-cookie|cookie)(\"?\s*[:=]\s*\"?)[^\"'\r\n]+"),
    re.compile(
        r"(?i)\b(x-api-key|api[_-]?key|access_token|token)(\"?\s*[:=]\s*\"?)"
        r"(?![\[{]|null\b|true\b|false\b)[^\s\"'&,}]+"
    ),
)
_BARE_TOKENS = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)


def _env_secrets() -> list[str]:
    return [
        value
        for name, value in os.environ.items()
        if _SECRET_ENV_NAME.search(name.upper()) and len(value) >= _MIN_ENV_SECRET_LENGTH
    ]


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask known secret values, secret-bearing headers and common token shapes."""
    for value in sorted({*secrets, *_env_secrets()}, key=len, reverse=True):
        if value:
            text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    for pattern in _BARE_TOKENS:
        text = pattern.sub(MASK, text)
    return text


def _under_secret_key(value: Any, known: tuple[str, ...]) -> Any:
    """Mask a secret field's value but keep its shape, so a response can still be checked.

    A list of sentences is an error message, as in {"token": ["is missing"]}, not a secret.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return {key: _under_secret_key(item, known) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [
            redact_text(item, known)
            if isinstance(item, str) and any(c.isspace() for c in item.strip())
            else _under_secret_key(item, known)
            for item in value
        ]
    return MASK


def _redact_string(value: str, known: tuple[str, ...]) -> str:
    """A string holding JSON is redacted as JSON, so it stays valid."""
    if value.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return redact_text(value, known)
        return json.dumps(redact(parsed, known), ensure_ascii=False)
    return redact_text(value, known)


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact strings and containers, masking every value stored under a secret-looking key."""
    known = tuple(secrets)
    if isinstance(value, str):
        return _redact_string(value, known)
    if isinstance(value, Mapping):
        return {
            key: _under_secret_key(item, known)
            if isinstance(key, str) and _SECRET_KEY.search(key)
            else redact(item, known)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact(item, known) for item in value]
    return value
