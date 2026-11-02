"""Deterministic checks and failure classification. Arrives in M2."""

from __future__ import annotations

from workflowval.schemas import TestResult, Verdict


def classify(failure: TestResult) -> Verdict:
    """Label one failure."""
    raise NotImplementedError("triage.classify arrives in M2")
