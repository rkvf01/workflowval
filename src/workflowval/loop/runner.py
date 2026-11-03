"""The pipeline runner: runs steps in order, enforces budgets and stop rules, checkpoints state."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from workflowval.loop.stats import StatsCollector, append_history, read_history, suggest_max_steps
from workflowval.loop.trace import Tracer
from workflowval.models.base import Message, Model, ModelError, Reply, ToolCall, ToolSpec, Usage
from workflowval.redact import redact_text
from workflowval.schemas import Label, RunState, RunStats, save_json
from workflowval.tools import ROLE_TOOLS, TOOLS, Tool, ToolContext, ToolError

MAX_INVALID_RETRIES = 2
PASSED = "passed"


class StopReason(StrEnum):
    """Why a run ended. Everything but COMPLETED names a limit or failure that was hit."""

    COMPLETED = "completed"
    MAX_ITERATIONS = "max_iterations"
    MAX_STEPS = "max_steps"
    NO_PROGRESS = "no_progress"
    INVALID_OUTPUT = "invalid_output"
    MODEL_ERROR = "model_error"
    API_UNREACHABLE = "api_unreachable"


@dataclass(frozen=True)
class Budget:
    """Hard limits for a run. One step is one model call, so review rounds count toward it."""

    max_review_rounds: int = 2
    max_fix_attempts: int = 3
    max_iterations: int = 10
    max_steps: int = 200
    no_progress_limit: int = 3


@dataclass(frozen=True)
class Role:
    """A prompt plus a restricted tool set. A known role can't be built with a forbidden tool."""

    name: str
    prompt: str
    tools: tuple[Tool, ...]
    model: Model

    def __post_init__(self) -> None:
        allowed = ROLE_TOOLS.get(self.name)
        if allowed is None:
            return
        forbidden = sorted({t.name for t in self.tools} - allowed)
        if forbidden:
            raise ValueError(f"role {self.name!r} may not have tools: {forbidden}")


@dataclass(frozen=True)
class TestOutcome:
    """One test's result from the Runner. `message` is empty when it passed."""

    __test__ = False

    test_id: str
    passed: bool
    message: str = ""


@dataclass(frozen=True)
class RoleStep:
    """Run a role once. `workflow_id` credits its calls to that workflow in the stats."""

    name: str
    role: Role
    instruction: str
    workflow_id: str | None = None


@dataclass(frozen=True)
class CodeStep:
    """Run plain code, with no model."""

    name: str
    fn: Callable[[RunContext], None]


@dataclass(frozen=True)
class ReviewLoop:
    """Review, and revise while the check asks for it, up to `max_review_rounds` rounds."""

    name: str
    review: RoleStep
    revise: RoleStep
    needs_revision: Callable[[RunContext], bool]


@dataclass(frozen=True)
class FixLoop:
    """Run tests, label failures, fix those labeled TEST_BUG, and re-run the fixed ones.

    `execute` gets the ids to run, or None for all. `label` returns one label per failure, and a
    failure it leaves out becomes UNRESOLVED. `fix` is only ever called for a TEST_BUG.
    """

    name: str
    execute: Callable[[RunContext, frozenset[str] | None], Sequence[TestOutcome]]
    label: Callable[[RunContext, Sequence[TestOutcome]], Mapping[str, Label]]
    fix: Callable[[RunContext, TestOutcome], None]


Step = RoleStep | CodeStep | ReviewLoop | FixLoop


@dataclass(frozen=True)
class Pipeline:
    """The ordered steps of a run."""

    steps: tuple[Step, ...]


@dataclass(frozen=True)
class RunResult:
    """How a run ended. Tests still open when a budget hit are UNRESOLVED, never lost."""

    stop_reason: StopReason
    detail: str
    tests: dict[str, str]
    state: RunState
    stats: RunStats
    suggestion: str | None = None

    @property
    def summary(self) -> str:
        """One line of tokens and cost to print for every run (NFR-4)."""
        s = self.state
        cost = f"${s.cost_usd:.4f}"
        if not s.cost_tracked:
            cost = "not tracked"
        elif s.unpriced_calls:
            cost += f" (plus {s.unpriced_calls} calls with no known price)"
        line = (
            f"stopped: {self.stop_reason.value}; steps: {s.steps_used}; "
            f"tokens: {s.input_tokens} in, {s.output_tokens} out; cost: {cost}"
        )
        return f"{line}\n{self.suggestion}" if self.suggestion else line


class StopRun(Exception):
    """Raised by steps and the loop to end a run with a stated reason."""

    def __init__(self, reason: StopReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class RunContext:
    """Run state shared with code steps and callbacks."""

    budget: Budget
    workdir: Path
    tracer: Tracer
    state: RunState = field(default_factory=RunState)
    tool_context: ToolContext | None = None
    stats: StatsCollector = field(init=False)

    def __post_init__(self) -> None:
        self.stats = StatsCollector(self.budget.max_steps, self.state.spec_hash)

    @property
    def state_path(self) -> Path:
        """Where checkpoints go."""
        return self.workdir / ".workflowval" / "state.json"

    @property
    def stats_path(self) -> Path:
        """This run's stats."""
        return self.workdir / ".workflowval" / "stats.json"

    @property
    def history_path(self) -> Path:
        """One line per finished run."""
        return self.workdir / ".workflowval" / "stats_history.jsonl"

    def checkpoint(self) -> None:
        """Save the resume point and the stats so far."""
        save_json(self.state, self.state_path)
        save_json(self.stats.snapshot(), self.stats_path)

    def run_role(self, role: Role, instruction: str, workflow_id: str | None = None) -> str:
        """Talk to a role until it answers without tool calls, and return that answer.

        With `workflow_id`, every call counts toward that workflow in the stats. Without it, each
        tool call counts toward the workflow its arguments name, or else toward the run.
        """
        messages = [
            Message("system", role.prompt),
            Message("user", instruction),
        ]
        tools = {tool.name: tool for tool in role.tools}
        specs = [tool.spec() for tool in role.tools]
        invalid_streak = 0
        while True:
            reply = self._call_model(role, messages, specs, workflow_id)
            self.stats.record_reply(
                workflow_id, reply.tool_calls, {*TOOLS, *tools}, role.name, reply.usage.cost_usd
            )
            messages.append(Message("assistant", reply.text, tool_calls=reply.tool_calls))
            if not reply.tool_calls:
                return reply.text
            any_invalid = False
            for call in reply.tool_calls:
                result, invalid = self._call_tool(role, tools, call)
                any_invalid = any_invalid or invalid
                messages.append(Message("tool", result, tool_call_id=call.id))
            invalid_streak = invalid_streak + 1 if any_invalid else 0
            if invalid_streak > MAX_INVALID_RETRIES:
                raise StopRun(
                    StopReason.INVALID_OUTPUT,
                    f"role {role.name} sent invalid tool calls {invalid_streak} times in a row",
                )

    def _call_model(
        self,
        role: Role,
        messages: list[Message],
        specs: list[ToolSpec],
        workflow_id: str | None,
    ) -> Reply:
        if self.state.steps_used >= self.budget.max_steps:
            raise StopRun(StopReason.MAX_STEPS, f"used all {self.budget.max_steps} steps")
        self.state.steps_used += 1
        try:
            reply = role.model.complete(messages, specs)
        except ModelError as exc:
            self.stats.record_failed_call(workflow_id, role.name)
            self.tracer.event("model_error", role=role.name, model=role.model.name, error=str(exc))
            raise StopRun(StopReason.MODEL_ERROR, str(exc)) from exc
        self._add_usage(reply.usage)
        self.tracer.event(
            "model_call",
            role=role.name,
            model=role.model.name,
            messages=len(messages),
            reply_size=len(reply.text),
            tool_calls=[call.name for call in reply.tool_calls],
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            cost_usd=reply.usage.cost_usd,
        )
        return reply

    def _add_usage(self, usage: Usage) -> None:
        self.state.input_tokens += usage.input_tokens
        self.state.output_tokens += usage.output_tokens
        if usage.cost_usd is None:
            self.state.unpriced_calls += 1
        else:
            self.state.cost_usd += usage.cost_usd

    def _call_tool(self, role: Role, tools: dict[str, Tool], call: ToolCall) -> tuple[str, bool]:
        """Return the text for the model, and whether the call counts as invalid output."""
        invalid = True
        tool = tools.get(call.name)
        if tool is None:
            result = f"Error: role {role.name} can't call {call.name!r}. Tools: {sorted(tools)}"
        elif call.parse_error is not None:
            result = f"Error: {call.parse_error}"
        else:
            try:
                result = tool.invoke(call.arguments, self.tool_context or ToolContext(self.workdir))
                invalid = False
            except ValidationError as exc:
                # include_input=False: the rejected value may be a secret.
                errors = exc.errors(include_url=False, include_input=False, include_context=False)
                result = f"Error: invalid arguments for {call.name}: {json.dumps(errors)}"
            except ToolError as exc:
                result = f"Error: {exc}"
                invalid = False
        result = redact_text(result)
        self.tracer.event(
            "tool_call",
            role=role.name,
            tool=call.name,
            arguments=call.arguments,
            result_size=len(result),
            invalid=invalid,
            error=result if result.startswith("Error:") else None,
        )
        return result, invalid


def _run_review_loop(ctx: RunContext, step: ReviewLoop) -> None:
    while ctx.state.review_rounds < ctx.budget.max_review_rounds:
        ctx.run_role(step.review.role, step.review.instruction)
        ctx.state.review_rounds += 1
        ctx.checkpoint()
        if not step.needs_revision(ctx):
            return
        ctx.run_role(step.revise.role, step.revise.instruction)
        ctx.checkpoint()


def _close(ctx: RunContext, test_id: str, status: str) -> None:
    ctx.state.closed_tests[test_id] = status
    if test_id in ctx.state.open_tests:
        ctx.state.open_tests.remove(test_id)


def _run_fix_loop(ctx: RunContext, step: FixLoop) -> None:
    state, budget = ctx.state, ctx.budget
    to_run: frozenset[str] | None = None
    previous: tuple[int, frozenset[tuple[str, str]]] | None = None
    stalled = 0
    while to_run is None or to_run:
        if state.iterations >= budget.max_iterations:
            raise StopRun(StopReason.MAX_ITERATIONS, f"used all {budget.max_iterations} iterations")
        state.iterations += 1
        outcomes = list(step.execute(ctx, to_run))
        # A test asked for but not reported must not count as passing.
        reported = {outcome.test_id for outcome in outcomes}
        outcomes += [
            TestOutcome(test_id, False, "the Runner reported no result")
            for test_id in sorted((to_run or frozenset()) - reported)
        ]
        failures = [outcome for outcome in outcomes if not outcome.passed]
        state.open_tests = sorted({*state.open_tests, *(f.test_id for f in failures)})
        for outcome in outcomes:
            if outcome.passed:
                _close(ctx, outcome.test_id, PASSED)

        labels = step.label(ctx, failures) if failures else {}
        to_fix: list[TestOutcome] = []
        for failure in failures:
            label = labels.get(failure.test_id, Label.UNRESOLVED)
            attempts = state.fix_attempts.get(failure.test_id, 0)
            if label is not Label.TEST_BUG:
                _close(ctx, failure.test_id, label.value)
            elif attempts >= budget.max_fix_attempts:
                _close(ctx, failure.test_id, Label.UNRESOLVED.value)
            else:
                to_fix.append(failure)

        # Progress: a test closed, or the failures changed. The first pass sets the baseline.
        current = (
            len(state.closed_tests),
            frozenset((f.test_id, f.message) for f in failures),
        )
        stalled = stalled + 1 if current == previous else 0
        previous = current
        ctx.checkpoint()
        if to_fix and stalled >= budget.no_progress_limit:
            raise StopRun(StopReason.NO_PROGRESS, f"no progress in {stalled} iterations")

        for failure in to_fix:
            state.fix_attempts[failure.test_id] = state.fix_attempts.get(failure.test_id, 0) + 1
            step.fix(ctx, failure)
            ctx.checkpoint()
        to_run = frozenset(failure.test_id for failure in to_fix)


def _run_step(ctx: RunContext, step: Step) -> None:
    if isinstance(step, RoleStep):
        ctx.run_role(step.role, step.instruction, step.workflow_id)
    elif isinstance(step, CodeStep):
        step.fn(ctx)
    elif isinstance(step, ReviewLoop):
        _run_review_loop(ctx, step)
    else:
        _run_fix_loop(ctx, step)


def run(
    pipeline: Pipeline,
    budget: Budget,
    *,
    workdir: Path,
    spec_hash: str = "",
    verbose: bool = False,
    tool_context: ToolContext | None = None,
    cost_tracked: bool = True,
) -> RunResult:
    """Run a pipeline. It always terminates, and the result says which limit stopped it, if any."""
    tracer = Tracer(workdir / ".workflowval" / "trace.jsonl", echo=verbose)
    state = RunState(spec_hash=spec_hash, cost_tracked=cost_tracked)
    ctx = RunContext(budget, workdir, tracer, state, tool_context)
    reason, detail = StopReason.COMPLETED, ""
    tracer.event("run_start", spec_hash=spec_hash, steps=[step.name for step in pipeline.steps])
    ctx.checkpoint()
    try:
        for step in pipeline.steps:
            _run_step(ctx, step)
            ctx.state.completed_steps.append(step.name)
            ctx.checkpoint()
    except StopRun as stop:
        reason, detail = stop.reason, stop.detail
    # Whatever is still open failed without a final label, and must show up as UNRESOLVED.
    for test_id in list(ctx.state.open_tests):
        _close(ctx, test_id, Label.UNRESOLVED.value)
    ctx.state.stop_reason = reason.value
    ctx.checkpoint()
    stats = ctx.stats.snapshot(reason.value)
    save_json(stats, ctx.stats_path)
    append_history(stats, ctx.history_path)
    suggestion = suggest_max_steps(read_history(ctx.history_path), budget.max_steps)
    result = RunResult(reason, detail, dict(ctx.state.closed_tests), ctx.state, stats, suggestion)
    tracer.event(
        "run_end",
        stop_reason=reason.value,
        detail=detail,
        steps_used=ctx.state.steps_used,
        input_tokens=ctx.state.input_tokens,
        output_tokens=ctx.state.output_tokens,
        cost_usd=ctx.state.cost_usd,
        unpriced_calls=ctx.state.unpriced_calls,
        suggestion=suggestion,
    )
    return result
