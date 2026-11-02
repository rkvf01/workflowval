from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from workflowval.schemas import Workflow
from workflowval.tools import ROLE_TOOLS, TOOLS, ToolContext, ToolError

WORKFLOW = {
    "id": "wf",
    "source": "planner",
    "basis": "documented",
    "rule": "Pets can be read back",
    "steps": [{"op": "getPet", "expect": {"status": 200}}],
}
ANSWER = {"decision": "rejected", "reason": "duplicate"}

# One valid and one invalid argument set per tool, so a new tool without a test fails below.
SAMPLES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "read_spec": ({"operation_id": "getPet"}, {"operation_id": 1}),
    "read_docs": ({"path": "a.md"}, {"path": 1}),
    "read_workflows": ({}, {"workflow_id": 1}),
    "write_workflow": ({"workflow": WORKFLOW}, {"workflow": {"id": "../x"}}),
    "write_review": ({"review": {"round": 1}}, {"review": {"round": 0}}),
    "answer_review": ({"item_id": "j1", "answer": ANSWER}, {"item_id": "j1"}),
    "write_test": ({"workflow_id": "wf", "source": "x"}, {"workflow_id": "../x", "source": "x"}),
    "read_tests": ({"workflow_id": "wf"}, {"workflow_id": "../x"}),
    "run_tests": ({"test_ids": ["t"]}, {"test_ids": "t"}),
    "read_results": ({"test_id": "t"}, {"test_id": 1}),
    "http_probe": ({"method": "GET", "url": "http://x/"}, {"method": "TRACE", "url": "http://x/"}),
    "write_verdict": (
        {"test_id": "t", "label": "API_BUG", "confidence": "high", "reason": "r"},
        {"test_id": "t", "label": "MAYBE", "confidence": "high", "reason": "r"},
    ),
    "write_bug_report": (
        {
            "kind": "conflict",
            "title": "t",
            "curl_steps": ["curl x"],
            "expected": "e",
            "actual": "a",
            "spec_reference": "s",
            "docs_excerpt": "d",
            "affected_workflows": ["wf"],
            "suggested_resolution": "r",
        },
        {"kind": "other"},
    ),
    "propose_spec_patch": (
        {"test_id": "t", "patch": [{"op": "replace", "path": "/a", "value": 1}]},
        {"test_id": "t", "patch": []},
    ),
}
STUBS = sorted(set(TOOLS) - {"write_workflow"})


def context(tmp_path: Path) -> ToolContext:
    return ToolContext(tmp_path)


def test_every_tool_from_the_doc_exists() -> None:
    doc_tools = {
        "read_spec",
        "read_docs",
        "read_workflows",
        "write_workflow",
        "write_review",
        "answer_review",
        "write_test",
        "run_tests",
        "read_results",
        "http_probe",
        "write_bug_report",
        "propose_spec_patch",
    }
    assert doc_tools <= set(TOOLS)
    assert set(TOOLS) == set(SAMPLES)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_has_schema_docstring_and_model_facing_spec(name: str) -> None:
    tool = TOOLS[name]
    assert issubclass(tool.schema, BaseModel)
    assert tool.description == tool.fn.__doc__
    assert tool.description
    spec = tool.spec()
    assert (spec.name, spec.description) == (name, tool.description)
    assert spec.parameters["type"] == "object"


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_rejects_invalid_arguments(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        TOOLS[name].invoke(SAMPLES[name][1], context(tmp_path))


@pytest.mark.parametrize("name", STUBS)
def test_stub_tool_accepts_valid_arguments_and_names_its_milestone(
    name: str, tmp_path: Path
) -> None:
    with pytest.raises(ToolError, match=r"arrives in M[123]"):
        TOOLS[name].invoke(SAMPLES[name][0], context(tmp_path))


# The doc's Roles table: "Can call" and "Never calls". Planner, Reviewer and Writer entries beyond
# "Can call" (read_tests, write_verdict) are the proposals listed in docs/m0-review.md.
@pytest.mark.parametrize(
    ("role", "can", "never"),
    [
        (
            "planner",
            {"read_spec", "read_docs", "read_workflows", "write_workflow", "answer_review"},
            {"write_test", "http_probe"},
        ),
        (
            "reviewer",
            {
                "read_spec",
                "read_docs",
                "read_workflows",
                "write_review",
                "read_results",
                "http_probe",
                "write_bug_report",
                "propose_spec_patch",
            },
            {"write_test", "write_workflow"},
        ),
        (
            "writer",
            {"read_spec", "read_workflows", "read_results", "write_test"},
            {"write_workflow", "write_bug_report", "propose_spec_patch"},
        ),
    ],
)
def test_role_tools_match_the_doc(role: str, can: set[str], never: set[str]) -> None:
    assert can <= ROLE_TOOLS[role]
    assert not never & ROLE_TOOLS[role]
    assert ROLE_TOOLS[role] <= set(TOOLS)


def test_no_role_runs_tests_itself() -> None:
    assert not any("run_tests" in tools for tools in ROLE_TOOLS.values())


def write(tmp_path: Path, **changes: Any) -> str:
    return TOOLS["write_workflow"].invoke({"workflow": {**WORKFLOW, **changes}}, context(tmp_path))


def test_write_workflow_creates_and_replaces_files(tmp_path: Path) -> None:
    assert write(tmp_path) == "wrote workflows/wf.json"
    write(tmp_path, rule="new rule")
    saved = Workflow.model_validate_json((tmp_path / "workflows" / "wf.json").read_text())
    assert saved.rule == "new rule"


def test_write_workflow_refuses_a_locked_file(tmp_path: Path) -> None:
    path = tmp_path / "workflows" / "wf.json"
    path.parent.mkdir()
    original = json.dumps({**WORKFLOW, "source": "user", "locked": True})
    path.write_text(original)
    with pytest.raises(ToolError, match="locked"):
        write(tmp_path, rule="hijack")
    assert path.read_text() == original


def test_write_workflow_will_not_overwrite_a_file_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "workflows" / "wf.json"
    path.parent.mkdir()
    path.write_text('{"locked": true, oops')
    with pytest.raises(ToolError, match="can't be read"):
        write(tmp_path)
    assert path.read_text() == '{"locked": true, oops'


@pytest.mark.parametrize("changes", [{"source": "user"}, {"source": "user", "locked": True}])
def test_agents_cannot_create_user_or_locked_workflows(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="locked or user-sourced"):
        write(tmp_path, **changes)
    assert not (tmp_path / "workflows").exists()


def test_write_workflow_cannot_escape_the_workflows_folder(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        write(tmp_path, id="../../outside")
    assert not (tmp_path.parent / "outside.json").exists()


BUG_REPORT: dict[str, Any] = {
    "kind": "api_bug",
    "title": "t",
    "curl_steps": ["curl x"],
    "expected": "e",
    "actual": "a",
    "spec_reference": "s",
}
CONFLICT_EXTRAS: dict[str, Any] = {
    "docs_excerpt": "d",
    "affected_workflows": ["wf"],
    "suggested_resolution": "r",
}


def test_an_api_bug_report_does_not_need_the_conflict_fields(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="arrives in"):
        TOOLS["write_bug_report"].invoke(BUG_REPORT, context(tmp_path))


@pytest.mark.parametrize("field", sorted(CONFLICT_EXTRAS))
@pytest.mark.parametrize("blank", ["missing", "empty"])
def test_a_conflict_report_needs_each_extra_field(field: str, blank: str, tmp_path: Path) -> None:
    extras = dict(CONFLICT_EXTRAS)
    if blank == "missing":
        del extras[field]
    else:
        extras[field] = [] if field == "affected_workflows" else ""
    arguments = {**BUG_REPORT, "kind": "conflict", **extras}
    with pytest.raises(ValidationError, match=f"also needs: {field}"):
        TOOLS["write_bug_report"].invoke(arguments, context(tmp_path))


def test_a_conflict_report_names_every_missing_field(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="docs_excerpt, affected_workflows, suggested"):
        TOOLS["write_bug_report"].invoke({**BUG_REPORT, "kind": "conflict"}, context(tmp_path))
