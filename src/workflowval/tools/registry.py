"""Every tool and which role may call it.

The first 12 tools are the doc's list. `read_tests` and `write_verdict` are proposed additions, and
`read_tests` for the Writer too; see docs/m0-review.md. Handlers other than `write_workflow` are
stubs until their milestone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from workflowval.schemas import (
    Answer,
    Confidence,
    Label,
    Review,
    Source,
    Workflow,
    atomic_write_text,
)
from workflowval.tools.base import Tool, ToolContext, ToolError


class ReadSpecArgs(BaseModel):
    """Arguments for read_spec."""

    operation_id: str | None = Field(default=None, description="Omit for the whole graph")


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


class WriteBugReportArgs(BaseModel):
    """Arguments for write_bug_report."""

    kind: Literal["api_bug", "conflict"]
    title: str = Field(min_length=1)
    curl_steps: list[str] = Field(min_length=1)
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


def read_spec(args: ReadSpecArgs, context: ToolContext) -> str:
    """Read the API's operations, or one operation's parameters and response schemas."""
    return _stub("M1")(args, context)


def read_docs(args: ReadDocsArgs, context: ToolContext) -> str:
    """List the prose docs, or read one of them."""
    return _stub("M1")(args, context)


def read_workflows(args: ReadWorkflowsArgs, context: ToolContext) -> str:
    """Read one workflow file, or all of them."""
    return _stub("M1")(args, context)


def write_workflow(args: WriteWorkflowArgs, context: ToolContext) -> str:
    """Create or replace workflows/<id>.json. Refuses locked files, and can't create them."""
    workflow = args.workflow
    if workflow.locked or workflow.source is Source.USER:
        raise ToolError("agents can't create locked or user-sourced workflows")
    path = context.workdir / "workflows" / f"{workflow.id}.json"
    if path.exists():
        try:
            existing = Workflow.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, OSError, UnicodeDecodeError) as exc:
            # It might be a user's locked file with a typo, so never overwrite what can't be read.
            raise ToolError(f"{path.name} exists but can't be read; left unchanged") from exc
        if existing.locked:
            raise ToolError(f"{path.name} is locked by the user and can't be changed")
    atomic_write_text(path, workflow.model_dump_json(indent=2, exclude_none=True) + "\n")
    return f"wrote workflows/{workflow.id}.json"


def write_review(args: WriteReviewArgs, context: ToolContext) -> str:
    """Record review.json: workflows to fix and user journeys that are missing."""
    return _stub("M3")(args, context)


def answer_review(args: AnswerReviewArgs, context: ToolContext) -> str:
    """Answer one review.json item: accept it naming a workflow, or reject it with a reason."""
    return _stub("M3")(args, context)


def write_test(args: WriteTestArgs, context: ToolContext) -> str:
    """Write tests/wf/test_<workflow_id>.py as plain pytest + httpx."""
    return _stub("M1")(args, context)


def read_tests(args: ReadTestsArgs, context: ToolContext) -> str:
    """Read the generated test file for a workflow."""
    return _stub("M1")(args, context)


def run_tests(args: RunTestsArgs, context: ToolContext) -> str:
    """Run the generated tests in a subprocess and record results."""
    return _stub("M1")(args, context)


def read_results(args: ReadResultsArgs, context: ToolContext) -> str:
    """Read test results: status, failing assertion and the last 3 HTTP exchanges."""
    return _stub("M1")(args, context)


def http_probe(args: HttpProbeArgs, context: ToolContext) -> str:
    """Send one request to an allowed host to investigate a failure."""
    return _stub("M2")(args, context)


def write_verdict(args: WriteVerdictArgs, context: ToolContext) -> str:
    """Record the single label for a failed test, with its confidence and reason."""
    return _stub("M2")(args, context)


def write_bug_report(args: WriteBugReportArgs, context: ToolContext) -> str:
    """Write a Markdown report for an API_BUG or a spec-docs CONFLICT, with curl steps."""
    return _stub("M2")(args, context)


def propose_spec_patch(args: ProposeSpecPatchArgs, context: ToolContext) -> str:
    """Propose a minimal JSON Patch (RFC 6902) making the spec match observed behaviour."""
    return _stub("M2")(args, context)


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
