"""The seeded app: the clean build keeps its promises, and each planted defect is really there.

These run in-process, so the benchmark's ground truth is checked with no network and no model.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from workflowval.schemas import Labels
from workflowval.spec import load_spec

SEEDED = Path(__file__).parent.parent / "benchmarks" / "seeded"
HEADERS = {"Authorization": "Bearer orders-practice-token"}
Check = Callable[[TestClient], bool]


def client(*, buggy: bool) -> TestClient:
    module_spec = importlib.util.spec_from_file_location("seeded_app", SEEDED / "app.py")
    assert module_spec is not None
    assert module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return TestClient(module.create_app(buggy=buggy), headers=HEADERS)


def new_order(api: TestClient, **changes: Any) -> dict[str, Any]:
    body = {
        "customer": "ann",
        "currency": "USD",
        "items": [
            {"sku": "a", "quantity": 2, "unit_price_cents": 100},
            {"sku": "b", "quantity": 1, "unit_price_cents": 500},
        ],
        **changes,
    }
    response = api.post("/orders", json=body)
    assert response.status_code == 201, response.text
    order: dict[str, Any] = response.json()
    return order


def _paid(api: TestClient) -> int:
    order_id = new_order(api)["id"]
    assert api.post(f"/orders/{order_id}/pay", json={"amount_cents": 700}).status_code == 200
    return int(order_id)


def _bad_quantity_is_422(api: TestClient) -> bool:
    body = {
        "customer": "ann",
        "currency": "USD",
        "items": [{"sku": "a", "quantity": 0, "unit_price_cents": 100}],
    }
    return api.post("/orders", json=body).status_code == 422


def _ship_needs_token(api: TestClient) -> bool:
    order_id = _paid(api)
    response = api.post(f"/orders/{order_id}/ship", headers={"Authorization": ""})
    return response.status_code == 401


def _pages_show_every_order(api: TestClient) -> bool:
    ids = {new_order(api, customer="pager")["id"] for _ in range(4)}
    seen: set[int] = set()
    for page in (1, 2):
        result = api.get("/orders", params={"customer": "pager", "page": page, "page_size": 2})
        seen |= {item["id"] for item in result.json()["items"]}
    return seen == ids


def _shipped_cannot_be_cancelled(api: TestClient) -> bool:
    order_id = _paid(api)
    api.post(f"/orders/{order_id}/ship")
    return api.post(f"/orders/{order_id}/cancel").status_code == 409


def _partial_refund_is_cancelled_value(api: TestClient) -> bool:
    order_id = _paid(api)
    cancel = {"items": [{"sku": "a", "quantity": 1}]}
    assert api.post(f"/orders/{order_id}/cancel", json=cancel).status_code == 200
    return api.post(f"/orders/{order_id}/refund").json()["refunded_cents"] == 100


def _total_field_matches_spec(api: TestClient) -> bool:
    return "total_cents" in new_order(api)


def _currency_is_enforced(api: TestClient) -> bool:
    body = {"customer": "ann", "items": [{"sku": "a", "quantity": 1, "unit_price_cents": 100}]}
    return api.post("/orders", json=body).status_code == 422


def _date_is_iso(api: TestClient) -> bool:
    return "T" in new_order(api)["created_at"]


def _retry_returns_first_order(api: TestClient) -> bool:
    body = {
        "customer": "ann",
        "currency": "USD",
        "items": [{"sku": "a", "quantity": 1, "unit_price_cents": 100}],
    }
    key = {"Idempotency-Key": "abc"}
    first = api.post("/orders", json=body, headers=key)
    second = api.post("/orders", json=body, headers=key)
    return second.status_code == 200 and first.json()["id"] == second.json()["id"]


def _empty_filter_is_empty_page(api: TestClient) -> bool:
    response = api.get("/orders", params={"customer": "nobody"})
    return response.status_code == 200 and response.json()["items"] == []


DEFECTS: dict[str, Check] = {
    "wrong_validation_status": _bad_quantity_is_422,
    "missing_auth_check": _ship_needs_token,
    "pagination_off_by_one": _pages_show_every_order,
    "cancel_after_shipped": _shipped_cannot_be_cancelled,
    "refund_after_partial_cancel": _partial_refund_is_cancelled_value,
    "field_renamed": _total_field_matches_spec,
    "extra_required_field": _currency_is_enforced,
    "date_format": _date_is_iso,
    "idempotency_ignored": _retry_returns_first_order,
    "empty_filter_500": _empty_filter_is_empty_page,
}


def labels() -> Labels:
    return Labels.model_validate_json((SEEDED / "labels.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("bug_id", sorted(DEFECTS))
def test_the_rule_holds_on_the_clean_build(bug_id: str) -> None:
    assert DEFECTS[bug_id](client(buggy=False))


@pytest.mark.parametrize("bug_id", sorted(DEFECTS))
def test_the_defect_is_real_on_the_buggy_build(bug_id: str) -> None:
    assert not DEFECTS[bug_id](client(buggy=True))


def test_labels_cover_the_10_defects_and_the_conflict() -> None:
    bugs = labels().bugs
    assert {bug.id for bug in bugs} == {*DEFECTS, "page_size_conflict"}
    counts = [bug.expected_label for bug in bugs]
    assert counts.count("API_BUG") == 7
    assert counts.count("SPEC_DRIFT") == 3
    assert counts.count("CONFLICT") == 1


def test_labels_name_real_operations() -> None:
    spec = load_spec(str(SEEDED / "openapi.json"))
    real = {
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    for bug in labels().bugs:
        assert set(bug.operations) <= real, bug.id


@pytest.mark.parametrize("buggy", [False, True])
def test_the_conflict_is_in_both_builds(buggy: bool) -> None:
    api = client(buggy=buggy)
    assert api.get("/orders", params={"page_size": 100}).status_code == 200
    assert api.get("/orders", params={"page_size": 101}).status_code == 422
    spec = json.loads((SEEDED / "openapi.json").read_text(encoding="utf-8"))
    parameters = spec["paths"]["/orders"]["get"]["parameters"]
    size = next(parameter for parameter in parameters if parameter["name"] == "page_size")
    assert size["schema"]["maximum"] == 100
    assert "at most 50" in (SEEDED / "docs" / "orders.md").read_text(encoding="utf-8")


def test_every_request_needs_a_token_on_the_clean_build() -> None:
    api = client(buggy=False)
    order_id = new_order(api)["id"]
    calls = [
        ("POST", "/orders"),
        ("GET", "/orders"),
        ("GET", f"/orders/{order_id}"),
        ("DELETE", f"/orders/{order_id}"),
        ("POST", f"/orders/{order_id}/pay"),
        ("POST", f"/orders/{order_id}/ship"),
        ("POST", f"/orders/{order_id}/cancel"),
        ("POST", f"/orders/{order_id}/refund"),
    ]
    for method, path in calls:
        response = api.request(method, path, headers={"Authorization": ""})
        assert response.status_code == 401, (method, path)


def test_the_docs_folder_holds_no_answers() -> None:
    files = [path for path in (SEEDED / "docs").rglob("*") if path.is_file()]
    assert [path.name for path in files] == ["orders.md"]
    text = files[0].read_text(encoding="utf-8").lower()
    for word in ("bug", "defect", "seeded", "planted", "conflict"):
        assert word not in text


def test_the_spec_holds_no_answers() -> None:
    text = (SEEDED / "openapi.json").read_text(encoding="utf-8").lower()
    for word in ("bug", "defect", "seeded", "planted", "total_amount_cents"):
        assert word not in text


def test_the_auth_config_gives_nothing_away() -> None:
    assert "seeded" not in (SEEDED / "auth.json").read_text(encoding="utf-8").lower()
