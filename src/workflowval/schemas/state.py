"""`.workflowval/state.json`: the resume point and spec hash (FR-13)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from workflowval.schemas._base import Strict


class RunState(Strict):
    """A checkpoint, rewritten atomically as the run advances."""

    format_version: Literal[1] = 1
    spec_hash: str = ""
    completed_steps: list[str] = Field(default_factory=list)
    review_rounds: int = Field(default=0, ge=0)
    iterations: int = Field(default=0, ge=0)
    steps_used: int = Field(default=0, ge=0)
    fix_attempts: dict[str, int] = Field(default_factory=dict)
    closed_tests: dict[str, str] = Field(
        default_factory=dict, description="Test id to 'passed' or its final label"
    )
    open_tests: list[str] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    unpriced_calls: int = Field(default=0, ge=0)
    stop_reason: str | None = None
