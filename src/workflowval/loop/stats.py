"""Counts of model and tool calls per workflow, and a step limit suggested from past runs."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from workflowval.models.base import ToolCall
from workflowval.schemas import CallCounts, RunStats, StatsTotal

MIN_RUNS_FOR_SUGGESTION = 5
RECENT_RUNS = 20
MIN_SUGGESTED_LIMIT = 10
UNKNOWN_TOOL = "<unknown>"
MAX_STEPS_REASON = "max_steps"
# Runs that ended for other reasons stopped early, so they'd make the limit look too generous.
USABLE_STOP_REASONS = frozenset({"completed", MAX_STEPS_REASON, "max_iterations", "no_progress"})

_WORKFLOW_ID = re.compile(r"[a-z0-9_]+")
_TEST_FILE = re.compile(r"(?:^|/)test_([a-z0-9_]+)\.py(?:::|$)")


@dataclass
class _Tally:
    model_calls: int = 0
    cost_usd: float = 0.0
    tools: Counter[str] = field(default_factory=Counter)

    def counts(self) -> CallCounts:
        return CallCounts(
            model_calls=self.model_calls,
            tool_calls=dict(sorted(self.tools.items())),
            cost_usd=self.cost_usd,
        )


def workflow_of(arguments: Mapping[str, Any]) -> str | None:
    """Return the workflow a tool call's arguments name, if they name one validly."""
    workflow = arguments.get("workflow")
    candidates = [
        arguments.get("workflow_id"),
        workflow.get("id") if isinstance(workflow, Mapping) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and _WORKFLOW_ID.fullmatch(candidate):
            return candidate
    test_id = arguments.get("test_id")
    if isinstance(test_id, str) and (match := _TEST_FILE.search(test_id)):
        return match.group(1)
    return None


class StatsCollector:
    """Collects one run's counts and costs, per workflow and per role."""

    def __init__(self, limit: int, spec_hash: str = "") -> None:
        self.limit = limit
        self.spec_hash = spec_hash
        self._owners: dict[str | None, _Tally] = {}
        self._roles: dict[str, _Tally] = {}

    def _tallies(self, owner: str | None, role: str | None) -> list[_Tally]:
        tallies = [self._owners.setdefault(owner, _Tally())]
        if role is not None:
            tallies.append(self._roles.setdefault(role, _Tally()))
        return tallies

    def record_failed_call(self, scope: str | None, role: str | None = None) -> None:
        """Count a model call that failed, which still used a step."""
        for tally in self._tallies(scope, role):
            tally.model_calls += 1

    def record_reply(
        self,
        scope: str | None,
        calls: Iterable[ToolCall],
        known_tools: Collection[str],
        role: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Count one model call, its cost and its tool calls.

        A model can invent tool names and they aren't safe to store, so those count as unknown.
        """
        owners: set[str | None] = set()
        for call in calls:
            owner = scope or workflow_of(call.arguments)
            owners.add(owner)
            name = call.name if call.name in known_tools else UNKNOWN_TOOL
            for tally in self._tallies(owner, role):
                tally.tools[name] += 1
        model_owner = owners.pop() if scope is None and len(owners) == 1 else scope
        for tally in self._tallies(model_owner, role):
            tally.model_calls += 1
            tally.cost_usd += cost_usd or 0.0

    def snapshot(self, stop_reason: str | None = None) -> RunStats:
        """Return the counts so far."""
        tallies = list(self._owners.values())
        return RunStats(
            run=self._owners.get(None, _Tally()).counts(),
            workflows={
                owner: tally.counts()
                for owner, tally in sorted(
                    (item for item in self._owners.items() if item[0] is not None),
                    key=lambda item: str(item[0]),
                )
                if owner is not None
            },
            roles={role: self._roles[role].counts() for role in sorted(self._roles)},
            total=StatsTotal(
                model_calls=sum(tally.model_calls for tally in tallies),
                tool_calls=sum(sum(tally.tools.values()) for tally in tallies),
                cost_usd=sum(tally.cost_usd for tally in tallies),
                limit=self.limit,
            ),
            spec_hash=self.spec_hash,
            stop_reason=stop_reason,
            finished_at=datetime.now(UTC).isoformat() if stop_reason else None,
        )


def append_history(stats: RunStats, path: Path) -> None:
    """Add a finished run to the history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(stats.model_dump_json(exclude_none=True) + "\n")


def read_history(path: Path) -> list[RunStats]:
    """Read past runs. Bad lines are skipped, since they must never stop a run from finishing."""
    if not path.exists():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            runs.append(RunStats.model_validate_json(line))
        except ValidationError:
            continue
    return runs


def _one_and_a_half_times(value: int) -> int:
    """Return 1.5 times a value, rounded up to the next 10."""
    return math.ceil(value * 15 / 100) * 10


def suggest_max_steps(history: Iterable[RunStats], limit: int) -> str | None:
    """Suggest a `--max-steps` value from past runs. This only ever advises; it changes nothing."""
    usable = [run for run in history if run.stop_reason in USABLE_STOP_REASONS][-RECENT_RUNS:]
    if len(usable) < MIN_RUNS_FOR_SUGGESTION:
        return None
    # A run that hit a lower, older limit says nothing against the current limit.
    hits = [
        run for run in usable if run.stop_reason == MAX_STEPS_REASON and run.total.limit >= limit
    ]
    if hits:
        return (
            f"{len(hits)} of your last {len(usable)} runs hit the step limit of {limit}; "
            f"consider --max-steps {_one_and_a_half_times(limit)}"
        )
    largest = max(run.total.model_calls for run in usable)
    target = max(_one_and_a_half_times(largest), MIN_SUGGESTED_LIMIT)
    if target == limit:
        return None
    steps = "step" if largest == 1 else "steps"
    return (
        f"the largest of your last {len(usable)} runs used {largest} {steps}; "
        f"consider --max-steps {target}"
    )
