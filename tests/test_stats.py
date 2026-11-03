from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from workflowval.loop import (
    Budget,
    CodeStep,
    FixLoop,
    Pipeline,
    Role,
    RoleStep,
    RunContext,
    make_role,
    run,
)
from workflowval.loop.runner import StopReason, TestOutcome
from workflowval.loop.stats import (
    StatsCollector,
    append_history,
    read_history,
    suggest_max_steps,
    workflow_of,
)
from workflowval.models import FakeModel, Reply, ToolCall, Usage, call
from workflowval.models.base import ModelError
from workflowval.schemas import Label, RunStats, StatsTotal, load_json

DONE = Reply(text="done")
WORKFLOW = {
    "id": "wf_a",
    "source": "planner",
    "basis": "documented",
    "rule": "Pets can be read back",
    "steps": [{"op": "getPet", "expect": {"status": 200}}],
}


def stats_file(tmp_path: Path) -> RunStats:
    return load_json(RunStats, tmp_path / ".workflowval" / "stats.json")


def go(steps: list[Any], tmp_path: Path, **budget: Any) -> Any:
    return run(Pipeline(tuple(steps)), Budget(**budget), workdir=tmp_path)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"workflow_id": "wf_a"}, "wf_a"),
        ({"workflow": {"id": "wf_a"}}, "wf_a"),
        ({"test_id": "tests/wf/test_wf_a.py::test_wf_a"}, "wf_a"),
        ({"test_id": "test_wf_a.py"}, "wf_a"),
        ({"test_id": "some/other_test_thing"}, None),
        ({"workflow_id": "../secret"}, None),
        ({"workflow_id": 1}, None),
        ({"workflow": "wf_a"}, None),
        ({"operation_id": "getPet"}, None),
        ({}, None),
    ],
)
def test_workflow_of(arguments: dict[str, Any], expected: str | None) -> None:
    assert workflow_of(arguments) == expected


def test_calls_are_credited_to_the_workflow_their_arguments_name(tmp_path: Path) -> None:
    model = FakeModel(
        [
            call("read_spec"),
            call("write_workflow", workflow=WORKFLOW),
            call("read_workflows", workflow_id="wf_b"),
            call("read_workflows", workflow_id="wf_b"),
            DONE,
        ]
    )
    result = go([RoleStep("plan", make_role("planner", model), "x")], tmp_path)
    stats = stats_file(tmp_path)
    assert stats.run.tool_calls == {"read_spec": 1}
    assert stats.run.model_calls == 2  # read_spec, and the final answer.
    assert stats.workflows["wf_a"].tool_calls == {"write_workflow": 1}
    assert stats.workflows["wf_a"].model_calls == 1
    assert stats.workflows["wf_b"].tool_calls == {"read_workflows": 2}
    assert stats.workflows["wf_b"].model_calls == 2
    assert (stats.total.model_calls, stats.total.tool_calls, stats.total.limit) == (5, 4, 200)
    assert stats == result.stats.model_copy(update={"finished_at": stats.finished_at})


def test_a_scoped_role_step_credits_everything_to_its_workflow(tmp_path: Path) -> None:
    model = FakeModel([call("read_spec"), call("read_tests", workflow_id="other"), DONE])
    step = RoleStep("fix", make_role("writer", model), "x", workflow_id="wf_a")
    go([step], tmp_path)
    stats = stats_file(tmp_path)
    assert list(stats.workflows) == ["wf_a"]
    assert stats.workflows["wf_a"].model_calls == 3
    assert stats.workflows["wf_a"].tool_calls == {"read_spec": 1, "read_tests": 1}
    assert stats.run == type(stats.run)()


def test_fix_callbacks_can_scope_their_calls(tmp_path: Path) -> None:
    writer = make_role("writer", FakeModel([call("read_spec"), DONE]))
    runs = iter([[TestOutcome("t", False, "boom")], [TestOutcome("t", True)]])

    def fix(ctx: RunContext, _: TestOutcome) -> None:
        ctx.run_role(writer, "fix it", workflow_id="wf_a")

    step = FixLoop("fix", lambda *_: next(runs), lambda *_: {"t": Label.TEST_BUG}, fix)
    go([step], tmp_path)
    assert stats_file(tmp_path).workflows["wf_a"].model_calls == 2


def test_a_reply_touching_two_workflows_counts_its_model_call_to_the_run() -> None:
    collector = StatsCollector(10)
    calls = [
        ToolCall("1", "read_tests", {"workflow_id": "a"}),
        ToolCall("2", "read_tests", {"workflow_id": "b"}),
    ]
    collector.record_reply(None, calls, {"read_tests"})
    stats = collector.snapshot()
    assert stats.run.model_calls == 1
    assert stats.workflows["a"].tool_calls == {"read_tests": 1}
    assert stats.workflows["b"].model_calls == 0


def test_invented_tool_names_are_not_stored(tmp_path: Path) -> None:
    model = FakeModel([call("sk-looks-like-a-secret"), DONE])
    go([RoleStep("s", make_role("writer", model), "x")], tmp_path)
    assert stats_file(tmp_path).run.tool_calls == {"<unknown>": 1}


def test_refused_tools_are_counted_by_name(tmp_path: Path) -> None:
    model = FakeModel([call("write_workflow", workflow=WORKFLOW), DONE])
    go([RoleStep("s", make_role("writer", model), "x")], tmp_path)
    assert stats_file(tmp_path).workflows["wf_a"].tool_calls == {"write_workflow": 1}


def test_stats_never_hold_arguments(tmp_path: Path) -> None:
    model = FakeModel([call("read_docs", path="planted-argument-value"), DONE])
    go([RoleStep("s", make_role("planner", model), "x")], tmp_path)
    for name in ("stats.json", "stats_history.jsonl"):
        assert "planted-argument-value" not in (tmp_path / ".workflowval" / name).read_text()


def test_total_model_calls_always_equal_the_steps_used(tmp_path: Path) -> None:
    model = FakeModel([call("read_spec")] * 9)
    result = go([RoleStep("s", make_role("writer", model), "x")], tmp_path, max_steps=4)
    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.stats.total.model_calls == result.state.steps_used == 4
    assert result.stats.stop_reason == "max_steps"


def test_a_failed_model_call_still_counts(tmp_path: Path) -> None:
    class Failing:
        name = "fake/failing"

        def complete(self, *_: Any) -> Reply:
            raise ModelError("down")

    result = go([RoleStep("s", Role("custom", "p", (), Failing()), "x", "wf_a")], tmp_path)
    assert result.stats.total.model_calls == result.state.steps_used == 1
    assert result.stats.workflows["wf_a"].model_calls == 1


def test_stats_are_on_disk_before_the_run_ends(tmp_path: Path) -> None:
    model = FakeModel([call("read_spec"), DONE])
    step = RoleStep("first", make_role("writer", model), "x")

    def die(_: RunContext) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        go([step, CodeStep("second", die)], tmp_path)
    assert stats_file(tmp_path).total.model_calls == 2
    assert not (tmp_path / ".workflowval" / "stats_history.jsonl").exists()


def test_each_finished_run_adds_one_history_line(tmp_path: Path) -> None:
    for _ in range(3):
        go([RoleStep("s", make_role("writer", FakeModel([DONE])), "x")], tmp_path)
    history = read_history(tmp_path / ".workflowval" / "stats_history.jsonl")
    assert len(history) == 3
    assert all(entry.stop_reason == "completed" and entry.finished_at for entry in history)


def test_stats_json_is_replaced_each_run_but_history_keeps_all(tmp_path: Path) -> None:
    go([RoleStep("s", make_role("writer", FakeModel([DONE, DONE])), "x")], tmp_path)
    go([RoleStep("s", make_role("writer", FakeModel([call("read_spec"), DONE])), "x")], tmp_path)
    assert stats_file(tmp_path).total.tool_calls == 1
    assert len(read_history(tmp_path / ".workflowval" / "stats_history.jsonl")) == 2


def test_missing_history_is_empty(tmp_path: Path) -> None:
    assert read_history(tmp_path / "none.jsonl") == []


def test_bad_history_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    append_history(past(10), path)
    with path.open("a") as handle:
        handle.write('{"total": {"model_ca\n')
        handle.write(json.dumps({"unexpected": 1}) + "\n")
    append_history(past(20), path)
    assert [entry.total.model_calls for entry in read_history(path)] == [10, 20]


def past(steps: int, reason: str = "completed", limit: int = 200) -> RunStats:
    return RunStats(total=StatsTotal(model_calls=steps, limit=limit), stop_reason=reason)


def test_no_suggestion_before_five_usable_runs() -> None:
    assert suggest_max_steps([past(84)] * 4, 200) is None
    assert suggest_max_steps([past(84)] * 4 + [past(1, "model_error")], 200) is None


def test_suggestion_is_one_and_a_half_times_the_largest_rounded_up() -> None:
    message = suggest_max_steps([past(40)] * 4 + [past(84)], 200)
    assert message == "the largest of your last 5 runs used 84 steps; consider --max-steps 130"


def test_suggestion_can_say_raise_when_runs_come_close() -> None:
    message = suggest_max_steps([past(190)] * 5, 200)
    assert message is not None
    assert "--max-steps 290" in message


def test_hitting_the_limit_suggests_raising_it() -> None:
    message = suggest_max_steps([past(40)] * 4 + [past(200, "max_steps")], 200)
    assert message == "1 of your last 5 runs hit the step limit of 200; consider --max-steps 300"


def test_a_hit_on_an_older_lower_limit_does_not_count_as_a_hit() -> None:
    message = suggest_max_steps([past(40)] * 4 + [past(100, "max_steps", limit=100)], 200)
    assert message == "the largest of your last 5 runs used 100 steps; consider --max-steps 150"


@pytest.mark.parametrize("reason", ["model_error", "invalid_output"])
def test_runs_that_ended_early_are_ignored(reason: str) -> None:
    assert suggest_max_steps([past(40)] * 5 + [past(500, reason)], 200) == (
        "the largest of your last 5 runs used 40 steps; consider --max-steps 60"
    )


def test_only_the_last_twenty_runs_count() -> None:
    message = suggest_max_steps([past(900)] + [past(40)] * 20, 200)
    assert message is not None
    assert "--max-steps 60" in message


def test_very_small_runs_keep_a_floor() -> None:
    message = suggest_max_steps([past(1)] * 5, 200)
    assert message is not None
    assert "--max-steps 10" in message


def test_no_suggestion_when_the_limit_is_already_right() -> None:
    assert suggest_max_steps([past(40)] * 5, 60) is None


def test_suggestion_appears_in_the_summary_and_never_changes_the_limit(tmp_path: Path) -> None:
    results = []
    for _ in range(5):
        step = RoleStep("s", make_role("writer", FakeModel([DONE])), "x")
        results.append(go([step], tmp_path, max_steps=50))
    assert results[3].suggestion is None
    assert results[3].summary.count("\n") == 0
    assert results[4].suggestion == (
        "the largest of your last 5 runs used 1 step; consider --max-steps 10"
    )
    assert results[4].suggestion in results[4].summary
    assert results[4].stats.total.limit == 50
    trace = (tmp_path / ".workflowval" / "trace.jsonl").read_text().splitlines()
    assert "consider --max-steps 10" in json.loads(trace[-1])["suggestion"]


def test_cost_is_tracked_per_workflow_and_role(tmp_path: Path) -> None:
    planner = FakeModel(
        [
            Reply(
                tool_calls=call("write_workflow", workflow=WORKFLOW).tool_calls,
                usage=Usage(1, 1, 0.5),
            ),
            Reply(text="done", usage=Usage(1, 1, 0.25)),
        ]
    )
    writer = FakeModel([Reply(text="done", usage=Usage(1, 1, 0.125))])
    steps = [
        RoleStep("plan", make_role("planner", planner), "x"),
        RoleStep("fix", make_role("writer", writer), "x", workflow_id="wf_a"),
    ]
    stats = go(steps, tmp_path).stats
    assert stats.workflows["wf_a"].cost_usd == 0.625
    assert stats.run.cost_usd == 0.25
    assert stats.roles["planner"].cost_usd == 0.75
    assert stats.roles["planner"].tool_calls == {"write_workflow": 1}
    assert stats.roles["writer"].cost_usd == 0.125
    assert stats.total.cost_usd == 0.875


def test_summary_says_when_cost_is_not_tracked(tmp_path: Path) -> None:
    step = RoleStep("s", make_role("writer", FakeModel([DONE])), "x")
    result = run(Pipeline((step,)), Budget(), workdir=tmp_path, cost_tracked=False)
    assert "cost: not tracked" in result.summary
