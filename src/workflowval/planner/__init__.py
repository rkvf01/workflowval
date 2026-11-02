"""Turns the graph and docs into workflow files. Arrives in M1."""

from __future__ import annotations

from workflowval.schemas import Workflow
from workflowval.spec import SpecGraph


def plan(graph: SpecGraph, docs: list[str]) -> list[Workflow]:
    """Propose workflows from the spec graph and docs."""
    raise NotImplementedError("planner.plan arrives in M1")
