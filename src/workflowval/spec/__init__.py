"""OpenAPI loading, the dependency graph and response validation."""

from __future__ import annotations

from typing import Any

from workflowval.spec.graph import (
    Link,
    Operation,
    Parameter,
    SpecGraph,
    build_graph,
    resolve_base_url,
)
from workflowval.spec.loader import SpecError, load_spec, parse_spec, resolve_refs, spec_hash


def validate_response(op: str, resp: Any) -> list[str]:
    """Return the ways a live response breaks the spec. The one boundary around openapi-core."""
    raise NotImplementedError("spec.validate_response arrives in M2")


__all__ = [
    "Link",
    "Operation",
    "Parameter",
    "SpecError",
    "SpecGraph",
    "build_graph",
    "load_spec",
    "parse_spec",
    "resolve_base_url",
    "resolve_refs",
    "spec_hash",
    "validate_response",
]
