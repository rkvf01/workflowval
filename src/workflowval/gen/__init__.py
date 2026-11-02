"""Pytest generation. Arrives in M1."""

from __future__ import annotations

from dataclasses import dataclass

from workflowval.schemas import Workflow


@dataclass(frozen=True)
class TestFile:
    """A generated test file."""

    __test__ = False

    path: str
    source: str


def generate(workflow: Workflow) -> TestFile:
    """Turn a workflow into a pytest file."""
    raise NotImplementedError("gen.generate arrives in M1")
