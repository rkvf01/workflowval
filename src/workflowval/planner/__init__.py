"""Turns the graph and docs into workflow files, through the Planner role (FR-4)."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from pathlib import Path

from workflowval.loop import TASK_CALL_LIMIT, Role, RunContext, TaskLimitReached
from workflowval.prompts import render_prompt
from workflowval.schemas import Workflow, load_json
from workflowval.spec import SpecGraph

CALLS_PER_OPERATION = 3


def load_workflows(workdir: Path) -> list[Workflow]:
    """Read every workflow file. An invalid file raises, naming nothing silently dropped."""
    return [load_json(Workflow, path) for path in sorted((workdir / "workflows").glob("*.json"))]


def uncovered_operations(
    workflows: Iterable[Workflow], graph: SpecGraph, ignore: Collection[str] = ()
) -> list[str]:
    """List operations no workflow calls. A login operation is used by the fixtures instead."""
    used = {step.op for w in workflows for step in [*w.steps, *w.cleanup]}
    return [
        op.operation_id
        for op in graph.operations
        if op.operation_id not in used and op.operation_id not in ignore
    ]


def plan_call_limit(graph: SpecGraph) -> int:
    """A planning task's call limit, which grows with the size of the API."""
    return TASK_CALL_LIMIT + CALLS_PER_OPERATION * len(graph.operations)


def plan(
    ctx: RunContext, role: Role, graph: SpecGraph, ignore: Collection[str] = ()
) -> list[Workflow]:
    """Have the Planner write workflow files, keeping locked ones, and return all of them.

    A model can stop early, so code checks coverage and gives it one more chance to fill gaps.
    """
    operations = "\n".join(f"- {op.operation_id}: {op.method} {op.path}" for op in graph.operations)
    existing = load_workflows(ctx.workdir)
    locked = [workflow.id for workflow in existing if workflow.locked]
    limit = plan_call_limit(graph)
    try:
        ctx.run_role(
            role,
            render_prompt("task_plan", operations=operations, locked=", ".join(locked) or "none"),
            max_calls=limit,
        )
        gaps = uncovered_operations(load_workflows(ctx.workdir), graph, ignore)
        if gaps:
            ctx.run_role(
                role, render_prompt("task_plan_gaps", operations=", ".join(gaps)), max_calls=limit
            )
    except TaskLimitReached:
        pass  # Keep what was written. Whatever's left out is reported as not covered.
    return load_workflows(ctx.workdir)
