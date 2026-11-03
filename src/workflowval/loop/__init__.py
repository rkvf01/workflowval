"""The pipeline runner: budgets, stop rules, per-role tool limits and checkpoints."""

from __future__ import annotations

from workflowval.loop.runner import (
    TASK_CALL_LIMIT,
    Budget,
    CodeStep,
    FixLoop,
    Pipeline,
    ReviewLoop,
    Role,
    RoleStep,
    RunContext,
    RunResult,
    StopReason,
    StopRun,
    TaskLimitReached,
    TestOutcome,
    run,
)
from workflowval.loop.trace import Tracer
from workflowval.models.base import Model
from workflowval.prompts import load_prompt
from workflowval.tools import ROLE_TOOLS, TOOLS


def make_role(name: str, model: Model, only: frozenset[str] | None = None) -> Role:
    """Build a pipeline role from its prompt file and its tools, optionally fewer of them."""
    if name not in ROLE_TOOLS:
        raise ValueError(f"unknown role {name!r}; roles are {sorted(ROLE_TOOLS)}")
    allowed = ROLE_TOOLS[name] if only is None else ROLE_TOOLS[name] & only
    tools = tuple(TOOLS[tool] for tool in sorted(allowed))
    return Role(name, load_prompt(name), tools, model)


__all__ = [
    "TASK_CALL_LIMIT",
    "Budget",
    "CodeStep",
    "FixLoop",
    "Pipeline",
    "ReviewLoop",
    "Role",
    "RoleStep",
    "RunContext",
    "RunResult",
    "StopReason",
    "StopRun",
    "TaskLimitReached",
    "TestOutcome",
    "Tracer",
    "make_role",
    "run",
]
