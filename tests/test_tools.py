from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from workflowval.schemas import Workflow
from workflowval.spec import Operation, Parameter, SpecGraph
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
STUBS = sorted(
    {"write_review", "answer_review", "run_tests", "http_probe", "write_bug_report"}
    | {"propose_spec_patch"}
)


def context(tmp_path: Path) -> ToolContext:
    return ToolContext(tmp_path)


GRAPH = SpecGraph(
    operations=[
        Operation(
            operation_id="getPet",
            method="GET",
            path="/pet/{id}",
            resource="pet",
            parameters=[Parameter(name="id", location="path", required=True)],
        ),
        Operation(operation_id="listPets", method="GET", path="/pet", resource="pet"),
    ]
)


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


def spec_context(tmp_path: Path, **options: Any) -> ToolContext:
    return ToolContext(tmp_path, GRAPH, **options)


def with_step(step: dict[str, Any]) -> dict[str, Any]:
    return {"workflow": {**WORKFLOW, "steps": [step]}}


def listing_step() -> dict[str, Any]:
    return {"op": "listPets", "expect": {"status": 200}}


def test_read_spec_lists_operations_and_reads_one(tmp_path: Path) -> None:
    tool, ctx = TOOLS["read_spec"], spec_context(tmp_path)
    assert "listPets: GET /pet" in tool.invoke({}, ctx)
    assert json.loads(tool.invoke({"operation_id": "getPet"}, ctx))["path"] == "/pet/{id}"
    with pytest.raises(ToolError, match="no operation"):
        tool.invoke({"operation_id": "nope"}, ctx)


def test_read_spec_needs_a_spec(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="no spec"):
        TOOLS["read_spec"].invoke({}, context(tmp_path))


def test_read_docs_lists_reads_and_stays_inside_the_folder(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("rule")
    (docs / "skip.bin").write_text("x")
    (tmp_path / "secret.md").write_text("private")
    tool, ctx = TOOLS["read_docs"], spec_context(tmp_path, docs_dir=docs)
    assert json.loads(tool.invoke({}, ctx)) == ["a.md"]
    assert tool.invoke({"path": "a.md"}, ctx) == "rule"
    with pytest.raises(ToolError, match="no doc"):
        tool.invoke({"path": "../secret.md"}, ctx)
    assert "No docs" in tool.invoke({}, spec_context(tmp_path))


def test_read_workflows_all_and_one(tmp_path: Path) -> None:
    write(tmp_path)
    tool, ctx = TOOLS["read_workflows"], context(tmp_path)
    assert json.loads(tool.invoke({}, ctx))[0]["id"] == "wf"
    assert json.loads(tool.invoke({"workflow_id": "wf"}, ctx))["id"] == "wf"
    with pytest.raises(ToolError, match="no workflow"):
        tool.invoke({"workflow_id": "none"}, ctx)


def test_read_workflows_reports_a_broken_file(tmp_path: Path) -> None:
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "bad.json").write_text("{}")
    with pytest.raises(ToolError, match="can't be read"):
        TOOLS["read_workflows"].invoke({"workflow_id": "bad"}, context(tmp_path))


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"op": "nope", "expect": {"status": 200}}, "isn't in the spec"),
        ({"op": "getPet", "expect": {"status": 200}}, "needs path parameters"),
        ({"op": "listPets", "params": {"x": 1}, "expect": {"status": 200}}, "no parameters named"),
    ],
)
def test_write_workflow_checks_against_the_spec(
    step: dict[str, Any], message: str, tmp_path: Path
) -> None:
    with pytest.raises(ToolError, match=message):
        TOOLS["write_workflow"].invoke(with_step(step), spec_context(tmp_path))


def test_expectation_changes_are_logged_so_weakening_is_visible(tmp_path: Path) -> None:
    ctx = spec_context(tmp_path)
    step = {"op": "listPets", "expect": {"status": 200, "body": {"$.a": 1, "$.b": 2}}}
    TOOLS["write_workflow"].invoke(with_step(step), ctx)
    log = tmp_path / ".workflowval" / "expectation_changes.jsonl"
    assert not log.exists()
    changed = {"op": "listPets", "expect": {"status": 201, "body": {"$.a": 9}}}
    TOOLS["write_workflow"].invoke(with_step(changed), ctx)
    entry = json.loads(log.read_text())
    assert entry["workflow_id"] == "wf"
    assert any("status 200 became 201" in change for change in entry["changes"])
    assert any("stopped checking $.b" in change for change in entry["changes"])
    assert any("expected value of $.a changed" in change for change in entry["changes"])


def test_removing_a_step_is_logged(tmp_path: Path) -> None:
    ctx = spec_context(tmp_path)
    two = {"workflow": {**WORKFLOW, "steps": [listing_step(), listing_step()]}}
    TOOLS["write_workflow"].invoke(two, ctx)
    TOOLS["write_workflow"].invoke(with_step(listing_step()), ctx)
    log = tmp_path / ".workflowval" / "expectation_changes.jsonl"
    assert "steps went from 2 to 1" in log.read_text()


GOOD_TEST = """\
def test_wf(api):
    response = api.call("GET", "/pet")
    assert response.status_code == 200, response.text
"""


def write_test_file(tmp_path: Path, source: str = GOOD_TEST) -> str:
    return TOOLS["write_test"].invoke({"workflow_id": "wf", "source": source}, context(tmp_path))


def test_write_test_formats_and_ties_the_file_to_its_workflow(tmp_path: Path) -> None:
    write(tmp_path)
    assert write_test_file(tmp_path) == "wrote tests/wf/test_wf.py"
    written = (tmp_path / "tests" / "wf" / "test_wf.py").read_text()
    assert written.startswith("# workflow-sha256: ")
    assert TOOLS["read_tests"].invoke({"workflow_id": "wf"}, context(tmp_path)) == written
    # Sending the file back keeps a single header.
    write_test_file(tmp_path, written)
    assert (tmp_path / "tests" / "wf" / "test_wf.py").read_text().count("workflow-sha256") == 1


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("def test_wf(:", "not valid Python"),
        ("import os\n" + GOOD_TEST, "import of 'os'"),
        ("from subprocess import run\n" + GOOD_TEST, "import of 'subprocess'"),
        (GOOD_TEST + "    open('x')\n", "'open' isn't allowed"),
        (GOOD_TEST + "    eval('1')\n", "'eval' isn't allowed"),
        ("import pytest\n\n\n" + GOOD_TEST + "    pytest.skip('x')\n", "skip isn't allowed"),
        ("import pytest\n\n\n@pytest.mark.xfail\n" + GOOD_TEST, "xfail isn't allowed"),
        (GOOD_TEST.replace("test_wf", "test_other"), "exactly one test"),
        (GOOD_TEST + "\n\ndef test_two(api):\n    pass\n", "exactly one test"),
        (GOOD_TEST.replace("200", "201"), "no longer checks status 200"),
        ("def test_wf(api):\n    x = 200\n", "ruff found"),
    ],
)
def test_write_test_refuses_unsafe_or_weakened_code(
    source: str, message: str, tmp_path: Path
) -> None:
    write(tmp_path)
    with pytest.raises(ToolError, match=message):
        write_test_file(tmp_path, source)
    assert not (tmp_path / "tests" / "wf" / "test_wf.py").exists()


def test_write_test_refuses_fewer_assertions(tmp_path: Path) -> None:
    write(tmp_path)
    write_test_file(tmp_path, GOOD_TEST + "    assert response.text\n")
    with pytest.raises(ToolError, match="assertion weakening"):
        write_test_file(tmp_path)


def test_read_tests_without_a_file(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="no test file"):
        TOOLS["read_tests"].invoke({"workflow_id": "wf"}, context(tmp_path))


def test_read_results(tmp_path: Path) -> None:
    tool, ctx = TOOLS["read_results"], context(tmp_path)
    with pytest.raises(ToolError, match="no tests have run"):
        tool.invoke({}, ctx)
    path = tmp_path / ".workflowval" / "last_results.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"t": {"status": "failed"}}))
    assert json.loads(tool.invoke({}, ctx)) == {"t": {"status": "failed"}}
    assert json.loads(tool.invoke({"test_id": "t"}, ctx)) == {"status": "failed"}
    with pytest.raises(ToolError, match="no result"):
        tool.invoke({"test_id": "x"}, ctx)


def verdict(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    arguments = {
        "test_id": "tests/wf/test_wf.py::test_wf",
        "label": "API_BUG",
        "confidence": "high",
        "reason": "r",
        **changes,
    }
    TOOLS["write_verdict"].invoke(arguments, context(tmp_path))
    stored = json.loads((tmp_path / ".workflowval" / "verdicts.json").read_text())
    return stored[arguments["test_id"]]


def test_write_verdict_keeps_high_confidence_on_a_documented_rule(tmp_path: Path) -> None:
    write(tmp_path)
    assert verdict(tmp_path)["confidence"] == "high"


def test_write_verdict_lowers_high_confidence_on_an_assumed_rule(tmp_path: Path) -> None:
    write(tmp_path, basis="assumed")
    assert verdict(tmp_path)["confidence"] == "med"


def test_write_verdict_defaults_a_test_bug_to_fixing_the_workflow(tmp_path: Path) -> None:
    write(tmp_path)
    assert verdict(tmp_path, label="TEST_BUG")["fix_target"] == "workflow"
    assert verdict(tmp_path, label="TEST_BUG", fix_target="test")["fix_target"] == "test"
    assert verdict(tmp_path, label="API_BUG", fix_target="test")["fix_target"] is None


def test_the_model_cannot_declare_a_test_unresolved(tmp_path: Path) -> None:
    write(tmp_path)
    with pytest.raises(ToolError, match="UNRESOLVED"):
        verdict(tmp_path, label="UNRESOLVED")
