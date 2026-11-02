from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from workflowval.schemas import (
    Action,
    ActionKind,
    Answer,
    AuthConfig,
    Basis,
    Confidence,
    Decision,
    Label,
    Labels,
    LoginAuth,
    MissingJourney,
    Review,
    RunResults,
    RunState,
    TestResult,
    TestStatus,
    Verdict,
    Workflow,
    WorkflowFix,
    load_json,
    save_json,
)

DOC_EXAMPLE: dict[str, Any] = {
    "id": "order_cancel_refund",
    "source": "reviewer",
    "basis": "documented",
    "locked": False,
    "rule": "Cancelling a paid order refunds the full amount (docs/orders.md)",
    "steps": [
        {
            "op": "createOrder",
            "save": {"order_id": "$.id", "paid_amount": "$.total"},
            "expect": {"status": 201},
        },
        {"op": "payOrder", "params": {"id": "{order_id}"}, "expect": {"status": 200}},
        {"op": "cancelOrder", "params": {"id": "{order_id}"}, "expect": {"status": 200}},
        {
            "op": "getOrder",
            "params": {"id": "{order_id}"},
            "expect": {
                "status": 200,
                "body": {"$.state": "cancelled", "$.refund.amount": "{paid_amount}"},
            },
        },
    ],
    "cleanup": [{"op": "deleteOrder", "params": {"id": "{order_id}"}}],
}


def workflow(**changes: Any) -> dict[str, Any]:
    return {**json.loads(json.dumps(DOC_EXAMPLE)), **changes}


def test_doc_example_round_trips_through_a_file(tmp_path: Path) -> None:
    original = Workflow.model_validate(DOC_EXAMPLE)
    path = tmp_path / "nested" / "order_cancel_refund.json"
    save_json(original, path)
    assert load_json(Workflow, path) == original
    written = json.loads(path.read_text())
    assert written["steps"][3]["expect"]["body"]["$.refund.amount"] == "{paid_amount}"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"id": "../evil"}, "pattern"),
        ({"rule": ""}, "rule"),
        ({"rule": "   "}, "rule"),
        ({"id": "Mixed_Case"}, "pattern"),
        ({"source": "robot"}, "source"),
        ({"basis": "vibes"}, "basis"),
        ({"steps": []}, "steps"),
        ({"surprise": 1}, "surprise"),
        ({"locked": True}, "only workflows with source 'user'"),
        ({"cleanup": [{"op": "deleteOrder", "params": {"id": "{never_saved}"}}]}, "never_saved"),
        (
            {
                "steps": [
                    {"op": "getOrder", "params": {"id": "{order_id}"}, "expect": {"status": 200}}
                ]
            },
            "order_id",
        ),
        ({"steps": [{"op": "x", "expect": {"status": 9}}]}, "status"),
        ({"steps": [{"op": "x", "expect": {"status": 200, "body": {"state": "a"}}}]}, "JSONPath"),
        ({"steps": [{"op": "x", "save": {"a": "id"}, "expect": {"status": 200}}]}, "JSONPath"),
    ],
)
def test_invalid_workflows_are_rejected(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Workflow.model_validate(workflow(**changes))


def test_every_workflow_must_state_its_rule() -> None:
    data = workflow()
    del data["rule"]
    with pytest.raises(ValidationError, match="rule"):
        Workflow.model_validate(data)


def test_user_workflow_can_be_locked() -> None:
    assert Workflow.model_validate(workflow(source="user", locked=True)).locked


def test_step_can_save_and_check_in_the_same_response() -> None:
    step = {"op": "x", "save": {"a": "$.id"}, "expect": {"status": 200, "body": {"$.id": "{a}"}}}
    Workflow.model_validate(workflow(steps=[step], cleanup=[]))


def test_review_tracks_unanswered_items() -> None:
    review = Review(
        round=1,
        fixes=[WorkflowFix(id="f1", workflow_id="a", reason="loose assertion")],
        missing_journeys=[
            MissingJourney(
                id="j1",
                user="customer",
                steps=["pay", "cancel", "check refund"],
                rule="full refund",
                basis=Basis.ASSUMED,
                answer=Answer(decision=Decision.REJECTED, reason="duplicate of a"),
            )
        ],
    )
    assert review.unanswered() == ["f1"]


def test_review_ids_must_be_unique() -> None:
    fix = WorkflowFix(id="same", workflow_id="a", reason="r")
    with pytest.raises(ValidationError, match="unique"):
        Review(round=1, fixes=[fix, fix])


def test_rejection_needs_a_reason_and_acceptance_needs_a_workflow() -> None:
    with pytest.raises(ValidationError, match="reason"):
        Answer(decision=Decision.REJECTED)
    with pytest.raises(ValidationError, match="workflow_id"):
        Answer(decision=Decision.ACCEPTED)


def result(**changes: Any) -> dict[str, Any]:
    base = {
        "id": "tests/wf/test_a.py::test_a",
        "workflow_id": "a",
        "basis": "documented",
        "status": "failed",
        "duration_s": 0.5,
    }
    return {**base, **changes}


def test_results_keep_at_most_three_exchanges() -> None:
    exchange = {"method": "GET", "url": "http://x/"}
    TestResult.model_validate(result(exchanges=[exchange] * 3))
    with pytest.raises(ValidationError, match="at most 3"):
        TestResult.model_validate(result(exchanges=[exchange] * 4))


def test_assumed_failure_is_capped_at_medium_confidence() -> None:
    high = Verdict(label=Label.API_BUG, confidence=Confidence.HIGH, reason="r")
    med = Verdict(label=Label.API_BUG, confidence=Confidence.MED, reason="r")
    TestResult.model_validate(result(verdict=high))
    TestResult.model_validate(result(basis="assumed", verdict=med))
    with pytest.raises(ValidationError, match="capped"):
        TestResult.model_validate(result(basis="assumed", verdict=high))


def test_results_count_labels_and_round_trip(tmp_path: Path) -> None:
    verdict = Verdict(
        label=Label.CONFLICT,
        confidence=Confidence.HIGH,
        reason="page size",
        action=Action(kind=ActionKind.BUG_REPORT, path="out/conflict.md"),
    )
    results = RunResults(
        stop_reason="completed",
        tests=[
            TestResult.model_validate(result(verdict=verdict)),
            TestResult.model_validate(result(id="t2", status=TestStatus.PASSED)),
        ],
    )
    assert results.label_counts == {"CONFLICT": 1}
    path = tmp_path / "results.json"
    save_json(results, path)
    assert json.loads(path.read_text())["label_counts"] == {"CONFLICT": 1}


def test_labels_require_a_triage_label_and_unique_ids() -> None:
    bug = {"id": "b", "description": "d", "expected_label": "API_BUG", "operations": ["op"]}
    assert Labels.model_validate({"bugs": [bug]}).bugs[0].id == "b"
    with pytest.raises(ValidationError, match="unique"):
        Labels.model_validate({"bugs": [bug, bug]})
    with pytest.raises(ValidationError, match="expected_label"):
        Labels.model_validate({"bugs": [{**bug, "expected_label": "TEST_BUG"}]})


@pytest.mark.parametrize(
    "scheme",
    [
        {"type": "api_key", "name": "X-Key", "location": "header", "value_env": "MY_KEY"},
        {"type": "bearer", "token_env": "MY_TOKEN"},
        {"type": "basic", "username_env": "U", "password_env": "P"},
        {
            "type": "login",
            "op": "createToken",
            "body_env": {"username": "BOOKER_USER", "password": "BOOKER_PASS"},
            "token_path": "$.token",
            "token_header": "Cookie",
            "token_prefix": "token=",
        },
    ],
)
def test_auth_config_accepts_every_scheme(scheme: dict[str, Any]) -> None:
    assert AuthConfig.model_validate({"scheme": scheme}).scheme.type == scheme["type"]


@pytest.mark.parametrize(
    "scheme",
    [
        {"type": "bearer", "token": "literal-secret"},
        {"type": "bearer", "token_env": "not a var name!"},
        {"type": "api_key", "name": "k", "location": "cookie", "value_env": "K"},
        {"type": "oauth"},
    ],
)
def test_auth_config_refuses_literal_secrets_and_bad_shapes(scheme: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AuthConfig.model_validate({"scheme": scheme})


def test_login_defaults_to_bearer_and_refresh() -> None:
    login = LoginAuth(type="login", op="o", body_env={}, token_path="$.t")
    assert (login.token_header, login.token_prefix, login.refresh_on_401) == (
        "Authorization",
        "Bearer ",
        True,
    )


def test_state_round_trips_and_rejects_unknown_versions(tmp_path: Path) -> None:
    state = RunState(spec_hash="abc", fix_attempts={"t": 2}, closed_tests={"u": "API_BUG"})
    path = tmp_path / "state.json"
    save_json(state, path)
    assert load_json(RunState, path) == state
    with pytest.raises(ValidationError, match="format_version"):
        RunState.model_validate({"format_version": 2})


def test_failed_atomic_write_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    save_json(RunState(spec_hash="old"), path)

    def boom(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_json(RunState(spec_hash="new"), path)
    assert load_json(RunState, path).spec_hash == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
