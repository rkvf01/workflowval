"""Builds curl steps from recorded calls, so a bug report reproduces by construction (FR-9)."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from workflowval.schemas import ApiKeyAuth, AuthConfig, BasicAuth, BearerAuth, LoginAuth

MASK = "[REDACTED]"
_DROPPED_HEADERS = frozenset(
    {"host", "accept-encoding", "user-agent", "connection", "content-length"}
)


def _secret_header(value: str, auth: AuthConfig | None) -> str:
    """Replace a masked header value with the env var it came from."""
    scheme = auth.scheme if auth else None
    if isinstance(scheme, BearerAuth):
        return f"Bearer ${scheme.token_env}"
    if isinstance(scheme, ApiKeyAuth):
        return f"${scheme.value_env}"
    if isinstance(scheme, LoginAuth):
        return value.replace(MASK, "$TOKEN")
    return "$SECRET"


def _header_args(headers: Mapping[str, str], auth: AuthConfig | None) -> list[str]:
    args = []
    for name, value in headers.items():
        if name.lower() in _DROPPED_HEADERS:
            continue
        if MASK in value:
            if isinstance(auth.scheme if auth else None, BasicAuth):
                continue  # Basic auth goes in through -u.
            # Double quotes, so the shell fills in the env var.
            args.append(f'-H "{name}: {_secret_header(value, auth)}"')
        else:
            args.append(f"-H {shlex.quote(f'{name}: {value}')}")
    return args


def curl_command(call: Mapping[str, Any], auth: AuthConfig | None) -> str:
    """Render one recorded call as a curl command, with env vars in place of secrets."""
    scheme = auth.scheme if auth else None
    url = str(call["url"])
    if isinstance(scheme, ApiKeyAuth) and MASK in url:
        quoted_url = f'"{url.replace(MASK, f"${scheme.value_env}")}"'
    else:
        quoted_url = shlex.quote(url)
    parts = ["curl", "-i", "-X", str(call["method"]), quoted_url]
    headers = call.get("request_headers") or {}
    parts += _header_args(headers, auth)
    if isinstance(scheme, BasicAuth) and any(MASK in value for value in headers.values()):
        parts.append(f'-u "${scheme.username_env}:${scheme.password_env}"')
    body = call.get("request_body")
    if body is not None:
        parts += ["-d", shlex.quote(body if isinstance(body, str) else json.dumps(body))]
    return " ".join(parts)


def curl_steps(calls: Sequence[Mapping[str, Any]], auth: AuthConfig | None) -> list[str]:
    """Render a test's calls as the steps to reproduce it by hand."""
    steps = []
    if auth is not None and isinstance(auth.scheme, LoginAuth):
        fields = ", ".join(auth.scheme.body_env.values())
        steps.append(
            f"# Log in with {auth.scheme.op}, using {fields}, and set TOKEN to the token returned."
        )
    if len(calls) > 1:
        steps.append("# Where a step uses an id, use the one your earlier response returned.")
    steps += [curl_command(call, auth) for call in calls]
    return steps
