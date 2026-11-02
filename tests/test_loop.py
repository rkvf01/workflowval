from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from workflowval.loop import (
    Budget,
    CodeStep,
    FixLoop,
    Pipeline,
    ReviewLoop,
    Role,
    RoleStep,
    RunContext,
    StopReason,
    TestOutcome,
    make_role,
    run,
)
from workflowval.models import (
    FakeModel,
    Message,
    ModelError,
    Reply,
    ToolCall,
    ToolSpec,
    Usage,
    call,
)
from workflowval.schemas import Label, RunState, load_json
from workflowval.tools import TOOLS, Tool

DONE = Reply(text="done")


class EchoArgs(BaseModel):
    value: int


def echo(args: EchoArgs, _: Any) -> str:
    """Echo a number."""
    return f"echo {args.value}"


ECHO = Tool("echo", "Echo a number.", EchoArgs, echo)


def role(script: Sequence[Reply], name: str = "custom") -> Role:
    return Role(name, "prompt", (ECHO,), FakeModel(list(script)))


def go(steps: Sequence[Any], tmp_path: Path, **budget: Any) -> Any:
    return run(Pipeline(tuple(steps)), Budget(**budget), workdir=tmp_path)


def trace(tmp_path: Path) -> list[dict[str, Any]]:
    lines = (tmp_path / ".workflowval" / "trace.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def tool_messages(model: Any, request: int) -> list[str]:
    return [m.content for m in model.requests[request][0] if m.role == "tool"]


# ---- Roles and tool calls


def test_role_step_feeds_tool_results_back_until_the_model_answers(tmp_path: Path) -> None:
    worker = role([call("echo", value=1), DONE])
    result = go([RoleStep("s", worker, "do it")], tmp_path)
    assert result.stop_reason is StopReason.COMPLETED
    model: Any = worker.model
    assert tool_messages(model, 1) == ["echo 1"]
    first_messages, first_tools = model.requests[0]
    assert [(m.role, m.content) for m in first_messages] == [
        ("system", "prompt"),
        ("user", "do it"),
    ]
    assert [t.name for t in first_tools] == ["echo"]


def test_every_tool_call_in_a_reply_gets_a_result(tmp_path: Path) -> None:
    both = Reply(
        tool_calls=(
            ToolCall("a", "echo", {"value": 1}),
            ToolCall("b", "echo", {"value": 2}),
        )
    )
    worker = role([both, DONE])
    go([RoleStep("s", worker, "x")], tmp_path)
    model: Any = worker.model
    answered = [m.tool_call_id for m in model.requests[1][0] if m.role == "tool"]
    assert answered == ["a", "b"]


def test_writer_is_refused_a_tool_outside_its_list(tmp_path: Path) -> None:
    workflow = {"id": "x", "source": "planner", "basis": "assumed", "steps": []}
    model = FakeModel([call("write_workflow", workflow=workflow), DONE])
    writer = make_role("writer", model)
    assert go([RoleStep("s", writer, "x")], tmp_path).stop_reason is StopReason.COMPLETED
    assert "can't call 'write_workflow'" in tool_messages(model, 1)[0]
    assert not (tmp_path / "workflows").exists()


@pytest.mark.parametrize(
    ("name", "tool"),
    [
        ("writer", "write_workflow"),
        ("writer", "propose_spec_patch"),
        ("planner", "write_test"),
        ("planner", "http_probe"),
        ("reviewer", "write_test"),
        ("reviewer", "write_workflow"),
    ],
)
def test_a_role_cannot_be_built_with_a_forbidden_tool(name: str, tool: str) -> None:
    with pytest.raises(ValueError, match="may not have tools"):
        Role(name, "p", (TOOLS[tool],), FakeModel())


@pytest.mark.parametrize("name", ["planner", "reviewer", "writer"])
def test_make_role_loads_its_prompt_and_offers_only_its_tools(name: str) -> None:
    built = make_role(name, FakeModel())
    assert built.prompt.startswith(f"You are the {name.title()}")
    assert built.tools


def test_make_role_rejects_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        make_role("runner", FakeModel())


def test_locked_refusal_reaches_the_model_and_does_not_count_as_invalid(tmp_path: Path) -> None:
    path = tmp_path / "workflows" / "wf.json"
    path.parent.mkdir()
    steps = [{"op": "o", "expect": {"status": 200}}]
    locked = {
        "id": "wf",
        "source": "user",
        "basis": "documented",
        "locked": True,
        "rule": "r",
        "steps": steps,
    }
    path.write_text(json.dumps(locked))
    attempt = {"id": "wf", "source": "planner", "basis": "assumed", "rule": "r", "steps": steps}
    model = FakeModel([call("write_workflow", workflow=attempt)] * 4 + [DONE])
    planner = make_role("planner", model)
    assert go([RoleStep("s", planner, "x")], tmp_path).stop_reason is StopReason.COMPLETED
    assert "locked" in tool_messages(model, 1)[0]
    assert json.loads(path.read_text()) == locked


# ---- Invalid output (NFR-9)


def test_invalid_arguments_return_the_error_then_the_model_can_recover(tmp_path: Path) -> None:
    worker = role(
        [call("echo", value="nope"), call("echo", value="nope"), call("echo", value=1), DONE]
    )
    result = go([RoleStep("s", worker, "x")], tmp_path)
    assert result.stop_reason is StopReason.COMPLETED
    model: Any = worker.model
    assert "invalid arguments for echo" in tool_messages(model, 1)[0]
    assert "value" in tool_messages(model, 1)[0]


def test_third_invalid_reply_in_a_row_stops_the_run_cleanly(tmp_path: Path) -> None:
    worker = role([call("echo", value="nope")] * 3)
    result = go([RoleStep("s", worker, "x")], tmp_path)
    assert result.stop_reason is StopReason.INVALID_OUTPUT
    assert result.state.steps_used == 3


def test_a_valid_reply_resets_the_invalid_count(tmp_path: Path) -> None:
    bad, good = call("echo", value="nope"), call("echo", value=1)
    worker = role([bad, bad, good, bad, bad, DONE])
    assert go([RoleStep("s", worker, "x")], tmp_path).stop_reason is StopReason.COMPLETED


@pytest.mark.parametrize(
    "bad",
    [
        Reply(tool_calls=(ToolCall("1", "echo", parse_error="arguments are not valid JSON"),)),
        call("no_such_tool"),
    ],
)
def test_malformed_and_unknown_calls_count_as_invalid(bad: Reply, tmp_path: Path) -> None:
    result = go([RoleStep("s", role([bad] * 3), "x")], tmp_path)
    assert result.stop_reason is StopReason.INVALID_OUTPUT


def test_validation_errors_do_not_echo_the_rejected_value(tmp_path: Path) -> None:
    worker = role([call("echo", value="secret-value-123"), DONE])
    go([RoleStep("s", worker, "x")], tmp_path)
    model: Any = worker.model
    assert "secret-value-123" not in tool_messages(model, 1)[0]


# ---- Step budget


def test_max_steps_stops_the_run_and_names_the_budget(tmp_path: Path) -> None:
    worker = role([call("echo", value=1)] * 50)
    result = go([RoleStep("s", worker, "x")], tmp_path, max_steps=4)
    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.detail == "used all 4 steps"
    assert result.state.steps_used == 4
    model: Any = worker.model
    assert len(model.requests) == 4


def test_steps_are_counted_across_roles(tmp_path: Path) -> None:
    first, second = role([DONE, DONE]), role([DONE, DONE])
    steps = [
        RoleStep("a", first, "x"),
        RoleStep("b", second, "x"),
        RoleStep("c", first, "x"),
    ]
    result = go(steps, tmp_path, max_steps=2)
    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.state.completed_steps == ["a", "b"]


# ---- Review rounds


def review_loop(reviewer: Role, planner: Role, needs: Callable[[RunContext], bool]) -> ReviewLoop:
    return ReviewLoop(
        "review",
        RoleStep("review_round", reviewer, "review"),
        RoleStep("revise_round", planner, "revise"),
        needs,
    )


def test_review_stops_at_max_rounds(tmp_path: Path) -> None:
    reviewer, planner = role([DONE] * 9), role([DONE] * 9)
    result = go([review_loop(reviewer, planner, lambda _: True)], tmp_path, max_review_rounds=2)
    assert result.stop_reason is StopReason.COMPLETED
    assert result.state.review_rounds == 2
    reviewer_model: Any = reviewer.model
    planner_model: Any = planner.model
    assert (len(reviewer_model.requests), len(planner_model.requests)) == (2, 2)


def test_review_stops_early_when_nothing_needs_revising(tmp_path: Path) -> None:
    reviewer, planner = role([DONE] * 9), role([DONE] * 9)
    result = go([review_loop(reviewer, planner, lambda _: False)], tmp_path)
    assert result.state.review_rounds == 1
    planner_model: Any = planner.model
    assert planner_model.requests == []


def test_zero_review_rounds_skips_review(tmp_path: Path) -> None:
    reviewer, planner = role([]), role([])
    result = go([review_loop(reviewer, planner, lambda _: True)], tmp_path, max_review_rounds=0)
    assert result.stop_reason is StopReason.COMPLETED
    assert result.state.steps_used == 0


def test_review_rounds_count_toward_the_step_budget(tmp_path: Path) -> None:
    reviewer, planner = role([DONE] * 9), role([DONE] * 9)
    result = go(
        [review_loop(reviewer, planner, lambda _: True)],
        tmp_path,
        max_review_rounds=5,
        max_steps=3,
    )
    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.state.steps_used == 3


# ---- Fix loop


class Harness:
    """A scripted Runner: `failures[test]` is how many runs that test fails before passing."""

    def __init__(
        self,
        failures: Mapping[str, int],
        labels: Mapping[str, Label] | None = None,
        changing_messages: bool = False,
    ) -> None:
        self.remaining = dict(failures)
        self.labels = labels or {}
        self.changing = changing_messages
        self.runs: list[frozenset[str] | None] = []
        self.fixes: list[str] = []
        self.labeled: list[list[str]] = []

    def execute(self, _: RunContext, ids: frozenset[str] | None) -> list[TestOutcome]:
        self.runs.append(ids)
        outcomes = []
        for test in sorted(self.remaining if ids is None else ids):
            left = self.remaining[test]
            self.remaining[test] = max(left - 1, 0)
            message = f"failed, {left} left" if self.changing else "assert 500 == 200"
            outcomes.append(TestOutcome(test, left == 0, "" if left == 0 else message))
        return outcomes

    def label(self, _: RunContext, failures: Sequence[TestOutcome]) -> dict[str, Label]:
        self.labeled.append([f.test_id for f in failures])
        return {f.test_id: self.labels.get(f.test_id, Label.TEST_BUG) for f in failures}

    def fix(self, _: RunContext, failure: TestOutcome) -> None:
        self.fixes.append(failure.test_id)

    def step(self) -> FixLoop:
        return FixLoop("fix", self.execute, self.label, self.fix)


def test_all_green_on_the_first_run_calls_no_labeling_or_fixing(tmp_path: Path) -> None:
    harness = Harness({"a": 0, "b": 0})
    result = go([harness.step()], tmp_path)
    assert result.stop_reason is StopReason.COMPLETED
    assert result.tests == {"a": "passed", "b": "passed"}
    assert harness.labeled == []
    assert harness.fixes == []


def test_only_fixed_tests_are_re_run(tmp_path: Path) -> None:
    harness = Harness({"a": 0, "b": 1})
    result = go([harness.step()], tmp_path)
    assert harness.runs == [None, frozenset({"b"})]
    assert result.tests == {"a": "passed", "b": "passed"}
    assert result.state.fix_attempts == {"b": 1}


def test_max_fix_attempts_one_patches_at_most_once_then_unresolved(tmp_path: Path) -> None:
    harness = Harness({"a": 99})
    result = go([harness.step()], tmp_path, max_fix_attempts=1)
    assert harness.fixes == ["a"]
    assert result.tests == {"a": "UNRESOLVED"}
    assert result.stop_reason is StopReason.COMPLETED


def test_default_three_attempts_then_unresolved_and_the_loop_moves_on(tmp_path: Path) -> None:
    harness = Harness({"stuck": 99, "ok": 1})
    result = go([harness.step()], tmp_path)
    assert harness.fixes.count("stuck") == 3
    assert result.tests == {"stuck": "UNRESOLVED", "ok": "passed"}


def test_zero_fix_attempts_never_patches(tmp_path: Path) -> None:
    harness = Harness({"a": 99})
    result = go([harness.step()], tmp_path, max_fix_attempts=0)
    assert harness.fixes == []
    assert result.tests == {"a": "UNRESOLVED"}


@pytest.mark.parametrize("label", [Label.API_BUG, Label.SPEC_DRIFT, Label.CONFLICT])
def test_writer_only_fixes_test_bugs(label: Label, tmp_path: Path) -> None:
    harness = Harness({"a": 99}, {"a": label})
    result = go([harness.step()], tmp_path)
    assert harness.fixes == []
    assert harness.runs == [None]
    assert result.tests == {"a": label.value}


def test_a_failure_left_unlabeled_becomes_unresolved(tmp_path: Path) -> None:
    harness = Harness({"a": 99})
    step = FixLoop("fix", harness.execute, lambda *_: {}, harness.fix)
    result = go([step], tmp_path)
    assert harness.fixes == []
    assert result.tests == {"a": "UNRESOLVED"}


def test_a_test_the_runner_stops_reporting_does_not_pass(tmp_path: Path) -> None:
    harness = Harness({"a": 1})
    runs = iter([[TestOutcome("a", False, "boom")], []])
    step = FixLoop("fix", lambda *_: next(runs), harness.label, harness.fix)
    result = go([step], tmp_path, max_fix_attempts=1)
    assert result.tests == {"a": "UNRESOLVED"}


def test_max_iterations_stops_and_leaves_open_tests_unresolved(tmp_path: Path) -> None:
    harness = Harness({"a": 99}, changing_messages=True)
    result = go([harness.step()], tmp_path, max_iterations=2, max_fix_attempts=50)
    assert result.stop_reason is StopReason.MAX_ITERATIONS
    assert result.detail == "used all 2 iterations"
    assert len(harness.runs) == 2
    assert result.tests == {"a": "UNRESOLVED"}


def test_no_progress_for_three_iterations_stops_the_run(tmp_path: Path) -> None:
    harness = Harness({"a": 99})
    result = go([harness.step()], tmp_path, max_fix_attempts=50)
    assert result.stop_reason is StopReason.NO_PROGRESS
    assert len(harness.runs) == 4  # A baseline, then three identical passes.
    assert result.tests == {"a": "UNRESOLVED"}


def test_a_changing_failure_counts_as_progress(tmp_path: Path) -> None:
    harness = Harness({"a": 5}, changing_messages=True)
    result = go([harness.step()], tmp_path, max_fix_attempts=50)
    assert result.stop_reason is StopReason.COMPLETED
    assert result.tests == {"a": "passed"}


def test_a_closing_test_counts_as_progress(tmp_path: Path) -> None:
    # "stuck" repeats its message, but other tests close on passes 2 to 4.
    harness = Harness({"stuck": 7, "b": 1, "c": 2, "d": 3})
    result = go([harness.step()], tmp_path, max_fix_attempts=50, no_progress_limit=2)
    assert result.tests["d"] == "passed"
    assert result.state.iterations >= 4


def test_step_budget_during_fixing_leaves_open_tests_unresolved(tmp_path: Path) -> None:
    writer = role([DONE])
    harness = Harness({"a": 99, "b": 99})

    def fix(ctx: RunContext, _: TestOutcome) -> None:
        ctx.run_role(writer, "fix it")

    step = FixLoop("fix", harness.execute, harness.label, fix)
    result = go([step], tmp_path, max_steps=1)
    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.tests == {"a": "UNRESOLVED", "b": "UNRESOLVED"}


@pytest.mark.parametrize("attempts", [0, 1, 3, 7, 50])
@pytest.mark.parametrize("iterations", [1, 2, 10])
def test_every_failure_ends_closed_whatever_the_limits(
    attempts: int, iterations: int, tmp_path: Path
) -> None:
    harness = Harness({"p": 0, "q": 2, "r": 99}, {"s": Label.API_BUG})
    harness.remaining["s"] = 99
    result = go([harness.step()], tmp_path, max_fix_attempts=attempts, max_iterations=iterations)
    assert set(result.tests) == {"p", "q", "r", "s"}
    assert result.state.open_tests == []
    assert all(harness.fixes.count(test) <= attempts for test in harness.fixes)
    assert "s" not in harness.fixes


# ---- Models, errors, usage, checkpoints and trace


class Failing:
    name = "fake/failing"

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Reply:
        raise ModelError("provider down")


def test_a_provider_failure_stops_the_run_with_a_reason(tmp_path: Path) -> None:
    worker = Role("custom", "p", (), Failing())
    result = go([RoleStep("s", worker, "x")], tmp_path)
    assert result.stop_reason is StopReason.MODEL_ERROR
    assert result.detail == "provider down"
    assert trace(tmp_path)[1]["kind"] == "model_error"


def test_usage_is_summed_and_unpriced_calls_are_reported(tmp_path: Path) -> None:
    worker = role(
        [
            Reply(tool_calls=call("echo", value=1).tool_calls, usage=Usage(10, 5, 0.5)),
            Reply(text="done", usage=Usage(1, 2, None)),
        ]
    )
    result = go([RoleStep("s", worker, "x")], tmp_path)
    assert (result.state.input_tokens, result.state.output_tokens) == (11, 7)
    assert result.state.cost_usd == 0.5
    assert result.state.unpriced_calls == 1
    assert "tokens: 11 in, 7 out" in result.summary
    assert "$0.5000 (plus 1 calls with no known price)" in result.summary


def test_summary_without_unpriced_calls(tmp_path: Path) -> None:
    worker = role([Reply(text="done", usage=Usage(1, 1, 0.1))])
    assert "no known price" not in go([RoleStep("s", worker, "x")], tmp_path).summary


def test_state_is_checkpointed_after_every_step_and_at_the_end(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def snapshot(ctx: RunContext) -> None:
        seen.append(load_json(RunState, ctx.state_path).completed_steps)

    steps = [CodeStep("one", snapshot), CodeStep("two", snapshot)]
    result = run(Pipeline(tuple(steps)), Budget(), workdir=tmp_path, spec_hash="abc")
    assert seen == [[], ["one"]]
    saved = load_json(RunState, tmp_path / ".workflowval" / "state.json")
    assert saved.spec_hash == "abc"
    assert saved.completed_steps == ["one", "two"]
    assert saved.stop_reason == "completed"
    assert saved == result.state


def test_a_killed_run_leaves_its_last_checkpoint(tmp_path: Path) -> None:
    def die(_: RunContext) -> None:
        raise KeyboardInterrupt

    steps = [CodeStep("one", lambda _: None), CodeStep("two", die)]
    with pytest.raises(KeyboardInterrupt):
        go(steps, tmp_path)
    assert load_json(RunState, tmp_path / ".workflowval" / "state.json").completed_steps == ["one"]


def test_trace_logs_each_call_with_size_tokens_and_cost(tmp_path: Path) -> None:
    worker = role(
        [Reply(tool_calls=call("echo", value=1).tool_calls, usage=Usage(3, 4, 0.5)), DONE]
    )
    go([RoleStep("s", worker, "x")], tmp_path)
    events = trace(tmp_path)
    assert [e["kind"] for e in events] == [
        "run_start",
        "model_call",
        "tool_call",
        "model_call",
        "run_end",
    ]
    model_call, tool_call = events[1], events[2]
    assert (model_call["input_tokens"], model_call["output_tokens"], model_call["cost_usd"]) == (
        3,
        4,
        0.5,
    )
    assert model_call["tool_calls"] == ["echo"]
    assert tool_call["arguments"] == {"value": 1}
    assert tool_call["result_size"] == len("echo 1")
    assert events[-1]["stop_reason"] == "completed"


class ProbeArgs(BaseModel):
    headers: dict[str, str]


def leaky(args: ProbeArgs, _: Any) -> str:
    """Return a response that contains a token."""
    return f"server echoed Authorization: {args.headers['Authorization']}"


def test_secrets_never_reach_the_trace_or_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLANTED_API_KEY", "planted-env-secret")
    tool = Tool("leaky", "Leak.", ProbeArgs, leaky)
    arguments = {"headers": {"Authorization": "Bearer abc123def456"}, "note": "planted-env-secret"}
    model = FakeModel([call("leaky", **arguments), DONE])
    go([RoleStep("s", Role("custom", "p", (tool,), model), "x")], tmp_path)
    text = (tmp_path / ".workflowval" / "trace.jsonl").read_text()
    assert "abc123def456" not in text
    assert "planted-env-secret" not in text
    assert "[REDACTED]" in text
    assert "abc123def456" not in tool_messages(model, 1)[0]


def test_verbose_echoes_the_trace_to_the_console(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(Pipeline(()), Budget(), workdir=tmp_path, verbose=True)
    assert '"kind": "run_start"' in capsys.readouterr().err


def test_identical_scripts_give_identical_results(tmp_path: Path) -> None:
    def once(folder: Path) -> Any:
        worker = role([call("echo", value=1), DONE])
        harness = Harness({"a": 1, "b": 99})
        return go([RoleStep("s", worker, "x"), harness.step()], folder, max_fix_attempts=2)

    first, second = once(tmp_path / "a"), once(tmp_path / "b")
    assert first.tests == second.tests
    assert first.state == second.state
