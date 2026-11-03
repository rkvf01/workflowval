"""The scorer: a score must mean what it says, on results written by hand."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent / "benchmarks"
_spec = importlib.util.spec_from_file_location("run_benchmark", ROOT / "run_benchmark.py")
assert _spec is not None
assert _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules["run_benchmark"] = bench
_spec.loader.exec_module(bench)
LABELS = json.loads((ROOT / "seeded" / "labels.json").read_text())


def workflow(name: str, *steps: dict[str, Any], rule: str = "a rule") -> dict[str, Any]:
    return {"id": name, "rule": rule, "steps": list(steps)}


def step(op: str, **extra: Any) -> dict[str, Any]:
    return {"op": op, "expect": {"status": 200}, **extra}


def failing(name: str, label: str, reason: str, status: str = "xfailed") -> dict[str, Any]:
    return {
        "workflow_id": name,
        "status": status,
        "verdict": {"label": label, "reason": reason, "action": {"path": f"reports/{name}.md"}},
    }


def results(*tests: dict[str, Any], findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"tests": list(tests), "findings": findings or [], "cost_usd": 0.1}


def bug(scored: dict[str, Any], bug_id: str) -> dict[str, Any]:
    return next(item for item in scored["bugs"] if item["id"] == bug_id)


def test_every_labeled_bug_has_a_rule() -> None:
    ids = {item["id"] for item in LABELS["bugs"]}
    assert ids == set(bench.EVIDENCE) == set(bench.COVERS)


def test_a_failure_with_the_right_evidence_and_label_scores() -> None:
    flow = workflow("q", step("createOrder", body={"items": [{"quantity": 0}]}))
    scored = bench.score_buggy(
        results(failing("q", "API_BUG", "quantity 0 gave 400")), [flow], LABELS
    )
    found = bug(scored, "wrong_validation_status")
    assert found["detected"]
    assert found["correct"]
    assert found["covered"]
    assert scored["correct"] == 1


def test_the_wrong_label_is_detected_but_not_correct() -> None:
    flow = workflow("q", step("createOrder"))
    scored = bench.score_buggy(results(failing("q", "SPEC_DRIFT", "quantity")), [flow], LABELS)
    found = bug(scored, "wrong_validation_status")
    assert found["detected"]
    assert not found["correct"]


def test_a_failure_on_the_operation_for_another_reason_does_not_count() -> None:
    flow = workflow("q", step("createOrder"))
    scored = bench.score_buggy(results(failing("q", "API_BUG", "the sku was odd")), [flow], LABELS)
    assert not bug(scored, "wrong_validation_status")["detected"]


def test_the_right_words_on_another_operation_do_not_count() -> None:
    flow = workflow("q", step("getOrder"))
    scored = bench.score_buggy(results(failing("q", "API_BUG", "quantity")), [flow], LABELS)
    assert not bug(scored, "wrong_validation_status")["detected"]


def test_a_dismissed_failure_does_not_count() -> None:
    flow = workflow("q", step("createOrder"))
    scored = bench.score_buggy(results(failing("q", "TEST_BUG", "quantity")), [flow], LABELS)
    assert not bug(scored, "wrong_validation_status")["detected"]


def test_a_tie_between_labels_is_not_correct() -> None:
    flow = workflow("q", step("createOrder"))
    both = results(failing("q", "API_BUG", "quantity"), failing("q", "SPEC_DRIFT", "quantity"))
    assert not bug(bench.score_buggy(both, [flow], LABELS), "wrong_validation_status")["correct"]


def test_a_finding_counts_as_detection() -> None:
    finding = {
        "message": "$.total_cents is missing",
        "operations": ["getOrder"],
        "label": "SPEC_DRIFT",
        "reason": "renamed",
    }
    scored = bench.score_buggy(results(findings=[finding]), [], LABELS)
    assert bug(scored, "field_renamed")["correct"]


def test_an_unreviewed_finding_does_not_count() -> None:
    finding = {"message": "$.total_cents is missing", "operations": ["getOrder"]}
    assert not bug(bench.score_buggy(results(findings=[finding]), [], LABELS), "field_renamed")[
        "detected"
    ]


@pytest.mark.parametrize(
    ("bug_id", "flow"),
    [
        ("missing_auth_check", [step("shipOrder", auth=False)]),
        ("pagination_off_by_one", [step("listOrders", params={"page": 2})]),
        ("cancel_after_shipped", [step("shipOrder"), step("cancelOrder")]),
        (
            "refund_after_partial_cancel",
            [step("cancelOrder", body={"items": [{"sku": "a"}]}), step("refundOrder")],
        ),
        ("field_renamed", [step("getOrder")]),
        ("date_format", [step("createOrder")]),
        ("extra_required_field", [step("createOrder", body={"customer": "a"})]),
        (
            "idempotency_ignored",
            [
                step("createOrder", params={"Idempotency-Key": "k"}),
                step("createOrder", params={"Idempotency-Key": "k"}),
            ],
        ),
        ("empty_filter_500", [step("listOrders", params={"customer": "x"})]),
        ("page_size_conflict", [step("listOrders", params={"page_size": 51})]),
    ],
)
def test_coverage_recognizes_a_workflow_that_could_catch_the_bug(
    bug_id: str, flow: list[dict[str, Any]]
) -> None:
    scored = bench.score_buggy(results(), [workflow("w", *flow)], LABELS)
    assert bug(scored, bug_id)["covered"]


@pytest.mark.parametrize(
    ("bug_id", "flow"),
    [
        ("wrong_validation_status", [step("createOrder", body={"items": [{"quantity": 1}]})]),
        ("missing_auth_check", [step("shipOrder")]),
        ("pagination_off_by_one", [step("listOrders", params={"page": 1})]),
        ("cancel_after_shipped", [step("cancelOrder"), step("shipOrder")]),
        ("refund_after_partial_cancel", [step("cancelOrder"), step("refundOrder")]),
        ("extra_required_field", [step("createOrder", body={"currency": "USD"})]),
        ("idempotency_ignored", [step("createOrder", params={"Idempotency-Key": "k"})]),
        ("empty_filter_500", [step("listOrders")]),
        ("page_size_conflict", [step("listOrders", params={"page_size": 50})]),
    ],
)
def test_coverage_refuses_a_workflow_that_could_not(
    bug_id: str, flow: list[dict[str, Any]]
) -> None:
    scored = bench.score_buggy(results(), [workflow("w", *flow)], LABELS)
    assert not bug(scored, bug_id)["covered"]


def test_the_conflict_is_scored_apart_from_the_10_defects() -> None:
    flow = workflow("p", step("listOrders"))
    run = results(failing("p", "CONFLICT", "page_size 51 is allowed, docs say 50"))
    scored = bench.score_buggy(run, [flow], LABELS)
    assert scored["defects"] == 10
    assert scored["detected"] == 0
    assert scored["conflict_reports"] == 1
    assert scored["conflict_api_bugs"] == 0


def test_two_tests_sharing_a_conflict_report_count_once() -> None:
    flow = workflow("p", step("listOrders"))
    first = failing("p", "CONFLICT", "page_size")
    second = failing("p", "CONFLICT", "page_size")
    assert bench.score_buggy(results(first, second), [flow], LABELS)["conflict_reports"] == 1


def test_clean_build_counts_false_bugs_and_green_tests() -> None:
    run = results(
        {"workflow_id": "a", "status": "passed"},
        failing("b", "API_BUG", "x"),
        failing("c", "TEST_BUG", "y", status="failed"),
    )
    scored = bench.score_clean(run)
    assert scored["false_api_bugs"] == 1
    assert (scored["green"], scored["tests"]) == (1, 3)


@pytest.mark.parametrize(
    ("costs", "limit", "allowed"),
    [
        ([], 2.0, True),
        ([0.5], 2.0, True),
        ([0.9, 0.9], 2.0, False),
        ([1.0, 1.0], 2.0, False),
        ([2.5], 2.0, False),
    ],
)
def test_no_run_starts_if_it_could_pass_the_limit(
    costs: list[float], limit: float, allowed: bool
) -> None:
    assert bench.next_run_is_allowed(costs, limit) is allowed


def test_the_table_shows_median_range_and_target() -> None:
    buggy = [
        {
            "detected": d,
            "correct": d,
            "covered": 9,
            "conflict_reports": 1,
            "conflict_api_bugs": 0,
            "cost_usd": 0.1,
        }
        for d in (6, 7, 9)
    ]
    clean = [{"false_api_bugs": 0, "green": 9, "tests": 10, "cost_usd": 0.05}] * 3
    table = bench.render_table(buggy, clean)
    assert "| Seeded bugs detected, of 10 | 7 (6 to 9) | 8 or more |" in table
    assert "| Cost per round, USD | 0.15 (0.15 to 0.15) | reported only |" in table


def test_a_benchmark_folder_is_kept_out_of_git() -> None:
    assert ".benchmark/" in (ROOT.parent / ".gitignore").read_text()
