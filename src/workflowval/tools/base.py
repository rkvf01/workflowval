"""The tool type: a function the model can call, with a Pydantic input schema."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from workflowval.models.base import ToolSpec
from workflowval.schemas import AuthConfig
from workflowval.spec import SpecGraph


class ToolError(Exception):
    """A refusal or failure to report back to the model, not a crash."""


@dataclass(frozen=True)
class ToolContext:
    """Where a tool may read and write, and what it knows about the API."""

    workdir: Path
    graph: SpecGraph | None = None
    docs_dir: Path | None = None
    reports_dir: Path | None = None
    raw_spec: dict[str, Any] | None = None
    auth: AuthConfig | None = None
    new_workflows_only: bool = False


@dataclass(frozen=True)
class Tool:
    """A callable tool. `fn` receives arguments already validated against `schema`."""

    name: str
    description: str
    schema: type[BaseModel]
    fn: Callable[[Any, ToolContext], str]

    def spec(self) -> ToolSpec:
        """Describe this tool to the model."""
        return ToolSpec(self.name, self.description, self.schema.model_json_schema())

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments and run. Raises ValidationError or ToolError."""
        return self.fn(self.schema.model_validate(arguments), context)
