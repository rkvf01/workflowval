"""Remove secrets from anything written to logs, reports or generated code (NFR-7)."""

from __future__ import annotations

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
    re.compile(r"(?i)\b(x-api-key|api[_-]?key|access_token|token)(\"?\s*[:=]\s*\"?)[^\s\"'&,}]+"),
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


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact strings and containers, masking every value stored under a secret-looking key."""
    known = tuple(secrets)
    if isinstance(value, str):
        return redact_text(value, known)
    if isinstance(value, Mapping):
        return {
            key: MASK if isinstance(key, str) and _SECRET_KEY.search(key) else redact(item, known)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [redact(item, known) for item in value]
    return value
