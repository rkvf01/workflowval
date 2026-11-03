"""`workflows/<id>.json`: one reviewable workflow, which becomes one test file (FR-4)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from workflowval.schemas._base import Strict

_PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)\}")
_JSON_PATH = re.compile(r"\$(\.[A-Za-z_][\w-]*|\[\d+\])*")
BUILTIN_VALUES = frozenset({"unique", "unique_int"})
"""Placeholders always available: fresh values per test run, so runs don't collide (FR-5)."""


class Source(StrEnum):
    """Who added the workflow."""

    PLANNER = "planner"
    REVIEWER = "reviewer"
    USER = "user"


class Basis(StrEnum):
    """Whether the rule is documented or was inferred by the Reviewer."""

    DOCUMENTED = "documented"
    ASSUMED = "assumed"


class Expect(Strict):
    """Expected response. `body` maps JSONPath expressions to expected values."""

    status: int = Field(ge=100, le=599)
    body: dict[str, Any] = Field(default_factory=dict)

    @field_validator("body")
    @classmethod
    def _body_keys_are_jsonpath(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key in value:
            if not _JSON_PATH.fullmatch(key):
                raise ValueError(f"expect.body key must be a JSONPath like $.a.b[0]: {key!r}")
        return value


class CleanupStep(Strict):
    """A request that removes data the workflow created (NFR-3)."""

    op: str = Field(min_length=1, description="Spec operationId")
    params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


class Step(CleanupStep):
    """A request plus its expectation.

    `body` and `auth` are additions to the doc's example, which has no way to send a request body
    or to call a route unauthenticated. See docs/m0-review.md.
    """

    save: dict[str, str] = Field(default_factory=dict, description="Name to JSONPath")
    expect: Expect
    auth: bool = Field(default=True, description="False sends this request without credentials")

    @field_validator("save")
    @classmethod
    def _save_values_are_jsonpath(cls, value: dict[str, str]) -> dict[str, str]:
        for name, path in value.items():
            if not _JSON_PATH.fullmatch(path):
                raise ValueError(f"save[{name!r}] must be a JSONPath like $.a.b[0]: {path!r}")
        return value


def _placeholders(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield from _PLACEHOLDER.findall(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _placeholders(item)
    elif isinstance(value, list):
        for item in value:
            yield from _placeholders(item)


class Workflow(Strict):
    """One user journey. A locked workflow belongs to the user; agents may never change it."""

    id: str = Field(
        pattern=r"^[a-z0-9_]+$",
        description="Lowercase, digits and underscores: names workflows/<id>.json and test_<id>.py",
    )
    source: Source
    basis: Basis
    locked: bool = False
    rule: str = Field(
        pattern=r"\S",
        description="The business rule this workflow checks, and where it comes from. Required.",
    )
    steps: list[Step] = Field(min_length=1)
    cleanup: list[CleanupStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Workflow:
        if self.locked and self.source is not Source.USER:
            raise ValueError("only workflows with source 'user' can be locked")
        saved = set(BUILTIN_VALUES)
        for index, step in enumerate(self.steps):
            self._require_saved(_placeholders([step.params, step.body]), saved, f"steps[{index}]")
            saved |= set(step.save)
            self._require_saved(_placeholders(step.expect.body), saved, f"steps[{index}].expect")
        for index, cleanup in enumerate(self.cleanup):
            where = f"cleanup[{index}]"
            self._require_saved(_placeholders([cleanup.params, cleanup.body]), saved, where)
        return self

    @staticmethod
    def _require_saved(names: Iterator[str], saved: set[str], where: str) -> None:
        for name in names:
            if name not in saved:
                raise ValueError(f"{where} uses {{{name}}}, which no earlier step saves")
