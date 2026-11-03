"""Load and validate an OpenAPI 3.x JSON spec from a file or URL, and resolve its `$ref`s (FR-1)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError


class SpecError(Exception):
    """A spec can't be used. The message says what's wrong and where."""


def read_spec_text(source: str) -> str:
    """Read a spec's text from a path or an http(s) URL."""
    if urlparse(source).scheme in {"http", "https"}:
        try:
            response = httpx.get(source, timeout=30, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SpecError(f"can't fetch spec from {source}: {exc}") from exc
        return response.text
    path = Path(source)
    if not path.is_file():
        raise SpecError(f"spec file not found: {source}")
    return path.read_text(encoding="utf-8")


def parse_spec(text: str, source: str) -> dict[str, Any]:
    """Parse and validate spec text. Only JSON is supported in v1."""
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(
            f"{source} isn't valid JSON ({exc}). Only JSON specs are supported: "
            "convert YAML to JSON first."
        ) from exc
    if not isinstance(spec, dict):
        raise SpecError(f"{source} must be a JSON object, not {type(spec).__name__}")
    version = str(spec.get("openapi", ""))
    if not version.startswith(("3.0", "3.1")):
        found = version or "no 'openapi' field"
        raise SpecError(f"{source}: only OpenAPI 3.0 and 3.1 are supported, found {found}")
    try:
        validate(spec)
    except OpenAPIValidationError as exc:
        where = "/".join(str(part) for part in exc.absolute_path) or "(top level)"
        raise SpecError(f"{source}: invalid spec at {where}: {exc.message}") from exc
    return spec


def load_raw_spec(source: str) -> dict[str, Any]:
    """Load a spec as written, with its `$ref`s. Spec patches apply to this form."""
    return parse_spec(read_spec_text(source), source)


def load_spec(source: str) -> dict[str, Any]:
    """Load a spec from a path or URL, validated, with every `$ref` resolved."""
    return resolve_refs(parse_spec(read_spec_text(source), source))


def spec_hash(spec: dict[str, Any]) -> str:
    """Return a stable hash of a spec, used to decide whether to re-plan."""
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def _lookup(root: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise SpecError(f"only local $refs are supported, found {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            raise SpecError(f"$ref points at nothing: {ref}")
        node = node[key]
    return node


def resolve_refs(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with `$ref`s replaced. A recursive reference stays as a `$ref`, marking it."""

    def walk(node: Any, active: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [walk(item, active) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in active:
                return {"$ref": ref}
            return walk(copy.deepcopy(_lookup(spec, ref)), (*active, ref))
        return {key: walk(value, active) for key, value in node.items()}

    resolved: dict[str, Any] = walk(spec, ())
    return resolved
