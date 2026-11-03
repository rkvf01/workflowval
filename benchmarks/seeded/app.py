"""Seeded-bug orders API: ground truth for the benchmark. Never edit it to change a score.

`SEEDED_BUILD=clean` turns off the 10 planted defects. The spec-docs conflict stays in both builds,
because it lives in the documents, not in this code. `labels.json` lists every defect, and each one
below is marked with its id.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

MAX_PAGE_SIZE = 100
STATUSES = {"pending", "paid", "shipped", "cancelled", "refunded"}
TOKEN_ENV = "ORDERS_TOKEN"  # noqa: S105 - the name of an env var, not a secret
DEFAULT_TOKEN = "orders-practice-token"  # noqa: S105 - a practice value for a local demo app


class ApiError(Exception):
    """An error response in the spec's Error format."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _invalid(message: str, status: int = 422) -> ApiError:
    return ApiError(status, "validation_error", message)


def create_app(*, buggy: bool) -> FastAPI:
    """Build the app, with or without the planted defects."""
    app = FastAPI(title="Orders", openapi_url=None, docs_url=None, redoc_url=None)
    orders: dict[int, dict[str, Any]] = {}
    keys: dict[str, int] = {}
    lock = threading.Lock()
    counter = {"next": 1}

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"code": "validation_error", "message": str(exc)}, status_code=422)

    def require_token(request: Request) -> None:
        token = os.environ.get(TOKEN_ENV, DEFAULT_TOKEN)
        if request.headers.get("authorization") != f"Bearer {token}":
            raise ApiError(401, "unauthorized", "a valid bearer token is required")

    def find(order_id: int) -> dict[str, Any]:
        order = orders.get(order_id)
        if order is None:
            raise ApiError(404, "not_found", f"no order {order_id}")
        return order

    def public(order: dict[str, Any]) -> dict[str, Any]:
        view = {key: value for key, value in order.items() if not key.startswith("_")}
        created: datetime = order["_created"]
        view["created_at"] = created.isoformat().replace("+00:00", "Z")
        if buggy:
            # field_renamed: the spec still says total_cents.
            view["total_amount_cents"] = view.pop("total_cents")
            # date_format: the spec says date-time.
            view["created_at"] = created.strftime("%m/%d/%Y")
        return view

    async def json_object(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError as exc:
            raise _invalid("the body must be JSON") from exc
        if not isinstance(body, dict):
            raise _invalid("the body must be a JSON object")
        return body

    def parsed_items(body: dict[str, Any]) -> list[dict[str, Any]]:
        items = body.get("items")
        if not isinstance(items, list) or not items:
            raise _invalid("items must be a non-empty list")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("sku"), str):
                raise _invalid("every item needs a sku")
            if not _positive_int(item.get("unit_price_cents")):
                raise _invalid("unit_price_cents must be a positive integer")
            if not _positive_int(item.get("quantity")):
                # wrong_validation_status: the spec says 422.
                raise _invalid("quantity must be a positive integer", 400 if buggy else 422)
        return [
            {
                "sku": item["sku"],
                "quantity": item["quantity"],
                "unit_price_cents": item["unit_price_cents"],
            }
            for item in items
        ]

    @app.post("/orders")
    async def create_order(request: Request) -> JSONResponse:
        require_token(request)
        body = await json_object(request)
        customer = body.get("customer")
        if not isinstance(customer, str) or not customer:
            raise _invalid("customer is required")
        currency = body.get("currency")
        # extra_required_field: the spec requires currency, and this build ignores it.
        if buggy:
            currency = "USD"
        elif not isinstance(currency, str) or len(currency) != 3:
            raise _invalid("currency is required, as a 3-letter code")
        items = parsed_items(body)
        key = request.headers.get("idempotency-key")
        with lock:
            # idempotency_ignored: a retry with the same key must return the first order.
            if key and not buggy and key in keys:
                return JSONResponse(public(orders[keys[key]]), status_code=200)
            order_id = counter["next"]
            counter["next"] += 1
            total = sum(item["quantity"] * item["unit_price_cents"] for item in items)
            order = {
                "id": order_id,
                "customer": customer,
                "currency": currency,
                "status": "pending",
                "items": items,
                "total_cents": total,
                "refundable_cents": 0,
                "refunded_cents": 0,
                "_created": datetime.now(UTC),
                "_original_total": total,
                "_partial": False,
            }
            orders[order_id] = order
            if key:
                keys[key] = order_id
        return JSONResponse(public(order), status_code=201)

    @app.get("/orders")
    async def list_orders(
        request: Request,
        page: int = 1,
        page_size: int = 20,
        customer: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        require_token(request)
        if page < 1:
            raise _invalid("page must be at least 1")
        # The spec allows 100 and the docs say 50. This is the planted conflict, in both builds.
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise _invalid(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if status is not None and status not in STATUSES:
            raise _invalid(f"status must be one of {sorted(STATUSES)}")
        with lock:
            matches = [
                order
                for order in sorted(orders.values(), key=lambda order: order["id"])
                if (customer is None or order["customer"] == customer)
                and (status is None or order["status"] == status)
            ]
        if buggy and (customer is not None or status is not None) and not matches:
            # empty_filter_500: a filter that matches nothing must return an empty page.
            raise ApiError(500, "internal_error", "unexpected error")
        start = (page - 1) * page_size
        if buggy and page > 1:
            start += 1  # pagination_off_by_one: each later page skips one order.
        chosen = matches[start : start + page_size]
        return {
            "items": [public(order) for order in chosen],
            "page": page,
            "page_size": page_size,
            "total": len(matches),
        }

    @app.get("/orders/{order_id}")
    async def get_order(request: Request, order_id: int) -> dict[str, Any]:
        require_token(request)
        return public(find(order_id))

    @app.delete("/orders/{order_id}")
    async def delete_order(request: Request, order_id: int) -> Response:
        require_token(request)
        with lock:
            find(order_id)
            del orders[order_id]
        return Response(status_code=204)

    @app.post("/orders/{order_id}/pay")
    async def pay_order(request: Request, order_id: int) -> dict[str, Any]:
        require_token(request)
        body = await json_object(request)
        with lock:
            order = find(order_id)
            if order["status"] != "pending":
                raise ApiError(409, "invalid_state", "only a pending order can be paid")
            if body.get("amount_cents") != order["total_cents"]:
                raise _invalid("amount_cents must equal the order total")
            order["status"] = "paid"
            return public(order)

    @app.post("/orders/{order_id}/ship")
    async def ship_order(request: Request, order_id: int) -> dict[str, Any]:
        if not buggy:  # missing_auth_check: this write route must require the token.
            require_token(request)
        with lock:
            order = find(order_id)
            if order["status"] != "paid":
                raise ApiError(409, "invalid_state", "only a paid order can be shipped")
            order["status"] = "shipped"
            return public(order)

    def cancel_part(order: dict[str, Any], lines: list[Any]) -> None:
        cancelled = 0
        for line in lines:
            if not isinstance(line, dict) or not _positive_int(line.get("quantity")):
                raise _invalid("every cancelled item needs a sku and a positive quantity")
            match = next((i for i in order["items"] if i["sku"] == line.get("sku")), None)
            if match is None or match["quantity"] < line["quantity"]:
                raise _invalid("cancelled items must be on the order")
            match["quantity"] -= line["quantity"]
            cancelled += line["quantity"] * match["unit_price_cents"]
        order["items"] = [item for item in order["items"] if item["quantity"] > 0]
        order["total_cents"] -= cancelled
        order["_partial"] = True
        if order["status"] == "paid":
            order["refundable_cents"] += cancelled
        if not order["items"]:
            order["status"] = "cancelled"

    @app.post("/orders/{order_id}/cancel")
    async def cancel_order(request: Request, order_id: int) -> dict[str, Any]:
        require_token(request)
        lines = None
        if await request.body():
            lines = (await json_object(request)).get("items")
        with lock:
            order = find(order_id)
            status = order["status"]
            # cancel_after_shipped: the docs and spec say a shipped order can't be cancelled.
            blocked = {"cancelled", "refunded"} if buggy else {"shipped", "cancelled", "refunded"}
            if status in blocked:
                raise ApiError(409, "invalid_state", f"a {status} order can't be cancelled")
            if lines:
                cancel_part(order, lines)
            else:
                if status == "paid":
                    order["refundable_cents"] += order["total_cents"]
                order["status"] = "cancelled"
            return public(order)

    @app.post("/orders/{order_id}/refund")
    async def refund_order(request: Request, order_id: int) -> dict[str, Any]:
        require_token(request)
        with lock:
            order = find(order_id)
            amount = order["refundable_cents"]
            if amount <= 0:
                raise ApiError(409, "invalid_state", "this order has nothing to refund")
            if buggy and order["_partial"]:
                # refund_after_partial_cancel: only the cancelled items' value is due.
                amount = order["_original_total"]
            order["refunded_cents"] += amount
            order["refundable_cents"] = 0
            if order["status"] == "cancelled":
                order["status"] = "refunded"
            return public(order)

    return app


app = create_app(buggy=os.environ.get("SEEDED_BUILD", "buggy") != "clean")
