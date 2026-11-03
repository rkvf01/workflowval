"""Response checks against the spec: what's caught, what's quiet, and how findings get reviewed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from workflowval.models import FakeModel, Reply, call
from workflowval.pipeline import TestConfig, run_test_pipeline
from workflowval.schemas import Label, RunResults
from workflowval.spec import ResponseChecker, build_graph, load_raw_spec, load_spec
from workflowval.triage import MismatchGroup, classify, group_mismatches

SEEDED = Path(__file__).parent.parent / "benchmarks" / "seeded"
HEADERS = {"Authorization": "Bearer orders-practice-token"}
NEW_ORDER = {
    "customer": "ann",
    "currency": "USD",
    "items": [{"sku": "a", "quantity": 1, "unit_price_cents": 5}],
}


def seeded_client(*, buggy: bool) -> TestClient:
    module_spec = importlib.util.spec_from_file_location("seeded_app", SEEDED / "app.py")
    assert module_spec is not None
    assert module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return TestClient(module.create_app(buggy=buggy), headers=HEADERS)


@pytest.fixture
def checker() -> ResponseChecker:
    return ResponseChecker(load_raw_spec(str(SEEDED / "openapi.json")))


def check(checker: ResponseChecker, method: str, path: str, response: Any) -> list[str]:
    found = checker.check(
        method, path, response.status_code, response.headers.get("content-type"), response.content
    )
    return [item.message for item in found]


def order_messages(checker: ResponseChecker, *, buggy: bool) -> list[str]:
    response = seeded_client(buggy=buggy).post("/orders", json=NEW_ORDER)
    return check(checker, "POST", "/orders", response)


def test_the_clean_build_raises_nothing_on_any_operation(checker: ResponseChecker) -> None:
    api = seeded_client(buggy=False)
    created = api.post("/orders", json=NEW_ORDER)
    order_id = created.json()["id"]
    item = f"/orders/{order_id}"
    calls = [
        ("POST", "/orders", created),
        ("POST", "/orders", api.post("/orders", json={})),
        ("GET", "/orders", api.get("/orders")),
        ("GET", "/orders", api.get("/orders", params={"page_size": 101})),
        ("GET", "/orders/{order_id}", api.get(item)),
        ("GET", "/orders/{order_id}", api.get("/orders/999999")),
        ("POST", "/orders/{order_id}/pay", api.post(f"{item}/pay", json={"amount_cents": 5})),
        ("POST", "/orders/{order_id}/ship", api.post(f"{item}/ship")),
        ("POST", "/orders/{order_id}/cancel", api.post(f"{item}/cancel")),
        ("POST", "/orders/{order_id}/refund", api.post(f"{item}/refund")),
        ("DELETE", "/orders/{order_id}", api.delete(item)),
    ]
    for method, path, response in calls:
        assert check(checker, method, path, response) == [], (method, path, response.status_code)


def test_a_renamed_field_is_caught_by_name(checker: ResponseChecker) -> None:
    assert any("total_cents" in text for text in order_messages(checker, buggy=True))


def test_a_wrong_date_format_is_caught(checker: ResponseChecker) -> None:
    messages = order_messages(checker, buggy=True)
    assert any("created_at" in text and "date-time" in text for text in messages)


def test_an_undocumented_status_is_caught(checker: ResponseChecker) -> None:
    found = checker.check("POST", "/orders", 418, "application/json", b"{}")
    assert [item.key for item in found] == ["status 418"]


def test_the_host_never_matters() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "https://elsewhere.example/api"}],
        "paths": {"/x": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    assert ResponseChecker(spec).check("GET", "/x", 200, None, b"") == []


def test_the_same_problem_with_another_value_has_the_same_key(checker: ResponseChecker) -> None:
    def key(date: str) -> str:
        order = seeded_client(buggy=False).post("/orders", json=NEW_ORDER).json()
        body = json.dumps({**order, "created_at": date}).encode()
        return checker.check("POST", "/orders", 201, "application/json", body)[0].key

    assert key("11/03/2026") == key("01/02/2027")


def recorded(buggy: bool, method: str = "POST") -> dict[str, Any]:
    response = seeded_client(buggy=buggy).post("/orders", json=NEW_ORDER)
    return {
        "method": method,
        "template": "/orders",
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "raw_body": response.text,
    }


def test_one_problem_seen_by_many_tests_is_grouped_once(checker: ResponseChecker) -> None:
    graph = build_graph(load_spec(str(SEEDED / "openapi.json")))
    outcomes = {"t1": [recorded(True)], "t2": [recorded(True)]}
    groups = group_mismatches(outcomes, checker, graph)
    assert len(groups) == 2, "one for the date and one for the renamed field"
    for group in groups:
        assert group.tests == {"t1", "t2"}
        assert group.operations == {"createOrder"}


def test_a_5xx_body_is_not_checked_for_drift(checker: ResponseChecker) -> None:
    graph = build_graph(load_spec(str(SEEDED / "openapi.json")))
    call_data = {**recorded(True), "status": 500}
    assert group_mismatches({"t": [call_data]}, checker, graph) == []


def test_a_call_with_no_template_is_skipped(checker: ResponseChecker) -> None:
    graph = build_graph(load_spec(str(SEEDED / "openapi.json")))
    assert group_mismatches({"t": [{"method": "GET", "status": 200}]}, checker, graph) == []


@pytest.mark.parametrize(
    ("status", "mismatches", "expected"),
    [
        (500, ["x"], "likely an API_BUG"),
        (503, [], "likely an API_BUG"),
        (200, ["$.a: wrong"], "likely SPEC_DRIFT: $.a: wrong"),
        (404, [], "no automatic signal"),
        (None, [], "no automatic signal"),
    ],
)
def test_classify_gives_hints(status: int | None, mismatches: list[str], expected: str) -> None:
    assert expected in classify(status, mismatches)


def finding_group() -> MismatchGroup:
    return MismatchGroup("k", "m", {"op"}, {"t"})


def test_a_finding_id_is_stable() -> None:
    assert finding_group().finding_id == finding_group().finding_id
    assert finding_group().finding_id.startswith("finding:")


def findings_of(results: RunResults, label: Label) -> int:
    return sum(finding.label is label for finding in results.findings)


def test_results_count_confirmed_findings_but_not_dismissed_ones() -> None:
    results = RunResults.model_validate(
        {
            "stop_reason": "completed",
            "findings": [
                {"id": "a", "message": "m", "label": "SPEC_DRIFT"},
                {"id": "b", "message": "m", "label": "TEST_BUG"},
                {"id": "c", "message": "m"},
            ],
        }
    )
    assert results.label_counts == {"SPEC_DRIFT": 1}
    assert results.summary() == "0 passed, 1 spec mismatch"
    assert findings_of(results, Label.TEST_BUG) == 1


def _config(tmp_path: Path, url: str, reviewer: list[Reply]) -> TestConfig:
    return TestConfig(
        spec_source=str(tmp_path / "openapi.json"),
        base_url=url,
        workdir=tmp_path,
        models={
            "planner": FakeModel([Reply(text="done"), Reply(text="done")]),
            "reviewer": FakeModel(reviewer),
            "writer": FakeModel([]),
        },
    )


@pytest.mark.enable_socket
def test_drift_in_a_passing_test_is_reviewed_once_and_the_run_still_passes(
    tmp_path: Path,
) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:
            body = b'{"wrong_name": 1}' if self.path != "/" else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    schema = {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "operationId": "getX",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": schema}},
                        }
                    },
                }
            }
        },
    }
    (tmp_path / "openapi.json").write_text(json.dumps(spec))
    for name in ("one", "two"):
        workflow = {
            "id": name,
            "source": "user",
            "basis": "documented",
            "rule": "r",
            "steps": [{"op": "getX", "expect": {"status": 200}}],
        }
        folder = tmp_path / "workflows"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(workflow))
    group_id = MismatchGroup("$: 'id' is a required property|200", "m").finding_id
    verdict = call(
        "write_verdict",
        test_id=group_id,
        label="SPEC_DRIFT",
        confidence="high",
        reason="the field was renamed",
    )
    try:
        report = run_test_pipeline(_config(tmp_path, url, [verdict, Reply(text="done")]))
    finally:
        server.shutdown()
    results = report.results
    assert [test.status.value for test in results.tests] == ["passed", "passed"]
    assert not results.has_unexpected_failures
    assert len(results.findings) == 1
    finding = results.findings[0]
    assert finding.label is Label.SPEC_DRIFT
    assert finding.operations == ["getX"]
    assert len(finding.tests) == 2
    assert results.summary() == "2 passed, 1 spec mismatch"
    saved = json.loads((tmp_path / "results.json").read_text())
    assert saved["findings"][0]["reason"] == "the field was renamed"
