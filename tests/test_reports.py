"""Bug reports, spec patches, report.md, JUnit, and the `drift` and `report` commands."""

from __future__ import annotations

import json
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from workflowval import cli
from workflowval.report import render_junit, render_report, write_reports
from workflowval.report.curl import curl_command, curl_steps
from workflowval.schemas import (
    ApiKeyAuth,
    AuthConfig,
    BasicAuth,
    BearerAuth,
    LoginAuth,
    RunResults,
)
from workflowval.spec import load_raw_spec
from workflowval.tools import TOOLS, ToolContext, ToolError

SEEDED = Path(__file__).parent.parent / "benchmarks" / "seeded"
runner = CliRunner()
MASK = "[REDACTED]"
CALL: dict[str, Any] = {
    "method": "POST",
    "url": "http://localhost:1/orders",
    "request_headers": {
        "host": "localhost",
        "user-agent": "x",
        "accept": "application/json",
        "authorization": MASK,
    },
    "request_body": {"customer": "o'neil"},
    "status": 500,
}


def bearer() -> AuthConfig:
    return AuthConfig(scheme=BearerAuth(type="bearer", token_env="MY_TOKEN"))


def test_curl_keeps_the_request_and_never_a_secret() -> None:
    command = curl_command(CALL, bearer())
    assert command.startswith("curl -i -X POST http://localhost:1/orders")
    assert '-H "authorization: Bearer $MY_TOKEN"' in command
    assert MASK not in command
    assert "user-agent" not in command
    assert "-H 'host" not in command


def test_curl_body_survives_shell_quoting() -> None:
    command = curl_command(CALL, bearer())
    parts = shlex.split(command)
    assert json.loads(parts[parts.index("-d") + 1]) == {"customer": "o'neil"}


@pytest.mark.parametrize(
    ("scheme", "expected"),
    [
        (ApiKeyAuth(type="api_key", name="X", location="header", value_env="KEY"), "$KEY"),
        (BasicAuth(type="basic", username_env="U", password_env="P"), '-u "$U:$P"'),
    ],
)
def test_curl_uses_the_env_var_for_each_auth_type(scheme: Any, expected: str) -> None:
    command = curl_command(CALL, AuthConfig(scheme=scheme))
    assert expected in command
    assert MASK not in command


def test_curl_fills_an_api_key_in_the_query() -> None:
    scheme = ApiKeyAuth(type="api_key", name="key", location="query", value_env="KEY")
    call = {**CALL, "url": f"http://h/x?key={MASK}", "request_headers": {}}
    assert '"http://h/x?key=$KEY"' in curl_command(call, AuthConfig(scheme=scheme))


def test_login_auth_explains_where_the_token_comes_from() -> None:
    scheme = LoginAuth(type="login", op="login", body_env={"password": "PW"}, token_path="$.t")
    call = {**CALL, "request_headers": {"authorization": f"Bearer {MASK}"}}
    steps = curl_steps([call], AuthConfig(scheme=scheme))
    assert "Log in with login, using PW" in steps[0]
    assert '-H "authorization: Bearer $TOKEN"' in steps[-1]


def test_more_than_one_step_says_how_ids_carry_over() -> None:
    steps = curl_steps([CALL, CALL], None)
    assert "earlier response" in steps[0]
    assert len(steps) == 3


def context(tmp_path: Path, **evidence: Any) -> ToolContext:
    folder = tmp_path / ".workflowval"
    folder.mkdir(exist_ok=True)
    (folder / "evidence.json").write_text(json.dumps(evidence))
    return ToolContext(
        tmp_path, raw_spec=load_raw_spec(str(SEEDED / "openapi.json")), auth=bearer()
    )


REPORT = {
    "kind": "api_bug",
    "title": "Order create fails: with 500!",
    "test_id": "t1",
    "expected": "201",
    "actual": "500",
    "spec_reference": "createOrder",
}


def test_a_bug_report_has_every_section_and_is_recorded(tmp_path: Path) -> None:
    ctx = context(tmp_path, t1={"calls": [CALL], "tests": ["t1", "t2"]})
    result = TOOLS["write_bug_report"].invoke(REPORT, ctx)
    path = tmp_path / "reports" / "bugs" / "order-create-fails-with-500.md"
    assert str(path) in result
    text = path.read_text()
    for needed in (
        "# Order create fails",
        "## Steps to reproduce",
        "curl -i -X POST",
        "## Expected",
    ):
        assert needed in text
    assert "`t1`" in text
    assert "`t2`" in text
    assert '-H "authorization: Bearer $MY_TOKEN"' in text, "the steps must run as written"
    assert MASK not in text
    written = json.loads((tmp_path / ".workflowval" / "reports.json").read_text())
    assert written == {"t1": str(path)}


def test_a_conflict_report_has_its_extra_sections(tmp_path: Path) -> None:
    ctx = context(tmp_path, t1={"calls": [CALL]})
    arguments = {
        **REPORT,
        "kind": "conflict",
        "docs_excerpt": "at most 50",
        "affected_workflows": ["paging"],
        "suggested_resolution": "pick one",
    }
    TOOLS["write_bug_report"].invoke(arguments, ctx)
    text = next((tmp_path / "reports" / "bugs").glob("*.md")).read_text()
    for needed in ("## What the docs say", "at most 50", "`paging`", "pick one"):
        assert needed in text


def test_a_report_for_an_unknown_test_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="nothing was recorded"):
        TOOLS["write_bug_report"].invoke(REPORT, context(tmp_path))


def test_a_report_title_cannot_escape_the_folder(tmp_path: Path) -> None:
    ctx = context(tmp_path, t1={"calls": [CALL]})
    TOOLS["write_bug_report"].invoke({**REPORT, "title": "../../../evil"}, ctx)
    assert (tmp_path / "reports" / "bugs" / "evil.md").is_file()


def test_a_planted_secret_in_a_report_is_redacted(tmp_path: Path) -> None:
    ctx = context(tmp_path, t1={"calls": [CALL]})
    TOOLS["write_bug_report"].invoke(
        {**REPORT, "actual": "Authorization: Bearer abcdef123456"}, ctx
    )
    text = next((tmp_path / "reports" / "bugs").glob("*.md")).read_text()
    assert "abcdef123456" not in text


ORDER = {
    "id": 1,
    "customer": "a",
    "currency": "USD",
    "status": "pending",
    "items": [],
    "total_amount_cents": 5,
    "refundable_cents": 0,
    "refunded_cents": 0,
    "created_at": "2026-11-03T04:10:45Z",
}
OBSERVED = {
    "method": "POST",
    "template": "/orders",
    "status": 201,
    "content_type": "application/json",
    "raw_body": json.dumps(ORDER),
}
REQUIRED = "/components/schemas/Order/required"
RENAME = [
    {
        "op": "replace",
        "path": "/components/schemas/Order/required/5",
        "value": "total_amount_cents",
    },
    {
        "op": "move",
        "from": "/components/schemas/Order/properties/total_cents",
        "path": "/components/schemas/Order/properties/total_amount_cents",
    },
]


def patch_ctx(tmp_path: Path) -> ToolContext:
    return context(tmp_path, t1={"calls": [OBSERVED], "tests": ["t1"]})


def test_a_good_patch_is_saved_and_applies_to_the_original_spec(tmp_path: Path) -> None:
    ctx = patch_ctx(tmp_path)
    result = TOOLS["propose_spec_patch"].invoke({"test_id": "t1", "patch": RENAME}, ctx)
    path = tmp_path / "reports" / "patches" / "t1.json"
    assert str(path) in result
    saved = json.loads(path.read_text())
    assert saved[1]["from"] == "/components/schemas/Order/properties/total_cents"
    assert saved[0]["value"] == "total_amount_cents"


def test_a_patch_that_does_not_apply_is_refused(tmp_path: Path) -> None:
    patch = [{"op": "replace", "path": "/nothing/here", "value": 1}]
    with pytest.raises(ToolError, match="doesn't apply"):
        TOOLS["propose_spec_patch"].invoke({"test_id": "t1", "patch": patch}, patch_ctx(tmp_path))


def test_a_patch_that_leaves_the_response_invalid_is_refused(tmp_path: Path) -> None:
    patch = [{"op": "replace", "path": "/info/title", "value": "Other"}]
    with pytest.raises(ToolError, match="still breaks"):
        TOOLS["propose_spec_patch"].invoke({"test_id": "t1", "patch": patch}, patch_ctx(tmp_path))


def test_a_patch_that_breaks_the_spec_is_refused(tmp_path: Path) -> None:
    patch = [{"op": "remove", "path": "/paths"}]
    with pytest.raises(ToolError, match="isn't valid OpenAPI"):
        TOOLS["propose_spec_patch"].invoke({"test_id": "t1", "patch": patch}, patch_ctx(tmp_path))


def test_a_patch_is_refused_when_the_response_already_fits(tmp_path: Path) -> None:
    good = {**ORDER, "total_cents": ORDER["total_amount_cents"]}
    fits = {**OBSERVED, "raw_body": json.dumps(good)}
    ctx = context(tmp_path, t1={"calls": [fits]})
    patch = [{"op": "replace", "path": "/info/title", "value": "Other"}]
    with pytest.raises(ToolError, match="nothing to patch"):
        TOOLS["propose_spec_patch"].invoke({"test_id": "t1", "patch": patch}, ctx)


def test_a_patch_can_target_one_of_several_mismatches(tmp_path: Path) -> None:
    both = {**ORDER, "created_at": "11/03/2026"}
    observed = {**OBSERVED, "raw_body": json.dumps(both)}
    key = "'total_cents' is a required property"
    ctx = context(tmp_path, f1={"calls": [observed], "keys": [key]})
    result = TOOLS["propose_spec_patch"].invoke({"test_id": "f1", "patch": RENAME}, ctx)
    assert "wrote" in result


def test_the_original_spec_is_never_changed_by_a_patch(tmp_path: Path) -> None:
    ctx = patch_ctx(tmp_path)
    before = json.dumps(ctx.raw_spec)
    TOOLS["propose_spec_patch"].invoke({"test_id": "t1", "patch": RENAME}, ctx)
    assert json.dumps(ctx.raw_spec) == before


def test_read_spec_reads_the_spec_as_written(tmp_path: Path) -> None:
    from workflowval.spec import build_graph, load_spec

    ctx = ToolContext(
        tmp_path,
        build_graph(load_spec(str(SEEDED / "openapi.json"))),
        raw_spec=load_raw_spec(str(SEEDED / "openapi.json")),
    )
    pointer = "/paths/~1orders/post/responses/201/content/application~1json/schema"
    assert json.loads(TOOLS["read_spec"].invoke({"pointer": pointer}, ctx)) == {
        "$ref": "#/components/schemas/Order"
    }
    with pytest.raises(ToolError, match="nothing at"):
        TOOLS["read_spec"].invoke({"pointer": "/nope"}, ctx)


def sample_results() -> RunResults:
    def test(name: str, status: str, basis: str = "documented", **extra: Any) -> dict[str, Any]:
        return {
            "id": f"tests/wf/test_{name}.py::test_{name}",
            "workflow_id": name,
            "basis": basis,
            "status": status,
            "duration_s": 0.5,
            **extra,
        }

    def verdict(label: str, confidence: str = "med", **extra: Any) -> dict[str, Any]:
        return {"label": label, "confidence": confidence, "reason": f"why {label}", **extra}

    filed = {"kind": "bug_report", "path": "reports/bugs/x.md"}
    return RunResults.model_validate(
        {
            "stop_reason": "completed",
            "cost_usd": 0.01,
            "uncovered_operations": ["uploadFile"],
            "tests": [
                test("good", "passed"),
                test("known", "xfailed", verdict=verdict("API_BUG", action=filed)),
                test("guess", "xfailed", "assumed", verdict=verdict("API_BUG")),
                test("clash", "xfailed", verdict=verdict("CONFLICT")),
                test("open", "failed", failing_assertion="boom <&>"),
            ],
            "findings": [
                {"id": "a", "message": "$.x is wrong", "operations": ["op"], "label": "SPEC_DRIFT"},
                {"id": "b", "message": "$.y is loose", "operations": ["op"], "label": "TEST_BUG"},
                {"id": "c", "message": "$.z is odd", "operations": ["op"]},
            ],
        }
    )


def test_report_md_keeps_each_kind_of_result_apart() -> None:
    text = render_report(sample_results())
    assert "1 passed, 1 expected to fail (API_BUG)" not in text
    assert "| API_BUG | 2 |" in text
    assert "| SPEC_DRIFT | 1 |" in text
    assert "## Open conflicts between the spec and docs" in text
    documented = text.split("## Failures on documented rules")[1].split("##")[0]
    assumed = text.split("## Failures on assumed rules")[1].split("##")[0]
    assert "`known`" in documented
    assert "reports/bugs/x.md" in documented
    assert "`guess`" in assumed
    assert "`guess`" not in documented
    assert "not reviewed" in text
    assert "dismissed" in text.lower()
    assert "`uploadFile`" in text
    assert "`good`" not in text


def test_report_md_redacts() -> None:
    results = sample_results()
    results.tests[4].failing_assertion = "Authorization: Bearer abcdef123456"
    assert "abcdef123456" not in render_report(results)


def test_junit_is_valid_and_treats_only_unreviewed_failures_as_failures() -> None:
    root = ET.fromstring(render_junit(sample_results()))  # noqa: S314 - we just wrote this XML
    assert root.get("tests") == "5"
    assert root.get("failures") == "1"
    assert root.get("skipped") == "3"
    failing = [case for case in root if case.find("failure") is not None]
    assert ["test_open" in str(case.get("name")) for case in failing] == [True]
    assert "<&>" in str(failing[0].find("failure").get("message"))  # type: ignore[union-attr]


def test_write_reports_writes_all_three(tmp_path: Path) -> None:
    paths = write_reports(sample_results(), tmp_path / "out")
    assert sorted(path.name for path in paths) == ["junit.xml", "report.md", "results.json"]
    assert all(path.is_file() for path in paths)


def test_report_command_rebuilds_from_results(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    write_reports(sample_results(), tmp_path / "first")
    result = runner.invoke(
        cli.app, ["report", "first/results.json", "--report", str(tmp_path / "again")]
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "again" / "report.md").read_text() == (
        tmp_path / "first" / "report.md"
    ).read_text()


def test_report_command_refuses_a_missing_or_bad_file(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli.app, ["report", "nope.json"]).exit_code == 2
    (tmp_path / "bad.json").write_text("{}")
    assert runner.invoke(cli.app, ["report", "bad.json"]).exit_code == 2
