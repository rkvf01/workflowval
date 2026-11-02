"""The pipeline runner: budgets, stop rules, per-role tool limits and checkpoints."""

from __future__ import annotations

from workflowval.loop.runner import (
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
    TestOutcome,
    run,
)
from workflowval.loop.trace import Tracer
from workflowval.models.base import Model
from workflowval.prompts import load_prompt
from workflowval.tools import ROLE_TOOLS, TOOLS


def make_role(name: str, model: Model) -> Role:
    """Build a pipeline role from its prompt file and the tools listed for it."""
    if name not in ROLE_TOOLS:
        raise ValueError(f"unknown role {name!r}; roles are {sorted(ROLE_TOOLS)}")
    tools = tuple(TOOLS[tool] for tool in sorted(ROLE_TOOLS[name]))
    return Role(name, load_prompt(name), tools, model)


__all__ = [
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
    "TestOutcome",
    "Tracer",
    "make_role",
    "run",
]
