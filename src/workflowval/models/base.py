"""The one interface the rest of workflowval uses to talk to a model."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class ModelError(Exception):
    """A provider call failed. The message is already redacted."""


@dataclass(frozen=True)
class ToolCall:
    """A tool call from the model. `parse_error` is set when its arguments weren't valid JSON."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str | None = None


@dataclass(frozen=True)
class Message:
    """One conversation message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """What the model sees of a tool."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Tokens and cost of one call. `cost_usd` is None when the model has no known price."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class Reply:
    """A model's answer."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)


class Model(Protocol):
    """Anything that can answer a conversation."""

    name: str

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
        """Return the model's reply. Raises ModelError if the provider call fails."""
        ...
