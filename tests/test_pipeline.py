"""End-to-end runs of the pipeline against a small local API, with scripted models.

The API runs on the loopback interface inside this test process. Nothing leaves the machine.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from workflowval import cli
from workflowval.gen import lint_source
from workflowval.loop import Budget, StopReason
from workflowval.models import FakeModel, Reply, call
from workflowval.pipeline import TestConfig, auth_env_names, run_test_pipeline
from workflowval.schemas import (
    ApiKeyAuth,
    AuthConfig,
    BasicAuth,
    BearerAuth,
    LoginAuth,
    Workflow,
)
from workflowval.spec import SpecError
from workflowval.testrun import is_reachable, run_tests
from workflowval.triage import MismatchGroup

pytestmark = pytest.mark.enable_socket
DONE = Reply(text="done")
TEST_ID = "tests/wf/test_item_lifecycle.py::test_item_lifecycle"
SECRET_PASSWORD = "planted-password-value"
SECRET_TOKEN = "planted-token-value"


def spec(**extra: Any) -> dict[str, Any]:
    item = {"type": "object", "properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}
    json_item = {"application/json": {"schema": item}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Items", "version": "1"},
        "paths": {
            "/login": {
                "post": {
                    "operationId": "login",
                    "responses": {"200": {"description": "token"}},
                }
            },
            "/items": {
                "post": {
                    "operationId": "createItem",
                    "requestBody": {"content": json_item},
                    "responses": {"201": {"description": "made", "content": json_item}},
                }
            },
            "/items/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                "get": {
                    "operationId": "getItem",
                    "responses": {"200": {"description": "it", "content": json_item}},
                },
                "delete": {
                    "operationId": "deleteItem",
                    "responses": {"204": {"description": "gone"}},
                },
            },
        },
        **extra,
    }


class ItemsApi:
    """A tiny in-memory API. `broken_get` plants a real bug."""

    def __init__(self) -> None:
        self.broken_get = False
        self.require_token = False
        self.valid_tokens = {SECRET_TOKEN}
        self.logins = 0
        self.items: dict[int, dict[str, Any]] = {}
        self.url = ""


def _make_handler(api: ItemsApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def _send(self, status: int, payload: Any = None) -> None:
            body = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length)) if length else None

        def _allowed(self) -> bool:
            if not api.require_token:
                return True
            cookie = self.headers.get("Cookie") or ""
            return cookie.removeprefix("token=") in api.valid_tokens

        def do_POST(self) -> None:
            body = self._body()
            if self.path == "/login":
                api.logins += 1
                ok = body == {"password": SECRET_PASSWORD}
                self._send(200, {"token": SECRET_TOKEN} if ok else {"reason": "bad"})
            elif not self._allowed():
                self._send(401)
            else:
                item_id = len(api.items) + 1
                api.items[item_id] = {"id": item_id, **(body or {})}
                self._send(201, api.items[item_id])

        def _item(self) -> int | None:
            match = re.fullmatch(r"/items/(\d+)", self.path.split("?")[0])
            return int(match.group(1)) if match else None

        def do_GET(self) -> None:
            item_id = self._item()
            if self.path.split("?")[0] == "/":
                self._send(200, {})
            elif api.broken_get:
                self._send(500, {"error": "boom"})
            elif item_id in api.items:
                self._send(200, api.items[item_id])
            else:
                self._send(404)

        def do_DELETE(self) -> None:
            item_id = self._item()
            if not self._allowed():
                self._send(401)
            elif item_id in api.items:
                del api.items[item_id]
                self._send(204)
            else:
                self._send(404)

    return Handler


@pytest.fixture
def items_api() -> Iterator[ItemsApi]:
    api = ItemsApi()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(api))
    api.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield api
    server.shutdown()
    server.server_close()


def lifecycle(**changes: Any) -> dict[str, Any]:
    return {
        "id": "item_lifecycle",
        "source": "planner",
        "basis": "documented",
        "rule": "An item can be created, read and deleted",
        "steps": [
            {
                "op": "createItem",
                "body": {"name": "n-{unique}"},
                "save": {"item": "$.id"},
                "expect": {"status": 201},
            },
            {"op": "getItem", "params": {"id": "{item}"}, "expect": {"status": 200}},
            {"op": "deleteItem", "params": {"id": "{item}"}, "expect": {"status": 204}},
        ],
        "cleanup": [{"op": "deleteItem", "params": {"id": "{item}"}}],
        **changes,
    }


def verdict(label: str, **changes: Any) -> Reply:
    return call(
        "write_verdict",
        test_id=TEST_ID,
        label=label,
        confidence="high",
        reason=f"reason for {label}",
        **changes,
    )


def config(
    api: ItemsApi,
    workdir: Path,
    planner: list[Reply] | None = None,
    reviewer: list[Reply] | None = None,
    writer: list[Reply] | None = None,
    specification: dict[str, Any] | None = None,
    **options: Any,
) -> TestConfig:
    path = workdir / "openapi.json"
    if not path.exists() or specification:
        path.write_text(json.dumps(specification or spec()))
    return TestConfig(
        spec_source=str(path),
        base_url=api.url,
        workdir=workdir,
        models={
            "planner": FakeModel(planner or []),
            "reviewer": FakeModel(reviewer or []),
            "writer": FakeModel(writer or []),
        },
        **options,
    )


PLANNED = [call("write_workflow", workflow=lifecycle()), DONE, DONE]


def test_green_run_writes_plain_tests_results_and_stats(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    report = run_test_pipeline(config(items_api, tmp_path, PLANNED))
    assert report.run.stop_reason is StopReason.COMPLETED
    assert report.run.tests == {TEST_ID: "passed"}
    assert report.results.label_counts == {}
    assert items_api.items == {}, "the test must clean up what it created"
    test_file = tmp_path / "tests" / "wf" / "test_item_lifecycle.py"
    lint_source(test_file.read_text())
    saved = json.loads((tmp_path / "results.json").read_text())
    assert saved["tests"][0]["status"] == "passed"
    assert saved["uncovered_operations"] == ["login"]
    exchanges = saved["tests"][0]["exchanges"]
    assert [exchange["method"] for exchange in exchanges] == ["POST", "GET", "DELETE"]
    stats = json.loads((tmp_path / ".workflowval" / "stats.json").read_text())
    assert stats["roles"]["planner"]["tool_calls"] == {"write_workflow": 1}


def test_generated_tests_run_with_plain_pytest_and_no_workflowval(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    # -I ignores PYTHONPATH and user paths; the guard fails if workflowval gets imported at all.
    code = (
        "import sys, pytest\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] == 'workflowval':\n"
        "            raise ImportError('workflowval must not be needed')\n"
        "sys.meta_path.insert(0, Block())\n"
        "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', '.']))\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=tmp_path / "tests" / "wf",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout


def test_a_real_failure_is_labeled_and_never_changed(items_api: ItemsApi, tmp_path: Path) -> None:
    items_api.broken_get = True
    # The workflow file is untouched: Planner and Writer have no replies scripted, so any fix fails.
    report = run_test_pipeline(
        config(items_api, tmp_path, PLANNED, [verdict("API_BUG"), DONE, DONE])
    )
    assert report.run.tests == {TEST_ID: "API_BUG"}
    test = report.results.tests[0]
    assert test.fix_attempts == 0
    assert test.verdict is not None
    assert test.verdict.confidence.value == "high"
    assert test.verdict.reason == "reason for API_BUG"
    assert test.verdict.evidence.exchanges[-1].status == 500
    assert report.expectation_changes == []
    assert items_api.items == {}, "cleanup must still run when a test fails"


def test_a_wrong_expectation_is_fixed_by_the_planner_and_shown(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    wrong = lifecycle()
    wrong["steps"][0]["expect"]["status"] = 200
    planner = [
        call("write_workflow", workflow=wrong),
        DONE,
        DONE,
        call("write_workflow", workflow=lifecycle()),
        DONE,
    ]
    reviewer = [verdict("TEST_BUG", fix_target="workflow"), DONE]
    report = run_test_pipeline(config(items_api, tmp_path, planner, reviewer))
    assert report.run.tests == {TEST_ID: "passed"}
    assert report.run.state.fix_attempts == {TEST_ID: 1}
    assert report.expectation_changes == [
        "item_lifecycle: steps[0] createItem: status 200 became 201"
    ]


def test_max_fix_attempts_one_patches_once_then_unresolved(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    wrong = lifecycle()
    wrong["steps"][0]["expect"]["status"] = 200
    planner = [call("write_workflow", workflow=wrong), DONE, DONE, DONE]
    reviewer = [verdict("TEST_BUG"), DONE, verdict("TEST_BUG"), DONE]
    cfg = config(items_api, tmp_path, planner, reviewer, budget=Budget(max_fix_attempts=1))
    report = run_test_pipeline(cfg)
    assert report.run.tests == {TEST_ID: "UNRESOLVED"}
    assert report.run.state.fix_attempts == {TEST_ID: 1}
    test = report.results.tests[0]
    assert test.verdict is not None
    assert test.verdict.label.value == "UNRESOLVED"
    assert "not resolved" in test.verdict.reason


def test_a_test_code_bug_goes_to_the_writer(items_api: ItemsApi, tmp_path: Path) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    test_file = tmp_path / "tests" / "wf" / "test_item_lifecycle.py"
    good = test_file.read_text()
    test_file.write_text(good.replace("== 201", "== 299"))
    # Same workflow, so the broken file is kept. Only the Writer has replies, so only it can fix it.
    writer = [call("write_test", workflow_id="item_lifecycle", source=good), DONE]
    reviewer = [verdict("TEST_BUG", fix_target="test"), DONE]
    cfg = config(items_api, tmp_path, [], reviewer, writer)
    report = run_test_pipeline(cfg)
    model: Any = cfg.models["writer"]
    assert len(model.requests) == 2
    assert report.run.tests == {TEST_ID: "passed"}
    assert report.run.state.fix_attempts == {TEST_ID: 1}
    assert report.planned is False


def test_a_missing_verdict_ends_unresolved(items_api: ItemsApi, tmp_path: Path) -> None:
    items_api.broken_get = True
    report = run_test_pipeline(config(items_api, tmp_path, PLANNED, [DONE]))
    assert report.run.tests == {TEST_ID: "UNRESOLVED"}


def test_an_unreachable_api_stops_the_run_before_labeling_anything(tmp_path: Path) -> None:
    api = ItemsApi()
    api.url = "http://127.0.0.1:9"
    report = run_test_pipeline(config(api, tmp_path, PLANNED))
    assert report.run.stop_reason is StopReason.API_UNREACHABLE
    assert "no response" in report.run.detail
    assert report.results.tests == []


def test_plan_only_stops_after_workflow_files(items_api: ItemsApi, tmp_path: Path) -> None:
    report = run_test_pipeline(config(items_api, tmp_path, PLANNED, plan_only=True))
    assert (tmp_path / "workflows" / "item_lifecycle.json").is_file()
    assert not (tmp_path / "tests").exists()
    assert report.run.state.completed_steps == ["plan"]


def test_user_edits_are_kept_and_the_test_regenerated_without_replanning(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED, plan_only=True))
    path = tmp_path / "workflows" / "item_lifecycle.json"
    edited = json.loads(path.read_text())
    edited["rule"] = "Edited by the user"
    path.write_text(json.dumps(edited))
    report = run_test_pipeline(config(items_api, tmp_path))
    assert report.planned is False
    test_file = tmp_path / "tests" / "wf" / "test_item_lifecycle.py"
    assert "Edited by the user" in test_file.read_text()
    assert json.loads(path.read_text())["rule"] == "Edited by the user"


def test_a_spec_change_triggers_replanning_and_keeps_locked_files(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    path = tmp_path / "workflows" / "item_lifecycle.json"
    locked = lifecycle(source="user", locked=True, rule="Mine")
    path.write_text(json.dumps(locked))
    attempt = call("write_workflow", workflow=lifecycle(rule="Agent rewrite"))
    changed = spec(info={"title": "Items v2", "version": "2"})
    report = run_test_pipeline(
        config(items_api, tmp_path, [attempt, DONE, DONE], specification=changed)
    )
    assert report.planned is True
    assert json.loads(path.read_text())["rule"] == "Mine"
    assert report.run.tests == {TEST_ID: "passed"}


def test_a_run_killed_during_planning_replans(items_api: ItemsApi, tmp_path: Path) -> None:
    first = config(items_api, tmp_path, [call("write_workflow", workflow=lifecycle())])
    with pytest.raises(RuntimeError, match="script ran out"):
        run_test_pipeline(first)
    report = run_test_pipeline(config(items_api, tmp_path, PLANNED))
    assert report.planned is True


def test_coverage_gaps_get_a_second_chance_then_are_reported(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    only_create = {
        "id": "create_only",
        "source": "planner",
        "basis": "assumed",
        "rule": "Items can be created",
        "steps": [{"op": "createItem", "save": {"item": "$.id"}, "expect": {"status": 201}}],
        "cleanup": [{"op": "deleteItem", "params": {"id": "{item}"}}],
    }
    model = [call("write_workflow", workflow=only_create), DONE, DONE]
    cfg = config(items_api, tmp_path, model)
    report = run_test_pipeline(cfg)
    planner: Any = cfg.models["planner"]
    follow_up = planner.requests[2][0][1].content
    assert "getItem" in follow_up
    assert report.results.uncovered_operations == ["login", "getItem"]


def test_a_planner_that_writes_nothing_stops_the_run(items_api: ItemsApi, tmp_path: Path) -> None:
    report = run_test_pipeline(config(items_api, tmp_path, [DONE, DONE]))
    assert report.run.stop_reason is StopReason.INVALID_OUTPUT
    assert "no workflows" in report.run.detail


def test_a_workflow_that_cannot_render_stops_the_run(items_api: ItemsApi, tmp_path: Path) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED, plan_only=True))
    path = tmp_path / "workflows" / "item_lifecycle.json"
    broken = lifecycle()
    broken["steps"][0]["op"] = "noSuchOperation"
    path.write_text(json.dumps(broken))
    report = run_test_pipeline(config(items_api, tmp_path))
    assert report.run.stop_reason is StopReason.INVALID_OUTPUT
    assert "noSuchOperation" in report.run.detail


def test_invalid_spec_fails_before_any_model_call(items_api: ItemsApi, tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("not json")
    cfg = config(items_api, tmp_path)
    cfg = TestConfig(**{**vars(cfg), "spec_source": str(tmp_path / "bad.json")})
    with pytest.raises(SpecError, match="convert YAML"):
        run_test_pipeline(cfg)


def login_auth() -> AuthConfig:
    return AuthConfig(
        scheme=LoginAuth(
            type="login", op="login", body_env={"password": "ITEMS_PASS"}, token_path="$.token"
        )
    )


COOKIE_SPEC = spec(
    components={"securitySchemes": {"c": {"type": "apiKey", "in": "cookie", "name": "token"}}}
)


def tree_text(root: Path) -> str:
    return "\n".join(
        path.read_text(errors="replace")
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in str(path)
    )


def test_login_flow_works_and_no_secret_reaches_any_file(
    items_api: ItemsApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ITEMS_PASS", SECRET_PASSWORD)
    items_api.require_token = True
    report = run_test_pipeline(
        config(items_api, tmp_path, PLANNED, specification=COOKIE_SPEC, auth=login_auth())
    )
    assert report.run.tests == {TEST_ID: "passed"}
    assert items_api.logins == 1, "one login, with the token reused across steps"
    text = tree_text(tmp_path)
    assert SECRET_PASSWORD not in text
    assert SECRET_TOKEN not in text
    assert "[REDACTED]" in text
    headers = report.results.tests[0].exchanges[0].request_headers
    assert headers["cookie"] == "[REDACTED]"


def test_an_expired_token_is_refreshed_on_401(
    items_api: ItemsApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ITEMS_PASS", SECRET_PASSWORD)
    items_api.require_token = True
    run_test_pipeline(
        config(items_api, tmp_path, PLANNED, specification=COOKIE_SPEC, auth=login_auth())
    )
    before = items_api.logins
    items_api.valid_tokens = _RefreshingTokens()
    outcome = run_tests(
        tmp_path / "tests" / "wf", base_url=items_api.url, env_names=["ITEMS_PASS"]
    )[0]
    assert outcome.passed, outcome.failing_assertion
    assert items_api.logins > before + 1, "a rejected token must trigger another login"


class _RefreshingTokens(set):  # type: ignore[type-arg]
    """Rejects the token until it's been rejected once, like an expired session."""

    def __init__(self) -> None:
        super().__init__()
        self.rejected = False

    def __contains__(self, item: object) -> bool:
        if not self.rejected:
            self.rejected = True
            return False
        return True


def test_a_failed_login_gives_a_clear_failure(
    items_api: ItemsApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ITEMS_PASS", "wrong-password")
    reviewer = [verdict("TEST_BUG"), DONE, verdict("API_BUG"), DONE]
    report = run_test_pipeline(
        config(
            items_api,
            tmp_path,
            [*PLANNED, DONE],
            reviewer,
            specification=COOKIE_SPEC,
            auth=login_auth(),
        )
    )
    outcome = report.results.tests[0]
    assert "no value at $.token" in (outcome.failing_assertion or "")


def test_test_subprocess_only_gets_the_env_vars_it_needs(
    items_api: ItemsApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_PROVIDER_API_KEY", "planted-provider-key")
    monkeypatch.setenv("ITEMS_PASS", "visible-to-tests")
    tests = tmp_path / "wf"
    tests.mkdir()
    (tests / "conftest.py").write_text(
        "import json, os\n"
        "def pytest_sessionfinish():\n"
        "    json.dump({'test_a.py::test_a': {'status': 'passed',"
        " 'stdout': json.dumps(dict(os.environ))}}, open(os.environ['WORKFLOWVAL_CAPTURE'], 'w'))\n"
    )
    (tests / "test_a.py").write_text("def test_a():\n    pass\n")
    outcome = run_tests(tests, base_url=items_api.url, env_names=["ITEMS_PASS"])[0]
    env = json.loads(outcome.stdout)
    assert "SOME_PROVIDER_API_KEY" not in env
    assert env["WORKFLOWVAL_BASE_URL"] == items_api.url


def test_no_tests_means_no_outcomes(tmp_path: Path) -> None:
    assert run_tests(tmp_path, base_url=None) == []


def test_a_test_that_never_reports_is_an_error_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def test_a():\n    pass\n")
    outcome = run_tests(tmp_path, base_url=None)[0]
    assert outcome.status == "error"
    assert not outcome.passed


def test_a_hung_run_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("workflowval.testrun.PER_TEST_TIMEOUT_SECONDS", 0.5)
    (tmp_path / "test_a.py").write_text("import time\n\n\ndef test_a():\n    time.sleep(30)\n")
    outcome = run_tests(tmp_path, base_url=None)[0]
    assert outcome.status == "error"
    assert outcome.failing_assertion == "the test run timed out"


def test_a_file_that_cannot_import_is_reported(items_api: ItemsApi, tmp_path: Path) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    tests = tmp_path / "tests" / "wf"
    (tests / "test_item_lifecycle.py").write_text("def broken(:\n")
    outcome = run_tests(tests, base_url=items_api.url)[0]
    assert outcome.status == "error"
    assert "SyntaxError" in (outcome.failing_assertion or "")


def test_a_failed_cleanup_cannot_change_a_test_result(items_api: ItemsApi, tmp_path: Path) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    tests = tmp_path / "tests" / "wf"
    (tests / "test_cleanup.py").write_text(
        'def test_cleanup(api):\n    api.cleanup("GET", "/gone")\n    assert api.unique\n'
    )
    # A base URL nothing listens on makes the cleanup request fail with a connection error.
    outcome = run_tests(tests, base_url="http://127.0.0.1:9", workflow_ids=["cleanup"])[0]
    assert outcome.passed, outcome.failing_assertion


def test_path_values_are_escaped_so_they_cannot_change_the_route(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    tests = tmp_path / "tests" / "wf"
    (tests / "test_escape.py").write_text(
        "def test_escape(api):\n"
        '    response = api.call("GET", "/items/{id}", path={"id": "1/../../x?y=1"})\n'
        "    assert response.request.url.raw_path == b'/items/1%2F..%2F..%2Fx%3Fy%3D1'\n"
    )
    outcome = run_tests(tests, base_url=items_api.url, workflow_ids=["escape"])[0]
    assert outcome.passed, outcome.failing_assertion


def test_is_reachable(items_api: ItemsApi) -> None:
    assert is_reachable(items_api.url)
    assert not is_reachable("http://127.0.0.1:9")


@pytest.mark.parametrize(
    ("scheme", "expected"),
    [
        (BearerAuth(type="bearer", token_env="T"), ["T"]),
        (BasicAuth(type="basic", username_env="U", password_env="P"), ["P", "U"]),
        (ApiKeyAuth(type="api_key", name="k", location="query", value_env="K"), ["K"]),
        (LoginAuth(type="login", op="o", body_env={"a": "A"}, token_path="$.t"), ["A"]),
    ],
)
def test_auth_env_names(scheme: Any, expected: list[str]) -> None:
    assert auth_env_names(AuthConfig(scheme=scheme)) == expected


def test_no_auth_needs_no_env_names() -> None:
    assert auth_env_names(None) == []


# ---- Credentials in generated clients


@pytest.mark.parametrize(
    ("scheme", "env"),
    [
        ({"type": "bearer", "token_env": "T_ENV"}, {"T_ENV": SECRET_TOKEN}),
        (
            {"type": "basic", "username_env": "U_ENV", "password_env": "P_ENV"},
            {"U_ENV": "user-name", "P_ENV": SECRET_PASSWORD},
        ),
        (
            {"type": "api_key", "name": "X-Key", "location": "header", "value_env": "K_ENV"},
            {"K_ENV": SECRET_TOKEN},
        ),
        (
            {"type": "api_key", "name": "key", "location": "query", "value_env": "K_ENV"},
            {"K_ENV": SECRET_TOKEN},
        ),
    ],
)
def test_other_auth_types_run_and_stay_out_of_every_file(
    scheme: dict[str, Any],
    env: dict[str, str],
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    auth = AuthConfig.model_validate({"scheme": scheme})
    report = run_test_pipeline(config(items_api, tmp_path, PLANNED, auth=auth))
    assert report.run.tests == {TEST_ID: "passed"}
    text = tree_text(tmp_path)
    for value in env.values():
        if value != "user-name":
            assert value not in text


# ---- The command line


runner = CliRunner()


def invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]) -> Any:
    monkeypatch.chdir(tmp_path)
    return runner.invoke(cli.app, args)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def install(**scripts: list[Reply]) -> None:
        def build(*_: Any) -> dict[str, FakeModel]:
            return {r: FakeModel(scripts.get(r, [])) for r in ("planner", "reviewer", "writer")}

        monkeypatch.setattr(cli, "_build_models", build)

    return install


def test_cli_test_prints_results_and_exits_zero_when_green(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    scripted(planner=PLANNED)
    result = invoke(tmp_path, monkeypatch, ["test", "s.json", "--base-url", items_api.url])
    assert result.exit_code == 0, result.output
    assert "PASSED      item_lifecycle" in result.output
    assert "NOT COVERED login" in result.output
    assert "stopped: completed" in result.output


def test_cli_exits_zero_on_a_reviewed_failure_and_lists_it_apart_from_passes(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    items_api.broken_get = True
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    scripted(planner=PLANNED, reviewer=[verdict("API_BUG"), DONE, DONE])
    result = invoke(tmp_path, monkeypatch, ["test", "s.json", "--base-url", items_api.url])
    assert result.exit_code == 0, result.output
    assert "EXPECTED    item_lifecycle (API_BUG): reason for API_BUG" in result.output
    assert "0 passed, 1 expected to fail (API_BUG)" in result.output


def test_cli_exits_non_zero_when_a_failure_has_no_label(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    items_api.broken_get = True
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    scripted(planner=PLANNED, reviewer=[DONE])
    result = invoke(tmp_path, monkeypatch, ["test", "s.json", "--base-url", items_api.url])
    assert result.exit_code == cli.EXIT_FAILURES
    assert "UNRESOLVED" in result.output
    assert "1 failed" in result.output


def test_cli_shows_expectation_changes(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    wrong = lifecycle()
    wrong["steps"][0]["expect"]["status"] = 200
    planner = [
        call("write_workflow", workflow=wrong),
        DONE,
        DONE,
        call("write_workflow", workflow=lifecycle()),
        DONE,
    ]
    scripted(planner=planner, reviewer=[verdict("TEST_BUG"), DONE])
    result = invoke(tmp_path, monkeypatch, ["test", "s.json", "--base-url", items_api.url])
    assert "CHANGED     item_lifecycle: steps[0] createItem: status 200 became 201" in result.output


def test_cli_plan_then_run_uses_no_model(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    scripted(planner=PLANNED)
    planned = invoke(tmp_path, monkeypatch, ["plan", "s.json"])
    assert planned.exit_code == 0, planned.output
    assert (tmp_path / "workflows" / "item_lifecycle.json").is_file()
    again = invoke(tmp_path, monkeypatch, ["plan", "s.json"])
    assert "existing workflow files were kept" in again.output
    scripted(planner=[])
    tested = invoke(tmp_path, monkeypatch, ["test", "s.json", "--base-url", items_api.url])
    assert tested.exit_code == 0, tested.output
    rerun = invoke(tmp_path, monkeypatch, ["run", "tests/wf", "--base-url", items_api.url])
    assert rerun.exit_code == 0, rerun.output
    assert "PASSED      item_lifecycle" in rerun.output
    items_api.broken_get = True
    failed = invoke(tmp_path, monkeypatch, ["run", "tests/wf", "--base-url", items_api.url])
    assert failed.exit_code == cli.EXIT_FAILURES
    assert "FAILED" in failed.output


def test_cli_plan_failure_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted: Callable[..., None]
) -> None:
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    scripted(planner=[DONE, DONE])
    assert invoke(tmp_path, monkeypatch, ["plan", "s.json"]).exit_code == cli.EXIT_FAILURES


def test_cli_run_refuses_a_missing_folder_or_empty_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke(tmp_path, monkeypatch, ["run", "nope"]).exit_code == 2
    (tmp_path / "empty").mkdir()
    result = invoke(tmp_path, monkeypatch, ["run", "empty"])
    assert result.exit_code == cli.EXIT_FAILURES


def test_cli_inspect_exports_the_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    result = invoke(tmp_path, monkeypatch, ["inspect", "s.json"])
    assert result.exit_code == 0
    graph = json.loads(result.output)
    assert {"producer": "createItem", "field": "id", "consumer": "getItem", "parameter": "id"} in (
        graph["links"]
    )


def test_cli_inspect_reports_a_bad_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = invoke(tmp_path, monkeypatch, ["inspect", "missing.json"])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_cli_test_reports_a_bad_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted: Callable[..., None]
) -> None:
    scripted()
    result = invoke(
        tmp_path, monkeypatch, ["test", "missing.json", "--base-url", "http://localhost"]
    )
    assert result.exit_code == 2
    assert "not found" in result.output


def test_cli_needs_a_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOWVAL_MODEL", raising=False)
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    result = invoke(tmp_path, monkeypatch, ["test", "s.json", "--base-url", "http://localhost"])
    assert result.exit_code == 2
    assert "WORKFLOWVAL_MODEL" in result.output


def test_build_models_gives_each_role_its_model() -> None:
    choice = cli.ModelChoice("a/default", {"writer": "b/cheap"})
    models = cli._build_models(choice, 1, 0.5, False)
    assert models["writer"].name == "b/cheap"
    assert models["planner"].name == "a/default"


@pytest.mark.parametrize(
    ("url", "extra", "message"),
    [
        ("https://prod.example.com", [], "--allow-remote"),
        ("https://prod.example.com", ["--allow-remote"], "--yes"),
        ("http://localhost:1", ["--allow-host", "other"], "--allow-host"),
        ("not-a-url", [], "full URL"),
    ],
)
def test_cli_protects_hosts_before_doing_anything(
    url: str, extra: list[str], message: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = invoke(
        tmp_path,
        monkeypatch,
        ["test", "s.json", "--base-url", url, "--model", "a/b", *extra],
    )
    assert result.exit_code == 2
    assert message in result.output


def test_remote_host_is_allowed_with_both_flags() -> None:
    cli.check_host("https://prod.example.com", None, allow_remote=True, yes=True)


def test_cli_reports_missing_auth_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ITEMS_PASS", raising=False)
    (tmp_path / "auth.json").write_text(login_auth().model_dump_json())
    result = invoke(
        tmp_path,
        monkeypatch,
        [
            "test",
            "s.json",
            "--base-url",
            "http://localhost",
            "--auth",
            "auth.json",
            "--model",
            "a/b",
        ],
    )
    assert result.exit_code == 2
    assert "ITEMS_PASS" in result.output


def test_cli_reports_a_bad_auth_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "auth.json").write_text('{"scheme": {"type": "oauth"}}')
    result = invoke(
        tmp_path,
        monkeypatch,
        ["run", ".", "--auth", "auth.json"],
    )
    assert result.exit_code == 2
    assert "can't read auth config" in result.output


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [(False, "", True), (True, "", False), (False, "off", False), (False, "on", True)],
)
def test_cost_tracking_switch(
    flag: bool, env: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKFLOWVAL_COST_TRACKING", env)
    assert cli._cost_tracked(flag) is expected


def marked_run(items_api: ItemsApi, tmp_path: Path) -> Any:
    items_api.broken_get = True
    return run_test_pipeline(config(items_api, tmp_path, PLANNED, [verdict("API_BUG"), DONE, DONE]))


def read_workflow(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "workflows" / "item_lifecycle.json"
    workflow: dict[str, Any] = json.loads(path.read_text())
    return workflow


def run_saved(items_api: ItemsApi, tmp_path: Path) -> Any:
    return run_tests(tmp_path / "tests" / "wf", base_url=items_api.url)[0]


def test_a_reviewed_failure_is_pinned_to_its_step_and_status(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    report = marked_run(items_api, tmp_path)
    issue = read_workflow(tmp_path)["known_issue"]
    assert issue["label"] == "API_BUG"
    assert issue["step"] == 1
    assert issue["check"] == "status"
    assert issue["status"] == 500
    test = report.results.tests[0]
    assert test.status.value == "xfailed"
    assert test.verdict.label.value == "API_BUG"
    assert report.results.label_counts == {"API_BUG": 1}
    assert not report.results.has_unexpected_failures
    assert report.results.summary() == "0 passed, 1 expected to fail (API_BUG)"
    assert items_api.items == {}, "cleanup must still run for an expected failure"


def test_marking_leaves_every_expectation_alone(items_api: ItemsApi, tmp_path: Path) -> None:
    marked_run(items_api, tmp_path)
    saved = Workflow.model_validate(read_workflow(tmp_path))
    assert saved.steps == Workflow.model_validate(lifecycle()).steps
    assert (
        "assert response.status_code == 200"
        in (tmp_path / "tests" / "wf" / "test_item_lifecycle.py").read_text()
    )


def test_a_marked_failure_needs_no_model_on_the_next_run(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    marked_run(items_api, tmp_path)
    report = run_test_pipeline(config(items_api, tmp_path))
    assert report.results.tests[0].status.value == "xfailed"
    assert report.results.tests[0].verdict.reason == "reason for API_BUG"
    assert report.run.state.steps_used == 0


def test_plain_pytest_also_treats_the_marked_failure_as_expected(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    marked_run(items_api, tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "."],
        cwd=tmp_path / "tests" / "wf",
        capture_output=True,
        text=True,
        check=False,
        env={"WORKFLOWVAL_BASE_URL": items_api.url, "PATH": ""},
    )
    assert result.returncode == 0, result.stdout
    assert "1 xfailed" in result.stdout


def test_a_different_failure_is_not_excused(items_api: ItemsApi, tmp_path: Path) -> None:
    marked_run(items_api, tmp_path)
    workflow = read_workflow(tmp_path)
    workflow["known_issue"]["status"] = 503
    (tmp_path / "workflows" / "item_lifecycle.json").write_text(json.dumps(workflow))
    report = run_test_pipeline(config(items_api, tmp_path, reviewer=[DONE]))
    assert report.results.tests[0].status.value == "failed"
    assert report.results.has_unexpected_failures


def test_a_failure_at_another_step_is_not_excused(items_api: ItemsApi, tmp_path: Path) -> None:
    marked_run(items_api, tmp_path)
    workflow = read_workflow(tmp_path)
    workflow["known_issue"]["step"] = 2
    (tmp_path / "workflows" / "item_lifecycle.json").write_text(json.dumps(workflow))
    report = run_test_pipeline(config(items_api, tmp_path, reviewer=[DONE]))
    assert report.results.tests[0].status.value == "failed"


def test_a_fixed_bug_passes_is_reported_and_loses_its_mark(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    marked_run(items_api, tmp_path)
    items_api.broken_get = False
    assert run_saved(items_api, tmp_path).looks_fixed
    report = run_test_pipeline(config(items_api, tmp_path))
    assert report.results.tests[0].status.value == "passed"
    assert report.results.tests[0].verdict is None
    assert report.looks_fixed == ["item_lifecycle: reason for API_BUG"]
    assert "known_issue" not in read_workflow(tmp_path)
    assert (
        "expected_failure" not in (tmp_path / "tests" / "wf" / "test_item_lifecycle.py").read_text()
    )


def test_plain_pytest_says_when_a_marked_test_passes(items_api: ItemsApi, tmp_path: Path) -> None:
    marked_run(items_api, tmp_path)
    items_api.broken_get = False
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "."],
        cwd=tmp_path / "tests" / "wf",
        capture_output=True,
        text=True,
        check=False,
        env={"WORKFLOWVAL_BASE_URL": items_api.url, "PATH": ""},
    )
    assert result.returncode == 0, result.stdout
    assert "this may be fixed: API_BUG: reason for API_BUG" in result.stdout


def test_a_wrong_value_is_pinned_to_the_body_check(items_api: ItemsApi, tmp_path: Path) -> None:
    steps = lifecycle()["steps"]
    steps[1]["expect"]["body"] = {"$.name": "something else"}
    planned = [call("write_workflow", workflow=lifecycle(steps=steps)), DONE, DONE]
    report = run_test_pipeline(
        config(items_api, tmp_path, planned, [verdict("SPEC_DRIFT"), DONE, DONE])
    )
    issue = read_workflow(tmp_path)["known_issue"]
    assert (issue["step"], issue["check"]) == (1, "body")
    assert "status" not in issue
    assert report.results.tests[0].status.value == "xfailed"


def test_a_test_bug_or_unresolved_failure_is_never_marked(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    items_api.broken_get = True
    report = run_test_pipeline(
        config(items_api, tmp_path, PLANNED, [DONE], budget=Budget(max_fix_attempts=1))
    )
    assert "known_issue" not in read_workflow(tmp_path)
    assert report.results.has_unexpected_failures


def test_a_locked_workflow_is_never_marked(items_api: ItemsApi, tmp_path: Path) -> None:
    items_api.broken_get = True
    folder = tmp_path / "workflows"
    folder.mkdir()
    locked = lifecycle(source="user", locked=True)
    (folder / "item_lifecycle.json").write_text(json.dumps(locked))
    before = (folder / "item_lifecycle.json").read_text()
    report = run_test_pipeline(
        config(items_api, tmp_path, [DONE, DONE], [verdict("API_BUG"), DONE, DONE])
    )
    assert (folder / "item_lifecycle.json").read_text() == before
    assert report.results.tests[0].status.value == "failed"
    assert report.results.tests[0].verdict.label.value == "API_BUG"
    assert report.results.has_unexpected_failures


def test_a_test_the_writer_fixed_is_not_regenerated_by_marking(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    test_file = tmp_path / "tests" / "wf" / "test_item_lifecycle.py"
    edited = test_file.read_text() + "# fixed by the Writer\n"
    test_file.write_text(edited)
    items_api.broken_get = True
    report = run_test_pipeline(
        config(items_api, tmp_path, reviewer=[verdict("API_BUG"), DONE, DONE])
    )
    assert test_file.read_text() == edited
    assert "known_issue" not in read_workflow(tmp_path)
    assert report.results.tests[0].status.value == "failed"
    assert report.results.tests[0].verdict.label.value == "API_BUG"


def test_cli_run_uses_no_model_and_counts_expected_failures(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked_run(items_api, tmp_path)
    result = invoke(tmp_path, monkeypatch, ["run", "tests/wf", "--base-url", items_api.url])
    assert result.exit_code == 0, result.output
    assert "EXPECTED    item_lifecycle: API_BUG: reason for API_BUG" in result.output
    assert "0 passed, 1 expected to fail, 0 failed" in result.output


def test_cli_run_says_when_a_marked_test_looks_fixed(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked_run(items_api, tmp_path)
    items_api.broken_get = False
    result = invoke(tmp_path, monkeypatch, ["run", "tests/wf", "--base-url", items_api.url])
    assert result.exit_code == 0, result.output
    assert "LOOKS FIXED item_lifecycle" in result.output


def test_cli_run_fails_on_a_different_failure(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked_run(items_api, tmp_path)
    test_file = tmp_path / "tests" / "wf" / "test_item_lifecycle.py"
    test_file.write_text(test_file.read_text().replace("== 500", "== 503"))
    result = invoke(tmp_path, monkeypatch, ["run", "tests/wf", "--base-url", items_api.url])
    assert result.exit_code == cli.EXIT_FAILURES
    assert "1 failed" in result.output


def test_a_real_failure_gets_one_report_linked_from_its_mark(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    items_api.broken_get = True
    report_call = call(
        "write_bug_report",
        kind="api_bug",
        title="Reading an item fails",
        test_id=TEST_ID,
        expected="200",
        actual="500",
        spec_reference="getItem",
    )
    report = run_test_pipeline(
        config(items_api, tmp_path, PLANNED, [verdict("API_BUG"), DONE, report_call, DONE])
    )
    path = tmp_path / "reports" / "bugs" / "reading-an-item-fails.md"
    text = path.read_text()
    assert "curl -i -X POST" in text, "the step that created the item"
    assert "curl -i -X GET" in text, "the step that failed"
    assert read_workflow(tmp_path)["known_issue"]["report"] == str(path)
    action = report.results.tests[0].verdict.action
    assert (action.kind.value, action.path) == ("bug_report", str(path))
    assert (tmp_path / "reports" / "report.md").is_file()
    assert (tmp_path / "reports" / "junit.xml").is_file()
    assert (tmp_path / "reports" / "results.json").is_file()


def two_workflows() -> list[Reply]:
    second = lifecycle(id="item_again")
    return [
        call("write_workflow", workflow=lifecycle()),
        call("write_workflow", workflow=second),
        DONE,
        DONE,
    ]


def test_one_root_cause_seen_by_two_tests_gets_one_report(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    items_api.broken_get = True
    other = "tests/wf/test_item_again.py::test_item_again"
    verdicts = [
        call("write_verdict", test_id=other, label="API_BUG", confidence="med", reason="same"),
        DONE,
        verdict("API_BUG"),
        DONE,
    ]
    report_call = call(
        "write_bug_report",
        kind="api_bug",
        title="One report",
        test_id=other,
        expected="200",
        actual="500",
        spec_reference="getItem",
    )
    report = run_test_pipeline(
        config(items_api, tmp_path, two_workflows(), [*verdicts, report_call, DONE])
    )
    files = list((tmp_path / "reports" / "bugs").glob("*.md"))
    assert [path.name for path in files] == ["one-report.md"]
    text = files[0].read_text()
    assert "test_item_lifecycle" in text
    assert "test_item_again" in text
    assert {t.verdict.action.path for t in report.results.tests} == {str(files[0])}


def test_a_reports_folder_can_be_chosen(items_api: ItemsApi, tmp_path: Path) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED, reports_dir=tmp_path / "elsewhere"))
    assert (tmp_path / "elsewhere" / "report.md").is_file()
    assert not (tmp_path / "reports").exists()


def test_plan_only_writes_no_reports(items_api: ItemsApi, tmp_path: Path) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED, plan_only=True))
    assert not (tmp_path / "reports").exists()


def test_cli_run_can_write_reports_with_no_model(
    items_api: ItemsApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked_run(items_api, tmp_path)
    args = ["run", "tests/wf", "--base-url", items_api.url, "--report", "out"]
    result = invoke(tmp_path, monkeypatch, args)
    assert result.exit_code == 0, result.output
    text = (tmp_path / "out" / "report.md").read_text()
    assert "1 expected to fail (API_BUG)" in text
    assert "reason for API_BUG" in text


def drift_args(url: str) -> list[str]:
    return ["drift", "openapi.json", "--base-url", url]


def test_cli_drift_needs_saved_workflows(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    (tmp_path / "openapi.json").write_text(json.dumps(spec()))
    scripted()
    result = invoke(tmp_path, monkeypatch, drift_args(items_api.url))
    assert result.exit_code == 2
    assert "run `workflowval test` first" in result.output


def test_cli_drift_is_quiet_and_exits_zero_when_the_api_fits(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    scripted()
    result = invoke(tmp_path, monkeypatch, drift_args(items_api.url))
    assert result.exit_code == 0, result.output
    assert "1 passed" in result.output


def test_cli_drift_reports_a_confirmed_mismatch_and_exits_non_zero(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    run_test_pipeline(config(items_api, tmp_path, PLANNED))
    changed = spec()
    created = changed["paths"]["/items"]["post"]["responses"]["201"]["content"]["application/json"]
    created["schema"]["required"] = ["missing_field"]
    (tmp_path / "openapi.json").write_text(json.dumps(changed))
    group = MismatchGroup("'missing_field' is a required property", "m")
    ruling = call(
        "write_verdict",
        test_id=group.finding_id,
        label="API_BUG",
        confidence="med",
        reason="the API lost a field",
    )
    scripted(reviewer=[ruling, DONE, DONE])
    result = invoke(tmp_path, monkeypatch, drift_args(items_api.url))
    assert result.exit_code == cli.EXIT_FAILURES, result.output
    assert "MISMATCH    API_BUG" in result.output
    assert (tmp_path / "reports" / "report.md").is_file()


def test_cli_drift_stops_cleanly_when_the_api_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted: Callable[..., None]
) -> None:
    folder = tmp_path / "workflows"
    folder.mkdir()
    (folder / "item_lifecycle.json").write_text(json.dumps(lifecycle()))
    (tmp_path / "openapi.json").write_text(json.dumps(spec()))
    scripted()
    result = invoke(tmp_path, monkeypatch, drift_args("http://127.0.0.1:9"))
    assert result.exit_code == cli.EXIT_FAILURES
    assert "api_unreachable" in result.output


def endless(name: str = "read_spec", count: int = 300, **arguments: Any) -> list[Reply]:
    """A model that never stops calling tools."""
    return [call(name, **arguments) for _ in range(count)]


def test_a_stuck_fix_stops_at_its_limit_and_the_test_ends_unresolved(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    steps = lifecycle()["steps"]
    steps[0]["expect"]["status"] = 299
    planned = [call("write_workflow", workflow=lifecycle(steps=steps)), DONE, DONE]
    report = run_test_pipeline(
        config(
            items_api,
            tmp_path,
            [*planned, *endless()],
            [verdict("TEST_BUG"), DONE],
            budget=Budget(max_fix_attempts=3),
        )
    )
    assert report.run.tests == {TEST_ID: "UNRESOLVED"}
    assert report.run.stop_reason is StopReason.COMPLETED
    assert report.results.tests[0].fix_attempts == 1, "a stuck fix gets no second go"
    assert report.results.tasks_stopped == ["planner, item_lifecycle: stopped at 20 calls"]
    # 3 calls to plan, 2 for the one verdict, and 20 for the stuck fix.
    assert report.run.state.steps_used == 3 + 2 + 20


def test_a_stuck_reviewer_leaves_the_test_unresolved_and_the_run_goes_on(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    items_api.broken_get = True
    report = run_test_pipeline(config(items_api, tmp_path, PLANNED, endless()))
    assert report.run.tests == {TEST_ID: "UNRESOLVED"}
    assert report.run.stop_reason is StopReason.COMPLETED
    assert report.results.tasks_stopped == ["reviewer, item_lifecycle: stopped at 20 calls"]
    assert (tmp_path / "reports" / "report.md").is_file()
    assert "stopped at 20 calls" in (tmp_path / "reports" / "report.md").read_text()


def test_a_stuck_planner_keeps_what_it_wrote_and_reports_the_rest(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    written = call("write_workflow", workflow=lifecycle())
    report = run_test_pipeline(config(items_api, tmp_path, [written, *endless()]))
    # This spec has 4 operations, so the limit is 20 + 3 x 4.
    assert report.results.tasks_stopped == ["planner, run: stopped at 32 calls"]
    assert report.run.state.steps_used == 32
    assert [test.workflow_id for test in report.results.tests] == ["item_lifecycle"]
    assert report.results.uncovered_operations == ["login"]
    assert report.results.tests[0].status.value == "passed"


def test_a_planner_that_is_stuck_and_wrote_nothing_stops_the_run(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    report = run_test_pipeline(config(items_api, tmp_path, endless()))
    assert report.run.stop_reason is StopReason.INVALID_OUTPUT


def test_a_stuck_report_leaves_the_failure_labeled_with_no_file(
    items_api: ItemsApi, tmp_path: Path
) -> None:
    items_api.broken_get = True
    report = run_test_pipeline(
        config(items_api, tmp_path, PLANNED, [verdict("API_BUG"), DONE, *endless()])
    )
    assert report.results.tests[0].verdict.label.value == "API_BUG"
    assert report.results.tests[0].verdict.action.kind.value == "none"
    assert report.results.tasks_stopped == ["reviewer, run: stopped at 20 calls"]


def test_cli_shows_a_task_that_hit_its_limit(
    items_api: ItemsApi,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scripted: Callable[..., None],
) -> None:
    items_api.broken_get = True
    (tmp_path / "s.json").write_text(json.dumps(spec()))
    scripted(planner=PLANNED, reviewer=endless())
    result = invoke(tmp_path, monkeypatch, ["test", "s.json", "--base-url", items_api.url])
    assert "LIMIT       reviewer, item_lifecycle: stopped at 20 calls" in result.output


def test_normal_tasks_never_see_the_limit(items_api: ItemsApi, tmp_path: Path) -> None:
    report = run_test_pipeline(config(items_api, tmp_path, PLANNED))
    assert report.results.tasks_stopped == []
