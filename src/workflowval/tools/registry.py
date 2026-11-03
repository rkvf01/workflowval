"""Every tool and which role may call it.

The first 12 tools are the doc's list. `read_tests` and `write_verdict` were added in the M0 review
(docs/m0-review.md). Tools that raise "arrives in" are stubs until that milestone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import jsonpatch
from jsonpointer import JsonPointerException, resolve_pointer
from pydantic import BaseModel, Field, ValidationError, model_validator

from workflowval.gen import (
    SourceError,
    check_test_source,
    format_source,
    hash_line,
    lint_source,
    test_path,
)
from workflowval.redact import redact_text
from workflowval.report.curl import curl_steps
from workflowval.schemas import (
    Answer,
    Basis,
    Confidence,
    Label,
    Review,
    Source,
    Workflow,
    atomic_write_text,
)
from workflowval.spec import ResponseChecker, SpecGraph
from workflowval.tools.base import Tool, ToolContext, ToolError


class ReadSpecArgs(BaseModel):
    """Arguments for read_spec."""

    operation_id: str | None = Field(default=None, description="Omit for the whole graph")
    pointer: str | None = Field(
        default=None,
        description="A JSON Pointer into the spec as written, with its $refs, for a spec patch",
    )


class ReadDocsArgs(BaseModel):
    """Arguments for read_docs."""

    path: str | None = Field(default=None, description="Omit to list the docs")


class ReadWorkflowsArgs(BaseModel):
    """Arguments for read_workflows."""

    workflow_id: str | None = Field(default=None, description="Omit for all workflows")


class WriteWorkflowArgs(BaseModel):
    """Arguments for write_workflow."""

    workflow: Workflow


class WriteReviewArgs(BaseModel):
    """Arguments for write_review."""

    review: Review


class AnswerReviewArgs(BaseModel):
    """Arguments for answer_review."""

    item_id: str = Field(min_length=1)
    answer: Answer


class WriteTestArgs(BaseModel):
    """Arguments for write_test."""

    workflow_id: str = Field(pattern=r"^[a-z0-9_]+$")
    source: str = Field(min_length=1, description="The full Python source")


class ReadTestsArgs(BaseModel):
    """Arguments for read_tests."""

    workflow_id: str = Field(pattern=r"^[a-z0-9_]+$")


class RunTestsArgs(BaseModel):
    """Arguments for run_tests."""

    test_ids: list[str] = Field(default_factory=list, description="Empty runs every test")


class ReadResultsArgs(BaseModel):
    """Arguments for read_results."""

    test_id: str | None = Field(default=None, description="Omit for all results")


class HttpProbeArgs(BaseModel):
    """Arguments for http_probe."""

    method: Literal["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"]
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None


class WriteVerdictArgs(BaseModel):
    """Arguments for write_verdict."""

    test_id: str = Field(min_length=1)
    label: Label
    confidence: Confidence
    reason: str = Field(min_length=1)
    fix_target: Literal["test", "workflow"] | None = Field(
        default=None,
        description="For TEST_BUG only: 'workflow' if an expectation or data is wrong, else 'test'",
    )


class WriteBugReportArgs(BaseModel):
    """Arguments for write_bug_report. Code adds the curl steps from what was recorded."""

    kind: Literal["api_bug", "conflict"]
    title: str = Field(min_length=1)
    test_id: str = Field(min_length=1, description="The test or finding id this report covers")
    expected: str = Field(min_length=1, description="For a conflict: per spec, and per docs")
    actual: str = Field(min_length=1)
    spec_reference: str = Field(min_length=1)
    docs_excerpt: str | None = Field(default=None, description="Required for a conflict")
    affected_workflows: list[str] = Field(
        default_factory=list, description="Required for a conflict"
    )
    suggested_resolution: str | None = Field(default=None, description="Required for a conflict")

    @model_validator(mode="after")
    def _conflict_has_its_extra_fields(self) -> WriteBugReportArgs:
        if self.kind == "conflict":
            required = ("docs_excerpt", "affected_workflows", "suggested_resolution")
            missing = [name for name in required if not getattr(self, name)]
            if missing:
                raise ValueError(f"a conflict report also needs: {', '.join(missing)}")
        return self


class JsonPatchOp(BaseModel):
    """One RFC 6902 operation."""

    op: Literal["add", "remove", "replace", "move", "copy", "test"]
    path: str = Field(pattern=r"^(/.*)?$")
    value: Any = None
    from_: str | None = Field(default=None, alias="from")


class ProposeSpecPatchArgs(BaseModel):
    """Arguments for propose_spec_patch."""

    test_id: str = Field(min_length=1)
    patch: list[JsonPatchOp] = Field(min_length=1)


def _stub(milestone: str) -> Callable[[Any, ToolContext], str]:
    def handler(args: Any, context: ToolContext) -> str:
        raise ToolError(f"this tool is not implemented yet; it arrives in {milestone}")

    return handler


def _workflows_dir(context: ToolContext) -> Path:
    return context.workdir / "workflows"


def _load_workflow(context: ToolContext, workflow_id: str) -> Workflow:
    path = _workflows_dir(context) / f"{workflow_id}.json"
    if not path.is_file():
        raise ToolError(f"no workflow named {workflow_id!r}")
    try:
        return Workflow.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, UnicodeDecodeError) as exc:
        raise ToolError(f"{path.name} can't be read: {exc}") from exc


def read_spec(args: ReadSpecArgs, context: ToolContext) -> str:
    """Read the API's operations, or one operation's parameters and response schemas."""
    graph = context.graph
    if graph is None:
        raise ToolError("no spec is loaded")
    if args.pointer is not None:
        try:
            return json.dumps(resolve_pointer(context.raw_spec or {}, args.pointer))
        except JsonPointerException as exc:
            raise ToolError(f"nothing at {args.pointer!r}: {exc}") from exc
    if args.operation_id is None:
        return json.dumps(
            {
                "operations": [
                    f"{op.operation_id}: {op.method} {op.path}" for op in graph.operations
                ],
                "links": [link.model_dump() for link in graph.links],
            }
        )
    operation = graph.operation(args.operation_id)
    if operation is None:
        raise ToolError(f"no operation named {args.operation_id!r}")
    return operation.model_dump_json(by_alias=True, exclude_none=True)


def read_docs(args: ReadDocsArgs, context: ToolContext) -> str:
    """List the prose docs, or read one of them."""
    root = context.docs_dir
    if root is None:
        return "No docs were provided: work from the spec."
    root = root.resolve()
    files = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.suffix.lower() in {".md", ".html", ".htm", ".txt"} and path.is_file()
    )
    if args.path is None:
        return json.dumps(files)
    if args.path not in files:
        raise ToolError(f"no doc named {args.path!r}. Docs: {files}")
    return (root / args.path).read_text(encoding="utf-8", errors="replace")


def read_workflows(args: ReadWorkflowsArgs, context: ToolContext) -> str:
    """Read one workflow file, or all of them."""
    if args.workflow_id is not None:
        return _load_workflow(context, args.workflow_id).model_dump_json(exclude_none=True)
    workflows = []
    for path in sorted(_workflows_dir(context).glob("*.json")):
        workflows.append(json.loads(path.read_text(encoding="utf-8")))
    return json.dumps(workflows)


def _check_against_spec(workflow: Workflow, context: ToolContext) -> None:
    graph = context.graph
    if graph is None:
        return
    for step in [*workflow.steps, *workflow.cleanup]:
        operation = graph.operation(step.op)
        if operation is None:
            raise ToolError(f"operation {step.op!r} isn't in the spec")
        parameters = {p.name: p for p in operation.parameters}
        unknown = sorted(set(step.params) - set(parameters))
        if unknown:
            raise ToolError(f"{step.op} has no parameters named {unknown}")
        missing = sorted(
            p.name
            for p in parameters.values()
            if p.location == "path" and p.name not in step.params
        )
        if missing:
            raise ToolError(f"{step.op} needs path parameters {missing}")
    _check_only_touches_own_data(workflow, graph)


def _is_generated(value: Any) -> bool:
    """True when a value comes from a saved or unique placeholder, not a fixed literal."""
    return isinstance(value, str) and "{" in value


def _check_only_touches_own_data(workflow: Workflow, graph: SpecGraph) -> None:
    """Enforce NFR-3: tests must never change or delete data they didn't create."""
    writes = {"PUT", "PATCH", "DELETE"}
    for step in [*workflow.steps, *workflow.cleanup]:
        operation = graph.operation(step.op)
        if operation is None:
            continue
        if operation.method in writes:
            path_names = {p.name for p in operation.parameters if p.location == "path"}
            for name in sorted(path_names & set(step.params)):
                if not _is_generated(step.params[name]):
                    raise ToolError(
                        f"{step.op} changes data, so {name} must come from a saved value or "
                        f"{{unique}}: a fixed value could hit data this test didn't create"
                    )
        items = step.body if isinstance(step.body, list) else [step.body]
        for item in items:
            if isinstance(item, dict) and "id" in item and not _is_generated(item["id"]):
                raise ToolError(
                    f"{step.op} sends a fixed id: use a saved value or {{unique_int}}, so it "
                    "can't overwrite data this test didn't create"
                )
    deletable = {op.resource for op in graph.operations if op.method == "DELETE"}
    if not workflow.cleanup:
        for step in workflow.steps:
            operation = graph.operation(step.op)
            if (
                operation is not None
                and operation.method == "POST"
                and step.expect.status < 400
                and operation.resource in deletable
            ):
                raise ToolError(
                    f"{step.op} creates data, so add a `cleanup` step that deletes it, even if a "
                    "step deletes it too: a failed test must not leave data behind"
                )


def _expectation_changes(old: Workflow, new: Workflow) -> list[str]:
    """List how a rewrite changes what the test checks, so weakening can't happen silently."""
    changes = []
    if len(new.steps) < len(old.steps):
        changes.append(f"steps went from {len(old.steps)} to {len(new.steps)}")
    for index, (before, after) in enumerate(zip(old.steps, new.steps, strict=False)):
        if before.expect.status != after.expect.status:
            changes.append(
                f"steps[{index}] {before.op}: status {before.expect.status} "
                f"became {after.expect.status}"
            )
        for key in sorted(set(before.expect.body) - set(after.expect.body)):
            changes.append(f"steps[{index}] {before.op}: stopped checking {key}")
        for key in sorted(set(before.expect.body) & set(after.expect.body)):
            if before.expect.body[key] != after.expect.body[key]:
                changes.append(f"steps[{index}] {before.op}: expected value of {key} changed")
    return changes


def write_workflow(args: WriteWorkflowArgs, context: ToolContext) -> str:
    """Create or replace workflows/<id>.json. Refuses locked files, and can't create them."""
    # Only the pipeline marks failures as expected. A rewrite gets judged afresh on its next run.
    workflow = args.workflow.model_copy(update={"known_issue": None})
    if workflow.locked or workflow.source is Source.USER:
        raise ToolError("agents can't create locked or user-sourced workflows")
    _check_against_spec(workflow, context)
    path = _workflows_dir(context) / f"{workflow.id}.json"
    changes: list[str] = []
    if path.exists():
        try:
            existing = Workflow.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError, UnicodeDecodeError) as exc:
            # It might be a user's locked file with a typo, so never overwrite what can't be read.
            raise ToolError(f"{path.name} exists but can't be read; left unchanged") from exc
        if existing.locked:
            raise ToolError(f"{path.name} is locked by the user and can't be changed")
        changes = _expectation_changes(existing, workflow)
    atomic_write_text(path, workflow.model_dump_json(indent=2, exclude_none=True) + "\n")
    if changes:
        log = context.workdir / ".workflowval" / "expectation_changes.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"workflow_id": workflow.id, "changes": changes}) + "\n")
    return f"wrote workflows/{workflow.id}.json"


def write_review(args: WriteReviewArgs, context: ToolContext) -> str:
    """Record review.json: workflows to fix and user journeys that are missing."""
    return _stub("M3")(args, context)


def answer_review(args: AnswerReviewArgs, context: ToolContext) -> str:
    """Answer one review.json item: accept it naming a workflow, or reject it with a reason."""
    return _stub("M3")(args, context)


def write_test(args: WriteTestArgs, context: ToolContext) -> str:
    """Replace tests/wf/test_<workflow_id>.py with fixed code. Assertions can't be weakened."""
    workflow = _load_workflow(context, args.workflow_id)
    path = context.workdir / test_path(args.workflow_id)
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    try:
        check_test_source(args.source, workflow, previous)
        body = args.source
        if body.startswith("# workflow-sha256: "):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        source = format_source(f"{hash_line(workflow)}\n{body}")
        lint_source(source)
    except SourceError as exc:
        raise ToolError(str(exc)) from exc
    atomic_write_text(path, source)
    return f"wrote {test_path(args.workflow_id)}"


def read_tests(args: ReadTestsArgs, context: ToolContext) -> str:
    """Read the generated test file for a workflow."""
    path = context.workdir / test_path(args.workflow_id)
    if not path.is_file():
        raise ToolError(f"no test file for {args.workflow_id!r}")
    return path.read_text(encoding="utf-8")


def run_tests(args: RunTestsArgs, context: ToolContext) -> str:
    """Run the generated tests in a subprocess and record results."""
    return _stub("M1")(args, context)


def read_results(args: ReadResultsArgs, context: ToolContext) -> str:
    """Read test results: status, failing assertion and the last 3 HTTP exchanges."""
    path = context.workdir / ".workflowval" / "last_results.json"
    if not path.is_file():
        raise ToolError("no tests have run yet")
    results = json.loads(path.read_text(encoding="utf-8"))
    if args.test_id is None:
        return json.dumps(results)
    if args.test_id not in results:
        raise ToolError(f"no result for {args.test_id!r}. Results: {sorted(results)}")
    return json.dumps(results[args.test_id])


def http_probe(args: HttpProbeArgs, context: ToolContext) -> str:
    """Send one request to an allowed host to investigate a failure."""
    return _stub("M3")(args, context)


def write_verdict(args: WriteVerdictArgs, context: ToolContext) -> str:
    """Record the single label for a failed test, with its confidence and reason."""
    if args.label is Label.UNRESOLVED:
        raise ToolError("UNRESOLVED is set by the pipeline when fix attempts run out")
    confidence = args.confidence
    _, separator, workflow_id = args.test_id.rpartition("::test_")
    if (
        separator
        and confidence is Confidence.HIGH
        and _load_workflow(context, workflow_id).basis is Basis.ASSUMED
    ):
        confidence = Confidence.MED  # Failures on assumed rules are capped at medium.
    path = context.workdir / ".workflowval" / "verdicts.json"
    verdicts = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    verdicts[args.test_id] = {
        "label": args.label.value,
        "confidence": confidence.value,
        "reason": args.reason,
        "fix_target": (args.fix_target or "workflow") if args.label is Label.TEST_BUG else None,
    }
    atomic_write_text(path, json.dumps(verdicts, indent=2) + "\n")
    return f"recorded {args.label.value} for {args.test_id}"


def _evidence(context: ToolContext, target: str) -> dict[str, Any]:
    path = context.workdir / ".workflowval" / "evidence.json"
    evidence: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    )
    if target not in evidence:
        raise ToolError(f"nothing was recorded for {target!r}. Recorded: {sorted(evidence)}")
    found: dict[str, Any] = evidence[target]
    return found


def _reports_dir(context: ToolContext) -> Path:
    return context.reports_dir or context.workdir / "reports"


def _record_report(context: ToolContext, target: str, path: Path) -> str:
    """Remember which file answers a test or finding, and return its path for the model."""
    index = context.workdir / ".workflowval" / "reports.json"
    written = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else {}
    written[target] = str(path)
    atomic_write_text(index, json.dumps(written, indent=2) + "\n")
    return str(path)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "report"


def write_bug_report(args: WriteBugReportArgs, context: ToolContext) -> str:
    """Write a Markdown report for an API_BUG or a spec-docs CONFLICT. Code adds the curl steps."""
    evidence = _evidence(context, args.test_id)
    steps = "\n".join(curl_steps(evidence["calls"], context.auth))
    tests = "\n".join(f"- `{test}`" for test in evidence.get("tests", [args.test_id]))
    sections = [
        f"# {args.title}",
        f"**Kind:** {args.kind}",
        f"## Tests that show it\n\n{tests}",
        f"## Steps to reproduce\n\n```\n{steps}\n```",
        f"## Expected\n\n{args.expected}",
        f"## Actual\n\n{args.actual}",
        f"## Spec reference\n\n{args.spec_reference}",
    ]
    if args.kind == "conflict":
        workflows = "\n".join(f"- `{workflow}`" for workflow in args.affected_workflows)
        sections += [
            f"## What the docs say\n\n{args.docs_excerpt}",
            f"## Affected workflows\n\n{workflows}",
            f"## Suggested resolution\n\n{args.suggested_resolution}",
        ]
    path = _reports_dir(context) / "bugs" / f"{_slug(args.title)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, redact_text("\n\n".join(sections)) + "\n")
    return f"wrote {_record_report(context, args.test_id, path)}"


def propose_spec_patch(args: ProposeSpecPatchArgs, context: ToolContext) -> str:
    """Propose a minimal JSON Patch (RFC 6902) making the spec match observed behaviour."""
    evidence = _evidence(context, args.test_id)
    raw = context.raw_spec
    if raw is None:
        raise ToolError("no spec is loaded")
    operations = [op.model_dump(by_alias=True, exclude_unset=True) for op in args.patch]
    try:
        patched = jsonpatch.apply_patch(raw, operations)
    except (jsonpatch.JsonPatchException, JsonPointerException) as exc:
        raise ToolError(f"the patch doesn't apply to the spec as written: {exc}") from exc
    try:
        checker = ResponseChecker(patched, validate_spec=True)
        before_checker = ResponseChecker(raw)
    except Exception as exc:  # openapi-core raises many types for a broken spec.
        raise ToolError(f"the patched spec isn't valid OpenAPI: {str(exc)[:300]}") from exc
    calls = evidence["calls"]
    wanted = evidence.get("keys")
    before = {m.key for call in calls for m in before_checker.check_call(call)}
    targets = set(wanted) if wanted else before
    if not targets:
        raise ToolError("the recorded response already fits the spec, so there's nothing to patch")
    after = {m.key for call in calls for m in checker.check_call(call)}
    if still := sorted(targets & after):
        raise ToolError(f"the observed response still breaks the patched spec: {still}")
    if added := sorted(after - before):
        raise ToolError(f"the patch makes the observed response break the spec: {added}")
    path = _reports_dir(context) / "patches" / f"{_slug(args.test_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(operations, indent=2) + "\n")
    return f"wrote {_record_report(context, args.test_id, path)}"


def _tool(fn: Callable[[Any, ToolContext], str], schema: type[BaseModel]) -> Tool:
    if not fn.__doc__:
        raise ValueError(f"tool {fn.__name__} needs a docstring: the model reads it")
    return Tool(fn.__name__, fn.__doc__, schema, fn)


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        _tool(read_spec, ReadSpecArgs),
        _tool(read_docs, ReadDocsArgs),
        _tool(read_workflows, ReadWorkflowsArgs),
        _tool(write_workflow, WriteWorkflowArgs),
        _tool(write_review, WriteReviewArgs),
        _tool(answer_review, AnswerReviewArgs),
        _tool(write_test, WriteTestArgs),
        _tool(read_tests, ReadTestsArgs),
        _tool(run_tests, RunTestsArgs),
        _tool(read_results, ReadResultsArgs),
        _tool(http_probe, HttpProbeArgs),
        _tool(write_verdict, WriteVerdictArgs),
        _tool(write_bug_report, WriteBugReportArgs),
        _tool(propose_spec_patch, ProposeSpecPatchArgs),
    )
}

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "planner": frozenset(
        {
            "read_spec",
            "read_docs",
            "read_workflows",
            "write_workflow",
            "answer_review",
            "read_tests",
        }
    ),
    "reviewer": frozenset(
        {
            "read_spec",
            "read_docs",
            "read_workflows",
            "write_review",
            "read_results",
            "http_probe",
            "write_verdict",
            "write_bug_report",
            "propose_spec_patch",
        }
    ),
    "writer": frozenset(
        {"read_spec", "read_workflows", "read_results", "write_test", "read_tests"}
    ),
}
