"""Counts of model and tool calls per workflow, and a step limit suggested from past runs."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
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
    """Collects one run's counts."""

    def __init__(self, limit: int, spec_hash: str = "") -> None:
        self.limit = limit
        self.spec_hash = spec_hash
        self._models: Counter[str | None] = Counter()
        self._tools: dict[str | None, Counter[str]] = {}

    def record_failed_call(self, scope: str | None) -> None:
        """Count a model call that failed, which still used a step."""
        self._models[scope] += 1

    def record_reply(
        self, scope: str | None, calls: Iterable[ToolCall], known_tools: Collection[str]
    ) -> None:
        """Count one model call and its tool calls.

        A model can invent tool names and they aren't safe to store, so those count as unknown.
        """
        owners: set[str | None] = set()
        for call in calls:
            owner = scope or workflow_of(call.arguments)
            owners.add(owner)
            name = call.name if call.name in known_tools else UNKNOWN_TOOL
            self._tools.setdefault(owner, Counter())[name] += 1
        self._models[owners.pop() if scope is None and len(owners) == 1 else scope] += 1

    def _counts(self, owner: str | None) -> CallCounts:
        tools = self._tools.get(owner, Counter())
        return CallCounts(model_calls=self._models[owner], tool_calls=dict(sorted(tools.items())))

    def snapshot(self, stop_reason: str | None = None) -> RunStats:
        """Return the counts so far."""
        named = {owner for owner in [*self._models, *self._tools] if owner is not None}
        return RunStats(
            run=self._counts(None),
            workflows={owner: self._counts(owner) for owner in sorted(named)},
            total=StatsTotal(
                model_calls=sum(self._models.values()),
                tool_calls=sum(sum(tools.values()) for tools in self._tools.values()),
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
    return (
        f"the largest of your last {len(usable)} runs used {largest} step{'s' if largest != 1 else ''}; "
        f"consider --max-steps {target}"
    )
