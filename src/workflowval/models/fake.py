"""A scripted model, which makes the loop fully deterministic in tests and CI (NFR-8)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflowval.models.base import Message, Reply, ToolCall, ToolSpec, Usage


class FakeScriptExhausted(RuntimeError):
    """The code under test asked for more replies than were scripted."""


def call(name: str, /, **arguments: Any) -> Reply:
    """Build a reply holding one tool call."""
    return Reply(tool_calls=(ToolCall(id=f"call_{name}", name=name, arguments=arguments),))


@dataclass
class FakeModel:
    """Replays replies in order and records every request it received."""

    script: Iterable[Reply] = ()
    name: str = "fake/scripted"
    requests: list[tuple[list[Message], list[ToolSpec]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._replies = iter(self.script)

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
        """Return the next scripted reply."""
        self.requests.append((list(messages), list(tools)))
        try:
            return next(self._replies)
        except StopIteration:
            raise FakeScriptExhausted(
                f"{self.name}: script ran out after {len(self.requests) - 1} replies"
            ) from None

    @classmethod
    def from_file(cls, path: Path) -> FakeModel:
        """Load a script: a JSON list of {text, tool_calls: [{id, name, arguments}], usage}."""
        replies = [
            Reply(
                text=item.get("text", ""),
                tool_calls=tuple(ToolCall(**tc) for tc in item.get("tool_calls", [])),
                usage=Usage(**item.get("usage", {})),
            )
            for item in json.loads(path.read_text(encoding="utf-8"))
        ]
        return cls(script=replies)
