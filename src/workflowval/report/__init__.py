"""Markdown, JSON and JUnit writers. Arrives in M2."""

from __future__ import annotations

from workflowval.schemas import RunResults


def write_reports(run: RunResults) -> None:
    """Write report.md, results.json and JUnit XML."""
    raise NotImplementedError("report.write_reports arrives in M2")
