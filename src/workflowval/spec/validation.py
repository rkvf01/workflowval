"""Checks live responses against a spec. The one boundary around openapi-core (FR-8)."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from openapi_core import Config, OpenAPI
from openapi_core.exceptions import OpenAPIError
from openapi_core.testing import MockRequest, MockResponse

_SERVER = "http://workflowval.invalid"
_MAX_MESSAGE = 300


@dataclass(frozen=True)
class SpecMismatch:
    """One way a response breaks the spec. `key` is the same however the value differs."""

    key: str
    message: str


def _without_servers(spec: dict[str, Any]) -> dict[str, Any]:
    """Point the spec at one fixed server, since callers check by operation path, not by host."""
    copied = copy.deepcopy(spec)
    copied["servers"] = [{"url": _SERVER}]
    for item in copied.get("paths", {}).values():
        item.pop("servers", None)
        for operation in item.values():
            if isinstance(operation, dict):
                operation.pop("servers", None)
    return copied


def _schema_errors(exc: BaseException) -> list[Any]:
    seen: BaseException | None = exc
    while seen is not None:
        errors = getattr(seen, "schema_errors", None)
        if errors:
            return list(errors)
        seen = seen.__cause__
    return []


def _from_schema_error(error: Any) -> SpecMismatch:
    path = "$" + "".join(
        "[]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
    )
    message = str(error.message)
    # Messages quote the bad value, which varies, so the key uses the rule that was broken. A
    # missing field's name is in its message.
    if error.validator == "required":
        shape = message
    else:
        shape = f"{error.validator} {error.validator_value!r}"[:_MAX_MESSAGE]
    return SpecMismatch(f"{path}: {shape}", f"{path}: {message}"[:_MAX_MESSAGE])


class ResponseChecker:
    """Checks responses against one spec, whichever host they came from."""

    def check_call(self, call: Mapping[str, Any]) -> list[SpecMismatch]:
        """Check one recorded call. A 5xx is an API failure, and its body says nothing of drift."""
        template, status = call.get("template"), call.get("status")
        if not isinstance(template, str) or not isinstance(status, int) or status >= 500:
            return []
        body = str(call.get("raw_body") or "").encode()
        return self.check(str(call["method"]), template, status, call.get("content_type"), body)

    def __init__(self, spec: dict[str, Any], *, validate_spec: bool = False) -> None:
        config = None if validate_spec else Config(spec_validator_cls=None)
        self._api = OpenAPI.from_dict(_without_servers(spec), config=config)

    def check(
        self, method: str, path: str, status: int, content_type: str | None, body: bytes
    ) -> list[SpecMismatch]:
        """Return the ways a response breaks the spec. `path` is the spec's path template."""
        request = MockRequest(_SERVER, method.lower(), path)
        response = MockResponse(body, status_code=status, content_type=content_type or "")
        try:
            self._api.validate_response(request, response)
        except OpenAPIError as exc:
            errors = _schema_errors(exc)
            if errors:
                return [_from_schema_error(error) for error in errors]
            kind = type(exc).__name__
            detail = str(exc.__cause__ or exc)[:_MAX_MESSAGE]
            if kind == "ResponseNotFound":
                return [SpecMismatch(f"status {status}", f"status {status} isn't in the spec")]
            return [SpecMismatch(f"{kind} on status {status}", detail)]
        return []
