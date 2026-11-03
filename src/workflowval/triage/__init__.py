"""Failure labeling: deterministic checks, then the Reviewer role decides (FR-8)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflowval.loop import Role, RunContext, TestOutcome
from workflowval.loop.stats import workflow_of
from workflowval.prompts import render_prompt
from workflowval.schemas import Label
from workflowval.spec import ResponseChecker, SpecGraph


def classify(status: int | None, mismatches: Sequence[str]) -> str:
    """Run the deterministic checks on a failure. This is a hint: the Reviewer decides the label."""
    if status is not None and status >= 500:
        return f"the API returned {status}, so this is likely an API_BUG"
    if mismatches:
        return "the response breaks the spec, so this is likely SPEC_DRIFT: " + "; ".join(
            mismatches
        )
    return "no automatic signal"


@dataclass
class MismatchGroup:
    """One way responses break the spec, with every test and operation that showed it."""

    key: str
    message: str
    operations: set[str] = field(default_factory=set)
    tests: set[str] = field(default_factory=set)
    example: dict[str, Any] = field(default_factory=dict)

    @property
    def finding_id(self) -> str:
        """A stable id for this mismatch, used as the key of its verdict."""
        digest = hashlib.sha256(self.key.encode()).hexdigest()[:10]
        return f"finding:{digest}"


def group_mismatches(
    outcomes: Mapping[str, Sequence[Mapping[str, Any]]], checker: ResponseChecker, graph: SpecGraph
) -> list[MismatchGroup]:
    """Group mismatches across tests, so a bug seen by every test is reviewed once."""
    groups: dict[tuple[str, int], MismatchGroup] = {}
    for test_id, calls in sorted(outcomes.items()):
        for call in calls:
            for mismatch in checker.check_call(call):
                operation = graph.operation_at(str(call["method"]), str(call["template"]))
                name = operation.operation_id if operation else str(call["template"])
                # The same field can mean different things under different statuses.
                group = groups.setdefault(
                    (mismatch.key, int(call["status"])),
                    MismatchGroup(f"{mismatch.key}|{call['status']}", mismatch.message),
                )
                group.operations.add(name)
                group.tests.add(test_id)
                group.example = group.example or dict(call)
    return [groups[key] for key in sorted(groups)]


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
    ctx: RunContext,
    reviewer: Role,
    failures: Sequence[TestOutcome],
    credentials: str,
    hints: Mapping[str, str] | None = None,
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
                hint=(hints or {}).get(failure.test_id, "no automatic signal"),
            ),
            workflow_id,
        )
        verdict = read_verdicts(ctx.workdir).get(failure.test_id)
        if verdict is not None:
            labels[failure.test_id] = verdict.label
    return labels


def review_finding(
    ctx: RunContext, reviewer: Role, group: MismatchGroup, credentials: str
) -> StoredVerdict | None:
    """Ask the Reviewer to rule on one mismatch. No recorded verdict leaves it unreviewed."""
    example = group.example
    ctx.run_role(
        reviewer,
        render_prompt(
            "task_finding",
            finding_id=group.finding_id,
            message=group.message,
            operations=", ".join(sorted(group.operations)),
            status=str(example.get("status")),
            body=str(example.get("raw_body") or "")[:1500],
            pointer=_pointer(example),
            credentials=credentials,
        ),
    )
    return read_verdicts(ctx.workdir).get(group.finding_id)


def _pointer(call: Mapping[str, Any]) -> str:
    template = str(call.get("template", "")).replace("~", "~0").replace("/", "~1")
    return f"/paths/{template}/{str(call.get('method', '')).lower()}/responses/{call.get('status')}"


def request_report(
    ctx: RunContext, reporter: Role, *, label: str, reason: str, test_id: str, tests: Sequence[str]
) -> None:
    """Have the Reviewer write one bug report for one root cause."""
    ctx.run_role(
        reporter,
        render_prompt(
            "task_report",
            label=label,
            reason=reason,
            test_id=test_id,
            tests=", ".join(tests),
            kind="conflict" if label == "CONFLICT" else "api_bug",
        ),
    )


def request_patch(
    ctx: RunContext, reporter: Role, *, reason: str, target: str, call: Mapping[str, Any]
) -> None:
    """Have the Reviewer propose one spec patch for one root cause."""
    ctx.run_role(
        reporter,
        render_prompt(
            "task_patch",
            reason=reason,
            target=target,
            pointer=_pointer(call),
            body=str(call.get("raw_body") or "")[:1500],
        ),
    )
