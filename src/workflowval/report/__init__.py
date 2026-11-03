"""Writes report.md, JUnit XML and results.json (FR-11). Bug reports come from a Reviewer tool."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from workflowval.redact import redact_text
from workflowval.schemas import (
    Basis,
    Decision,
    Finding,
    Label,
    RunResults,
    TestResult,
    TestStatus,
    atomic_write_text,
    save_json,
)


def _test_line(test: TestResult) -> str:
    verdict = test.verdict
    if verdict is None:
        return f"- `{test.workflow_id}`: {test.failing_assertion or test.status.value}"
    expected = ", expected to fail" if test.status is TestStatus.XFAILED else ""
    filed = f" Report: `{verdict.action.path}`." if verdict.action.path else ""
    return (
        f"- `{test.workflow_id}` ({verdict.label.value}, {verdict.confidence.value} confidence"
        f"{expected}): {verdict.reason}{filed}"
    )


def _finding_line(finding: Finding) -> str:
    label = finding.label.value if finding.label else "not reviewed"
    reason = f" {finding.reason}" if finding.reason else ""
    filed = f" Report: `{finding.action.path}`." if finding.action.path else ""
    operations = ", ".join(finding.operations)
    return f"- **{label}**, on {operations}: {finding.message}.{reason}{filed}"


def _journey_lines(run: RunResults) -> list[str]:
    lines = []
    for review in run.reviews:
        when = "after the tests" if review.after_tests else "before the tests"
        for journey in review.missing_journeys:
            answer = journey.answer
            if answer is None:
                outcome = "not answered"
            elif answer.decision is Decision.ACCEPTED:
                outcome = f"added as `{answer.workflow_id}`"
            else:
                outcome = f"rejected: {answer.reason}"
            lines.append(f"- {journey.rule} ({journey.user}, {when}): {outcome}.")
    return lines


def _section(title: str, lines: list[str]) -> list[str]:
    return [f"## {title}", "", *lines, ""] if lines else []


def _is_problem(test: TestResult) -> bool:
    return test.status not in {TestStatus.PASSED, TestStatus.SKIPPED}


def render_report(run: RunResults) -> str:
    """Render the human summary, with every label counted."""
    problems = [test for test in run.tests if _is_problem(test)]
    conflicts = [test for test in problems if test.verdict and test.verdict.label is Label.CONFLICT]
    documented = [
        test for test in problems if test not in conflicts and test.basis is Basis.DOCUMENTED
    ]
    assumed = [test for test in problems if test not in conflicts and test.basis is Basis.ASSUMED]
    conflict_findings = [f for f in run.findings if f.label is Label.CONFLICT]
    confirmed = [
        f
        for f in run.findings
        if f.label is None or f.label not in {Label.TEST_BUG, Label.CONFLICT}
    ]
    dismissed = [f for f in run.findings if f.label is Label.TEST_BUG]
    lines = [
        "# workflowval report",
        "",
        run.summary(),
        "",
        f"Stopped: {run.stop_reason}{f' ({run.stop_detail})' if run.stop_detail else ''}.",
        f"Cost: ${run.cost_usd:.4f}, with {run.input_tokens} input and {run.output_tokens} output "
        "tokens.",
        "",
    ]
    if run.label_counts:
        lines += ["| Label | Count |", "| --- | --- |"]
        lines += [f"| {label} | {count} |" for label, count in sorted(run.label_counts.items())]
        lines.append("")
    lines += _section(
        "Open conflicts between the spec and docs",
        [*map(_test_line, conflicts), *map(_finding_line, conflict_findings)],
    )
    lines += _section("Failures on documented rules", [_test_line(test) for test in documented])
    lines += _section(
        "Failures on assumed rules",
        [
            *(
                ["These rules were inferred, so readers should weigh them accordingly.", ""]
                if assumed
                else []
            ),
            *map(_test_line, assumed),
        ],
    )
    lines += _section("Responses that break the spec", [_finding_line(f) for f in confirmed])
    lines += _section(
        "Mismatches the Reviewer dismissed", [_finding_line(finding) for finding in dismissed]
    )
    lines += _section("User journeys the Reviewer proposed", _journey_lines(run))
    lines += _section(
        "Operations no workflow calls", [f"- `{op}`" for op in run.uncovered_operations]
    )
    lines += _section(
        "Tasks stopped at their call limit, so their work may be incomplete",
        [f"- {task}" for task in run.tasks_stopped],
    )
    return redact_text("\n".join(lines).rstrip() + "\n")


def render_junit(run: RunResults) -> str:
    """Render JUnit XML: an expected failure is skipped, and anything unreviewed is a failure."""
    failed = sum(t.status in {TestStatus.FAILED, TestStatus.ERROR} for t in run.tests)
    skipped = sum(
        t.status not in {TestStatus.FAILED, TestStatus.ERROR, TestStatus.PASSED} for t in run.tests
    )
    suite = ET.Element(
        "testsuite",
        name="workflowval",
        tests=str(len(run.tests)),
        failures=str(failed),
        skipped=str(skipped),
    )
    for test in run.tests:
        case = ET.SubElement(
            suite, "testcase", classname="workflowval", name=test.id, time=f"{test.duration_s:.3f}"
        )
        verdict = test.verdict
        reason = verdict.reason if verdict else test.failing_assertion or test.status.value
        if test.status in {TestStatus.FAILED, TestStatus.ERROR}:
            label = verdict.label.value if verdict else "unlabeled"
            ET.SubElement(case, "failure", message=f"{label}: {reason}"[:1000])
        elif test.status is TestStatus.SKIPPED:
            ET.SubElement(case, "skipped", message="read-only run")
        elif test.status is not TestStatus.PASSED:
            label = verdict.label.value if verdict else test.status.value
            ET.SubElement(case, "skipped", message=f"expected to fail, {label}: {reason}"[:1000])
    return redact_text(ET.tostring(suite, encoding="unicode", xml_declaration=True)) + "\n"


def write_reports(run: RunResults, folder: Path) -> list[Path]:
    """Write report.md, junit.xml and results.json to a folder."""
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write_text(folder / "report.md", render_report(run))
    atomic_write_text(folder / "junit.xml", render_junit(run))
    save_json(run, folder / "results.json")
    return [folder / "report.md", folder / "junit.xml", folder / "results.json"]
