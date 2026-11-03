"""The `workflowval test` pipeline: plan, render tests, run, label, fix, and write results."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from workflowval.gen import (
    GenerationError,
    generate,
    hash_line,
    render_conftest,
    test_path,
)
from workflowval.loop import (
    Budget,
    CodeStep,
    FixLoop,
    Pipeline,
    RunContext,
    RunResult,
    StopReason,
    StopRun,
    TaskLimitReached,
    TestOutcome,
    make_role,
    run,
)
from workflowval.models import Model
from workflowval.planner import load_workflows, plan, plan_call_limit, uncovered_operations
from workflowval.prompts import render_prompt
from workflowval.report import write_reports
from workflowval.schemas import (
    Action,
    ActionKind,
    Answer,
    AuthConfig,
    Basis,
    Confidence,
    Decision,
    Evidence,
    Finding,
    HttpExchange,
    KnownIssue,
    Label,
    LoginAuth,
    ReviewLog,
    RunResults,
    RunState,
    TestResult,
    TestStatus,
    Verdict,
    Workflow,
    atomic_write_text,
    load_json,
    save_json,
)
from workflowval.spec import (
    ResponseChecker,
    SpecError,
    SpecGraph,
    build_graph,
    load_raw_spec,
    load_spec,
    resolve_base_url,
    spec_hash,
)
from workflowval.testrun import Outcome, is_reachable, run_tests, test_id
from workflowval.tools import ToolContext
from workflowval.triage import (
    MismatchGroup,
    StoredVerdict,
    classify,
    group_mismatches,
    label_failures,
    read_verdicts,
    request_patch,
    request_report,
    review_finding,
)

PLANNER_TOOLS = frozenset({"read_spec", "read_docs", "read_workflows", "write_workflow"})
REVIEWER_TOOLS = frozenset(
    {"read_spec", "read_docs", "read_workflows", "read_results", "write_verdict"}
)
REVIEW_TOOLS = frozenset(
    {"read_spec", "read_docs", "read_workflows", "read_results", "write_review"}
)
ANSWER_TOOLS = frozenset(
    {"read_spec", "read_docs", "read_workflows", "write_workflow", "answer_review"}
)
REPORT_TOOLS = frozenset({"read_spec", "read_docs", "write_bug_report", "propose_spec_patch"})
MAX_REVIEWED_FINDINGS = 20
EXPECTABLE_LABELS = frozenset({"API_BUG", "SPEC_DRIFT", "CONFLICT"})
WRITER_TOOLS = frozenset(
    {"read_spec", "read_workflows", "read_results", "read_tests", "write_test"}
)


@dataclass(frozen=True)
class TestConfig:
    """Everything a `workflowval test` run needs."""

    __test__ = False

    spec_source: str
    base_url: str
    workdir: Path
    models: Mapping[str, Model]
    budget: Budget = field(default_factory=Budget)
    docs_dir: Path | None = None
    auth: AuthConfig | None = None
    reports_dir: Path | None = None
    read_only: bool = False
    no_redact: bool = False
    allowed_hosts: tuple[str, ...] = ()
    plan_only: bool = False
    verbose: bool = False
    cost_tracked: bool = True


@dataclass(frozen=True)
class TestRunReport:
    """What a run did."""

    __test__ = False

    run: RunResult
    results: RunResults
    results_path: Path
    expectation_changes: list[str]
    planned: bool
    looks_fixed: list[str] = field(default_factory=list)
    resumed: bool = False


def auth_env_names(auth: AuthConfig | None) -> list[str]:
    """The env vars an auth config names."""
    if auth is None:
        return []
    names = []
    for key, value in auth.scheme.model_dump().items():
        if key.endswith("_env"):
            names += list(value.values()) if isinstance(value, dict) else [value]
    return sorted(names)


def _needs_planning(config: TestConfig, current_hash: str) -> bool:
    """Plan unless the same spec was fully planned already and workflow files exist."""
    state_path = config.workdir / ".workflowval" / "state.json"
    has_workflows = any((config.workdir / "workflows").glob("*.json"))
    if not (state_path.is_file() and has_workflows):
        return True
    try:
        previous = load_json(RunState, state_path)
    except (ValidationError, OSError):
        return True
    return previous.spec_hash != current_hash or "plan" not in previous.completed_steps


def _write_test_if_changed(workflow: Workflow, graph: SpecGraph, workdir: Path) -> None:
    """Render a workflow's test, keeping a test that's already up to date with its workflow."""
    path = workdir / test_path(workflow.id)
    if path.is_file() and path.read_text(encoding="utf-8").startswith(hash_line(workflow)):
        return
    atomic_write_text(path, generate(workflow, graph).source)


def _resumable(config: TestConfig, current_hash: str) -> RunState | None:
    """Return the state of a run that was killed partway, on this same spec (FR-13).

    A run that ended by itself has a stop reason, so it starts afresh.
    """
    path = config.workdir / ".workflowval" / "state.json"
    try:
        state = load_json(RunState, path) if path.is_file() else None
    except (ValidationError, OSError):
        return None
    if (
        state is None
        or state.stop_reason is not None
        or state.spec_hash != current_hash
        or "plan" not in state.completed_steps
    ):
        return None
    return state


def _is_unedited(workflow: Workflow, graph: SpecGraph, workdir: Path) -> bool:
    """True when a workflow's test is exactly what code renders, so no fix would be lost."""
    path = workdir / test_path(workflow.id)
    return path.is_file() and path.read_text(encoding="utf-8") == generate(workflow, graph).source


def _known_issue_from(
    workflow: Workflow, outcome: Outcome, verdict: StoredVerdict
) -> KnownIssue | None:
    """Pin a reviewed failure to its step and check. None when it isn't tied to a response."""
    index = outcome.steps_run - 1
    if not 0 <= index < len(workflow.steps) or not outcome.exchanges:
        return None
    seen = outcome.exchanges[-1].get("status")
    if seen is None:
        return None
    wrong_status = seen != workflow.steps[index].expect.status
    return KnownIssue.model_validate(
        {
            "label": verdict.label.value,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "step": index,
            "check": "status" if wrong_status else "body",
            "status": seen if wrong_status else None,
        }
    )


def _failure_text(outcome: Outcome) -> str:
    return outcome.failing_assertion or f"the test ended with status {outcome.status}"


class _Session:
    """State the pipeline's steps share within one run."""

    def __init__(
        self, config: TestConfig, graph: SpecGraph, base_url: str, checker: ResponseChecker
    ) -> None:
        self.config = config
        self.graph = graph
        self.checker = checker
        self.findings: list[Finding] = []
        self.base_url = base_url
        self.workdir = config.workdir
        self.outcomes: dict[str, Outcome] = {}
        self.needs_planning = True
        self.looks_fixed: list[str] = []
        self.test_files: dict[str, str] = {}
        scheme = config.auth.scheme if config.auth else None
        self.login_operations = {scheme.op} if isinstance(scheme, LoginAuth) else set()
        self.planner = make_role("planner", config.models["planner"], PLANNER_TOOLS)
        self.reviewer = make_role("reviewer", config.models["reviewer"], REVIEWER_TOOLS)
        self.reporter = make_role("reviewer", config.models["reviewer"], REPORT_TOOLS)
        self.plan_reviewer = make_role("reviewer", config.models["reviewer"], REVIEW_TOOLS)
        self.answerer = make_role("planner", config.models["planner"], ANSWER_TOOLS)
        self.review_finished = False
        self.filed: set[str] = set()
        self.writer = make_role("writer", config.models["writer"], WRITER_TOOLS)

    def plan_step(self, ctx: RunContext) -> None:
        if not self.needs_planning:
            return
        if not plan(ctx, self.planner, self.graph, self.login_operations):
            raise StopRun(StopReason.INVALID_OUTPUT, "the Planner wrote no workflows")

    def _run_tests(self, workflow_ids: list[str] | None) -> list[Outcome]:
        return run_tests(
            self.workdir / "tests" / "wf",
            base_url=self.base_url,
            workflow_ids=workflow_ids,
            env_names=auth_env_names(self.config.auth),
            read_only=self.config.read_only,
            allowed_hosts=self.config.allowed_hosts,
            no_redact=self.config.no_redact,
        )

    def _save_progress(self) -> None:
        """Keep what a resumed run needs, since a killed process loses it."""
        atomic_write_text(
            self.workdir / ".workflowval" / "progress.json",
            json.dumps(
                {
                    "outcomes": {key: vars(value) for key, value in self.outcomes.items()},
                    "test_files": self.test_files,
                    "filed": sorted(self.filed),
                },
                default=str,
            ),
        )

    def load_progress(self) -> None:
        """Pick up where a killed run stopped."""
        path = self.workdir / ".workflowval" / "progress.json"
        if path.is_file():
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.outcomes = {key: Outcome(**value) for key, value in saved["outcomes"].items()}
            self.test_files = saved["test_files"]
            self.filed = set(saved["filed"])

    def _review_log(self) -> ReviewLog:
        path = self.workdir / "review.json"
        return load_json(ReviewLog, path) if path.is_file() else ReviewLog()

    def _review_round(self, ctx: RunContext, instruction: str, *, after_tests: bool) -> bool:
        """Run one review round and get every item answered. True if it proposed anything."""
        if ctx.state.review_rounds >= ctx.budget.max_review_rounds or ctx.tool_context is None:
            return False
        ctx.state.review_rounds += 1
        rounds_before = len(self._review_log().rounds)
        ctx.tool_context = replace(ctx.tool_context, new_workflows_only=after_tests)
        try:
            with contextlib.suppress(TaskLimitReached):
                ctx.run_role(self.plan_reviewer, instruction)
            log = self._review_log()
            if len(log.rounds) == rounds_before or not log.rounds[-1].items():
                return False
            note = (
                "Tests that have run can't change, so give each workflow a new id."
                if after_tests
                else ""
            )
            for _ in range(2):  # A model can stop early, so it gets one more chance.
                open_ids = set(self._review_log().unanswered())
                if not open_ids:
                    break
                items = [
                    item.model_dump(exclude_none=True)
                    for item in self._review_log().rounds[-1].items()
                    if item.id in open_ids
                ]
                with contextlib.suppress(TaskLimitReached):
                    ctx.run_role(
                        self.answerer,
                        render_prompt(
                            "task_answer_review", items=json.dumps(items, indent=1), note=note
                        ),
                        max_calls=plan_call_limit(self.graph),
                    )
            self._reject_unanswered()
            return True
        finally:
            ctx.tool_context = replace(ctx.tool_context, new_workflows_only=False)
            ctx.checkpoint()

    def _reject_unanswered(self) -> None:
        """Nothing is dropped silently, so an item the Planner left open is recorded as such."""
        log = self._review_log()
        refusal = Answer(decision=Decision.REJECTED, reason="The Planner gave no answer.")
        for review in log.rounds:
            for item in review.items():
                if item.answer is None:
                    item.answer = refusal
        save_json(log, self.workdir / "review.json")

    def review_plan(self, ctx: RunContext) -> None:
        """Review the plan before any test runs, keeping a round for after them if possible."""
        if not self.needs_planning:
            return
        rounds = ctx.budget.max_review_rounds
        before_tests = rounds if self.config.plan_only else max(rounds - 1, min(rounds, 1))
        for _ in range(before_tests):
            instruction = render_prompt("task_review_plan")
            if not self._review_round(ctx, instruction, after_tests=False):
                return

    def _how_far(self, workflow: Workflow) -> str:
        outcome = self.outcomes.get(test_id(workflow.id))
        if outcome is None:
            return f"- {workflow.id}: {workflow.rule}. It hasn't run."
        return (
            f"- {workflow.id}: {workflow.rule}. It {outcome.status}, and ran "
            f"{outcome.steps_run} of {len(workflow.steps)} steps."
        )

    def review_results(self, ctx: RunContext) -> None:
        """Look for what the tests never reached, and add new tests. Existing ones don't change."""
        if not self.needs_planning or self.review_finished:
            return
        workflows = load_workflows(self.workdir)
        known = [
            f"- {workflow.id} ({workflow.known_issue.label}): {workflow.known_issue.reason}"
            for workflow in workflows
            if workflow.known_issue is not None
        ]
        known += [
            f"- {group.message}, on {', '.join(sorted(group.operations))}"
            for group in group_mismatches(
                {key: outcome.calls for key, outcome in self.outcomes.items()},
                self.checker,
                self.graph,
            )
        ]
        instruction = render_prompt(
            "task_review_results",
            results="\n".join(self._how_far(workflow) for workflow in workflows),
            known="\n".join(known) or "none",
        )
        if not self._review_round(ctx, instruction, after_tests=True):
            self.review_finished = True

    def pending_tests(self, ctx: RunContext) -> frozenset[str]:
        """Tests with no final result yet: all of them at first, then only new ones."""
        ids = {test_id(workflow.id) for workflow in load_workflows(self.workdir)}
        return frozenset(ids - set(ctx.state.closed_tests))

    def render_tests(self, ctx: RunContext) -> None:
        atomic_write_text(
            self.workdir / "tests" / "wf" / "conftest.py",
            render_conftest(self.base_url, self.config.auth, self.graph),
        )
        for workflow in load_workflows(self.workdir):
            try:
                _write_test_if_changed(workflow, self.graph, self.workdir)
            except GenerationError as exc:
                raise StopRun(StopReason.INVALID_OUTPUT, f"{workflow.id}: {exc}") from exc

    def execute(self, ctx: RunContext, ids: frozenset[str] | None) -> list[TestOutcome]:
        if not is_reachable(self.base_url):
            raise StopRun(StopReason.API_UNREACHABLE, f"no response from {self.base_url}")
        wanted = None if ids is None else [i.rsplit("::test_", 1)[1] for i in ids]
        outcomes = self._run_tests(wanted)
        for outcome in outcomes:
            self.outcomes[test_id(outcome.workflow_id)] = outcome
        self._save_progress()
        atomic_write_text(
            self.workdir / ".workflowval" / "last_results.json",
            json.dumps(
                {
                    key: {name: item for name, item in vars(value).items() if name != "calls"}
                    for key, value in self.outcomes.items()
                },
                default=str,
            ),
        )
        return [
            TestOutcome(test_id(o.workflow_id), o.ok, "" if o.ok else _failure_text(o))
            for o in outcomes
        ]

    @property
    def credentials(self) -> str:
        scheme = self.config.auth.scheme if self.config.auth else None
        return scheme.type if scheme else "none, so requests carry no credentials"

    def _hint(self, outcome: Outcome) -> str:
        last = outcome.calls[-1] if outcome.calls else {}
        status = last.get("status")
        mismatches = [m.message for m in self.checker.check_call(last)] if last else []
        return classify(status if isinstance(status, int) else None, mismatches)

    def label(self, ctx: RunContext, failures: Sequence[TestOutcome]) -> Mapping[str, Label]:
        hints = {
            failure.test_id: self._hint(self.outcomes[failure.test_id])
            for failure in failures
            if failure.test_id in self.outcomes
        }
        return label_failures(ctx, self.reviewer, failures, self.credentials, hints)

    def _save_evidence(self, evidence: Mapping[str, Mapping[str, object]]) -> None:
        path = self.workdir / ".workflowval" / "evidence.json"
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        atomic_write_text(path, json.dumps({**saved, **evidence}, default=str))

    def _written_reports(self) -> dict[str, str]:
        path = self.workdir / ".workflowval" / "reports.json"
        written: dict[str, str] = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )
        return written

    def _ask_for_file(
        self, ctx: RunContext, label: str, reason: str, target: str, tests: list[str], call: object
    ) -> None:
        if label == Label.SPEC_DRIFT.value:
            request_patch(ctx, self.reporter, reason=reason, target=target, call=call)  # type: ignore[arg-type]
        elif label in EXPECTABLE_LABELS:
            request_report(
                ctx, self.reporter, label=label, reason=reason, test_id=target, tests=tests
            )

    def write_failure_files(self, ctx: RunContext) -> None:
        """Have the Reviewer write one report or patch per root cause, not one per test."""
        verdicts = read_verdicts(self.workdir)
        groups: dict[tuple[object, ...], list[str]] = {}
        for key, status in sorted(ctx.state.closed_tests.items()):
            outcome = self.outcomes.get(key)
            verdict = verdicts.get(key)
            if status not in EXPECTABLE_LABELS or outcome is None or verdict is None:
                continue
            if key in self.filed:
                continue
            self.filed.add(key)
            if not outcome.calls:
                continue
            last = outcome.calls[-1]
            root = (status, last.get("method"), last.get("template"))
            # One conflict is one report. Other labels can differ by status on the same operation.
            groups.setdefault(
                root if status == "CONFLICT" else (*root, last.get("status")), []
            ).append(key)
        for tests in groups.values():
            first = tests[0]
            outcome = self.outcomes[first]
            verdict = verdicts[first]
            self._save_evidence({first: {"calls": outcome.calls, "tests": tests}})
            self._ask_for_file(
                ctx, verdict.label.value, verdict.reason, first, tests, outcome.calls[-1]
            )
            written = self._written_reports().get(first)
            if written is not None:
                self.test_files.update(dict.fromkeys(tests, written))
            self._save_progress()

    def review_mismatches(self, ctx: RunContext) -> None:
        """Have the Reviewer rule once on each distinct way responses broke the spec."""
        calls = {key: outcome.calls for key, outcome in self.outcomes.items()}
        groups = group_mismatches(calls, self.checker, self.graph)
        self.findings = [_unreviewed_finding(group) for group in groups]
        for index, group in enumerate(groups[:MAX_REVIEWED_FINDINGS]):
            verdict = review_finding(ctx, self.reviewer, group, self.credentials)
            if verdict is None:
                continue
            self._save_evidence(
                {
                    group.finding_id: {
                        "calls": [group.example],
                        "keys": [group.key],
                        "tests": sorted(group.tests),
                    }
                }
            )
            self._ask_for_file(
                ctx,
                verdict.label.value,
                verdict.reason,
                group.finding_id,
                sorted(group.tests),
                group.example,
            )
            written = self._written_reports().get(group.finding_id)
            self.findings[index] = self.findings[index].model_copy(
                update={
                    "label": verdict.label,
                    "confidence": Confidence(verdict.confidence),
                    "reason": verdict.reason,
                    "action": _action_for(verdict.label.value, written),
                }
            )

    def execute_once(self, ctx: RunContext) -> None:
        """Run every saved test once, for `drift`. Nothing is labeled or fixed."""
        if not is_reachable(self.base_url):
            raise StopRun(StopReason.API_UNREACHABLE, f"no response from {self.base_url}")
        for outcome in self._run_tests(None):
            key = test_id(outcome.workflow_id)
            self.outcomes[key] = outcome
            ctx.state.closed_tests[key] = "passed" if outcome.ok else Label.UNRESOLVED.value

    def fix(self, ctx: RunContext, failure: TestOutcome) -> None:
        verdict = read_verdicts(self.workdir)[failure.test_id]
        workflow_id = failure.test_id.rsplit("::test_", 1)[1]
        values = {"workflow_id": workflow_id, "reason": verdict.reason, "failure": failure.message}
        if verdict.fix_target == "test":
            ctx.run_role(self.writer, render_prompt("task_fix_test", **values), workflow_id)
            return
        ctx.run_role(self.planner, render_prompt("task_fix_workflow", **values), workflow_id)
        for workflow in load_workflows(self.workdir):
            if workflow.id == workflow_id:
                try:
                    _write_test_if_changed(workflow, self.graph, self.workdir)
                except GenerationError as exc:
                    raise StopRun(StopReason.INVALID_OUTPUT, f"{workflow_id}: {exc}") from exc

    def _save_and_render(self, workflow: Workflow) -> None:
        atomic_write_text(
            self.workdir / "workflows" / f"{workflow.id}.json",
            workflow.model_dump_json(indent=2, exclude_none=True) + "\n",
        )
        atomic_write_text(
            self.workdir / test_path(workflow.id), generate(workflow, self.graph).source
        )

    def update_known_issues(self, ctx: RunContext) -> None:
        """Mark real failures as expected, then confirm the mark with one run. No model involved.

        A locked file is never edited, and a test the Writer fixed by hand is never regenerated, so
        those failures simply stay failing.
        """
        verdicts = read_verdicts(self.workdir)
        marked: list[str] = []
        for workflow in load_workflows(self.workdir):
            key = test_id(workflow.id)
            outcome = self.outcomes.get(key)
            verdict = verdicts.get(key)
            if (
                outcome is None
                or outcome.passed
                or outcome.expected
                or verdict is None
                or verdict.label.value not in EXPECTABLE_LABELS
                or ctx.state.closed_tests.get(key) != verdict.label.value
                or workflow.locked
                or not _is_unedited(workflow, self.graph, self.workdir)
            ):
                continue
            issue = _known_issue_from(workflow, outcome, verdict)
            if issue is not None:
                issue = issue.model_copy(update={"report": self.test_files.get(key)})
                self._save_and_render(workflow.model_copy(update={"known_issue": issue}))
                marked.append(workflow.id)
        if marked:
            for outcome in self._run_tests(marked):
                self.outcomes[test_id(outcome.workflow_id)] = outcome
            self._save_progress()
        self._clear_fixed()

    def _clear_fixed(self) -> None:
        """Remove the mark from a test that now passes. The bug looks fixed."""
        for workflow in load_workflows(self.workdir):
            outcome = self.outcomes.get(test_id(workflow.id))
            if (
                workflow.known_issue is not None
                and outcome is not None
                and outcome.passed
                and not workflow.locked
            ):
                self.looks_fixed.append(f"{workflow.id}: {workflow.known_issue.reason}")
                self._save_and_render(workflow.model_copy(update={"known_issue": None}))


def _unreviewed_finding(group: MismatchGroup) -> Finding:
    return Finding(
        id=group.finding_id,
        message=group.message,
        operations=sorted(group.operations),
        tests=sorted(group.tests),
    )


def _action_for(label: str, path: str | None) -> Action:
    if path is None:
        return Action(kind=ActionKind.NONE)
    kind = ActionKind.SPEC_PATCH if label == Label.SPEC_DRIFT.value else ActionKind.BUG_REPORT
    return Action(kind=kind, path=path)


def _evidence_for(outcome: Outcome, graph: SpecGraph) -> Evidence:
    last = outcome.calls[-1] if outcome.calls else {}
    operation = graph.operation_at(str(last.get("method")), str(last.get("template")))
    excerpt = (
        operation.model_dump_json(by_alias=True, exclude_none=True)[:2000] if operation else None
    )
    return Evidence(
        exchanges=[HttpExchange.model_validate(e) for e in outcome.exchanges], spec_excerpt=excerpt
    )


def _final_verdict(
    status: str,
    stored: StoredVerdict | None,
    outcome: Outcome,
    issue: KnownIssue | None,
    graph: SpecGraph,
    report: str | None = None,
) -> Verdict | None:
    if outcome.expected and issue is not None:
        return Verdict(
            label=Label(issue.label),
            confidence=Confidence(issue.confidence),
            reason=issue.reason,
            evidence=_evidence_for(outcome, graph),
            action=_action_for(issue.label, issue.report),
        )
    if status == "passed" or outcome.passed:
        return None
    label = Label(status)
    if stored is not None and stored.label is label:
        confidence, reason = Confidence(stored.confidence), stored.reason
    else:
        confidence = Confidence.LOW
        reason = f"not resolved: {_failure_text(outcome)}"
    return Verdict(
        label=label,
        confidence=confidence,
        reason=reason,
        evidence=_evidence_for(outcome, graph),
        action=_action_for(status, report),
    )


def _build_results(session: _Session, run_result: RunResult) -> RunResults:
    verdicts = read_verdicts(session.workdir)
    workflows = {workflow.id: workflow for workflow in load_workflows(session.workdir)}
    state = run_result.state
    tests = []
    for key, status in sorted(run_result.tests.items()):
        workflow_id = key.rsplit("::test_", 1)[1]
        outcome = session.outcomes.get(key) or Outcome(workflow_id, "error")
        workflow = workflows.get(workflow_id)
        if workflow is None:
            continue
        verdict = _final_verdict(
            status,
            verdicts.get(key),
            outcome,
            workflow.known_issue,
            session.graph,
            session.test_files.get(key),
        )
        tests.append(
            TestResult(
                id=key,
                workflow_id=workflow_id,
                basis=workflow.basis,
                status=TestStatus(outcome.status),
                duration_s=max(outcome.duration_s, 0.0),
                failing_assertion=outcome.failing_assertion,
                stdout=outcome.stdout,
                exchanges=[HttpExchange.model_validate(e) for e in outcome.exchanges[-3:]],
                fix_attempts=state.fix_attempts.get(key, 0),
                verdict=verdict,
            )
        )
    return RunResults(
        uncovered_operations=uncovered_operations(
            workflows.values(), session.graph, session.login_operations
        ),
        stop_reason=run_result.stop_reason.value,
        stop_detail=run_result.detail or None,
        tests=tests,
        findings=session.findings,
        reviews=session._review_log().rounds,
        tasks_stopped=state.tasks_stopped,
        input_tokens=state.input_tokens,
        output_tokens=state.output_tokens,
        cost_usd=state.cost_usd,
        unpriced_calls=state.unpriced_calls,
    )


def _read_expectation_changes(workdir: Path) -> list[str]:
    path = workdir / ".workflowval" / "expectation_changes.jsonl"
    if not path.is_file():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        lines += [f"{entry['workflow_id']}: {change}" for change in entry["changes"]]
    return lines


def _reports_folder(config: TestConfig) -> Path:
    return config.reports_dir or config.workdir / "reports"


def _tool_context(config: TestConfig, graph: SpecGraph, raw: dict[str, Any]) -> ToolContext:
    return ToolContext(
        config.workdir, graph, config.docs_dir, _reports_folder(config), raw, config.auth
    )


def results_from_outcomes(outcomes: Sequence[Outcome], workdir: Path) -> RunResults:
    """Build results from a model-free `run`. Only marks already reviewed carry a label."""
    workflows = {workflow.id: workflow for workflow in load_workflows(workdir)}
    tests = []
    for outcome in outcomes:
        workflow = workflows.get(outcome.workflow_id)
        issue = workflow.known_issue if workflow else None
        verdict = None
        if outcome.expected and issue is not None:
            verdict = Verdict(
                label=Label(issue.label),
                confidence=Confidence(issue.confidence),
                reason=issue.reason,
                action=_action_for(issue.label, issue.report),
            )
        tests.append(
            TestResult(
                id=test_id(outcome.workflow_id),
                workflow_id=outcome.workflow_id,
                basis=workflow.basis if workflow else Basis.ASSUMED,
                status=TestStatus(outcome.status),
                duration_s=max(outcome.duration_s, 0.0),
                failing_assertion=outcome.failing_assertion,
                stdout=outcome.stdout,
                exchanges=[HttpExchange.model_validate(e) for e in outcome.exchanges[-3:]],
                verdict=verdict,
            )
        )
    return RunResults(stop_reason="completed", tests=tests)


def run_drift_pipeline(config: TestConfig) -> TestRunReport:
    """Run `workflowval drift`: re-run saved tests and rule only on what breaks the spec."""
    spec = load_spec(config.spec_source)
    if not any((config.workdir / "workflows").glob("*.json")):
        raise SpecError("no workflows here yet: run `workflowval test` first")
    graph = build_graph(spec)
    base_url = resolve_base_url(config.base_url, graph)
    raw = load_raw_spec(config.spec_source)
    session = _Session(config, graph, base_url, ResponseChecker(raw))
    for name in ("verdicts.json", "evidence.json", "reports.json"):
        (config.workdir / ".workflowval" / name).unlink(missing_ok=True)
    # `drift` isn't a test run, so the next `test` must still know the plan is done.
    state_path = config.workdir / ".workflowval" / "state.json"
    earlier_state = state_path.read_bytes() if state_path.is_file() else None
    result = run(
        Pipeline(
            (
                CodeStep("render_tests", session.render_tests),
                CodeStep("execute_once", session.execute_once),
                CodeStep("review_mismatches", session.review_mismatches),
            )
        ),
        config.budget,
        workdir=config.workdir,
        spec_hash=spec_hash(spec),
        verbose=config.verbose,
        tool_context=_tool_context(config, graph, raw),
        cost_tracked=config.cost_tracked,
    )
    if earlier_state is not None:
        state_path.write_bytes(earlier_state)
    results = _build_results(session, result)
    results_path = config.workdir / "results.json"
    save_json(results, results_path)
    write_reports(results, _reports_folder(config))
    return TestRunReport(result, results, results_path, [], False)


def run_test_pipeline(config: TestConfig) -> TestRunReport:
    """Run `workflowval test`. Spec problems raise SpecError before any model is called."""
    spec = load_spec(config.spec_source)
    graph = build_graph(spec)
    current_hash = spec_hash(spec)
    base_url = resolve_base_url(config.base_url, graph)
    raw = load_raw_spec(config.spec_source)
    session = _Session(config, graph, base_url, ResponseChecker(raw))
    resume = None if config.plan_only else _resumable(config, current_hash)
    session.needs_planning = resume is not None or _needs_planning(config, current_hash)
    state_dir = config.workdir / ".workflowval"
    if resume is not None:
        session.load_progress()
    else:
        # Each run reports only its own expectation changes and verdicts.
        for name in (
            "expectation_changes.jsonl",
            "verdicts.json",
            "last_results.json",
            "evidence.json",
            "reports.json",
            "progress.json",
        ):
            (state_dir / name).unlink(missing_ok=True)
    if session.needs_planning and resume is None:
        (config.workdir / "review.json").unlink(missing_ok=True)
    steps: list[CodeStep | FixLoop] = [
        CodeStep("plan", session.plan_step),
        CodeStep("review_plan", session.review_plan),
    ]
    if not config.plan_only:
        steps += [
            CodeStep("render_tests", session.render_tests),
            FixLoop(
                "test_and_fix", session.execute, session.label, session.fix, session.pending_tests
            ),
            CodeStep("write_failure_files", session.write_failure_files),
            CodeStep("update_known_issues", session.update_known_issues),
        ]
        # Rounds left after the first review each look for what the tests never reached.
        for index in range(config.budget.max_review_rounds):
            steps += [
                CodeStep(f"review_results_{index}", session.review_results),
                CodeStep(f"render_new_tests_{index}", session.render_tests),
                FixLoop(
                    f"test_new_{index}",
                    session.execute,
                    session.label,
                    session.fix,
                    session.pending_tests,
                ),
                CodeStep(f"write_new_failure_files_{index}", session.write_failure_files),
                CodeStep(f"update_new_known_issues_{index}", session.update_known_issues),
            ]
        steps += [
            CodeStep("review_mismatches", session.review_mismatches),
        ]
    result = run(
        Pipeline(tuple(steps)),
        config.budget,
        workdir=config.workdir,
        spec_hash=current_hash,
        verbose=config.verbose,
        tool_context=_tool_context(config, graph, raw),
        cost_tracked=config.cost_tracked,
        resume=resume,
    )
    results = _build_results(session, result)
    results_path = config.workdir / "results.json"
    save_json(results, results_path)
    if not config.plan_only:
        write_reports(results, _reports_folder(config))
    return TestRunReport(
        result,
        results,
        results_path,
        _read_expectation_changes(config.workdir),
        session.needs_planning,
        session.looks_fixed,
        resume is not None,
    )
