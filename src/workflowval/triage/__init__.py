"""Failure labeling by the Reviewer role (FR-8). Deterministic checks arrive in M2."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workflowval.loop import Role, RunContext, TestOutcome
from workflowval.loop.stats import workflow_of
from workflowval.prompts import render_prompt
from workflowval.schemas import Label, TestResult, Verdict


def classify(failure: TestResult) -> Verdict:
    """Run the deterministic checks on one failure before any model sees it."""
    raise NotImplementedError("triage.classify arrives in M2")


@dataclass(frozen=True)
class StoredVerdict:
    """A verdict as the Reviewer recorded it."""

    label: Label
    confidence: str
    reason: str
    fix_target: str | None


def _path(workdir: Path) -> Path:
    return workdir / ".workflowval" / "verdicts.json"


def read_verdicts(workdir: Path) -> dict[str, StoredVerdict]:
    """Read the Reviewer's verdicts."""
    path = _path(workdir)
    if not path.is_file():
        return {}
    raw: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return {
        test_id: StoredVerdict(
            Label(item["label"]), item["confidence"], item["reason"], item.get("fix_target")
        )
        for test_id, item in raw.items()
    }


def _forget(workdir: Path, test_id: str) -> None:
    path = _path(workdir)
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop(test_id, None)
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")


def label_failures(
    ctx: RunContext, reviewer: Role, failures: Sequence[TestOutcome], credentials: str
) -> Mapping[str, Label]:
    """Ask the Reviewer to judge each failure against the spec and docs, one at a time.

    A failure with no recorded verdict stays unlabeled, which the loop turns into UNRESOLVED.
    """
    labels: dict[str, Label] = {}
    for failure in failures:
        _forget(ctx.workdir, failure.test_id)
        workflow_id = workflow_of({"test_id": failure.test_id})
        ctx.run_role(
            reviewer,
            render_prompt(
                "task_triage",
                test_id=failure.test_id,
                workflow_id=workflow_id or "",
                failure=failure.message,
                credentials=credentials,
            ),
            workflow_id,
        )
        verdict = read_verdicts(ctx.workdir).get(failure.test_id)
        if verdict is not None:
            labels[failure.test_id] = verdict.label
    return labels
