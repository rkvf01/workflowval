"""OpenAPI loading, the dependency graph and response validation. Arrives in M1.

validate_response() is the one boundary around openapi-core, so the library can be swapped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpecGraph:
    """Resources, operations, parameters and producer-to-consumer links."""

    operations: tuple[str, ...] = ()


def validate_response(op: str, resp: Any) -> list[str]:
    """Return the ways a live response breaks the spec for an operation."""
    raise NotImplementedError("spec.validate_response arrives in M1")
