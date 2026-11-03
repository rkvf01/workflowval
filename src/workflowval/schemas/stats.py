"""`.workflowval/stats.json` and `stats_history.jsonl`: names and counts, never arguments."""

from __future__ import annotations

from pydantic import Field

from workflowval.schemas._base import Strict


class CallCounts(Strict):
    """Model calls, and tool calls by tool name, including refused and invalid attempts."""

    model_calls: int = Field(default=0, ge=0)
    tool_calls: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = Field(default=0.0, ge=0, description="Sum over calls with a known price")


class StatsTotal(Strict):
    """Totals for a run. `model_calls` is what `--max-steps` limits."""

    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    limit: int = Field(ge=1, description="The --max-steps value for this run")


class RunStats(Strict):
    """One run's calls. `run` holds work that belongs to no single workflow."""

    run: CallCounts = Field(default_factory=CallCounts)
    workflows: dict[str, CallCounts] = Field(default_factory=dict)
    roles: dict[str, CallCounts] = Field(default_factory=dict)
    total: StatsTotal
    spec_hash: str = ""
    stop_reason: str | None = None
    finished_at: str | None = None
