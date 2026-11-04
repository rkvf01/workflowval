"""Scores workflowval on the seeded app. The rules below were committed before any scored run.

Usage: WORKFLOWVAL_MODEL=provider/name uv run python benchmarks/run_benchmark.py --rounds 3

A round is one run on the buggy build and one on the clean build. Spending is kept in
`.benchmark/spend.jsonl` across calls, and no run starts if it could pass `--limit`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parent
SEEDED = ROOT / "seeded"
LEDGER = ROOT.parent / ".benchmark" / "spend.jsonl"
PORT = 18090
TOKEN = "orders-practice-token"  # noqa: S105 - the seeded app's practice value
MAX_STEPS = 200
ORDER_OPERATIONS = frozenset(
    {"createOrder", "getOrder", "listOrders", "payOrder", "shipOrder", "cancelOrder", "refundOrder"}
)

# Evidence a result must mention to count as finding that bug, so that a failure on the same
# operation for another reason doesn't count.
EVIDENCE = {
    "wrong_validation_status": r"quantity|\b400\b",
    "missing_auth_check": r"auth|token|credential|\b401\b",
    "pagination_off_by_one": r"pagina|offset|skip|off.by.one|page [2-9]|second page|later page",
    "cancel_after_shipped": r"ship",
    "refund_after_partial_cancel": r"partial|refund",
    "field_renamed": r"total_cents|total_amount",
    "extra_required_field": r"currency",
    "date_format": r"created_at|date-time",
    "idempotency_ignored": r"idempot|retry|duplicate",
    "empty_filter_500": r"filter|empty|\b500\b",
    # A request above the docs' 50: page_size=2 on a paging test says nothing about this rule.
    "page_size_conflict": (
        r"page.?size\D{0,40}(5[1-9]|[6-9]\d|100)\b"
        r"|\b50\b.{0,80}page.?size|page.?size.{0,80}\b50\b"
    ),
}
NOT_A_DETECTION = frozenset({"TEST_BUG"})


def _last_status(exchanges: list[dict[str, Any]]) -> int | None:
    return exchanges[-1].get("status") if exchanges else None


def _creates(exchanges: list[dict[str, Any]]) -> int:
    return sum(
        e.get("method") == "POST" and str(e.get("url", "")).endswith("/orders") for e in exchanges
    )


# For bugs that words alone can't prove: what the recorded traffic must show. A test that stopped
# early for another reason never reached the bug, even if its reason mentions the same words.
TRAFFIC: dict[str, Callable[[list[dict[str, Any]]], bool]] = {
    "wrong_validation_status": lambda e: _last_status(e) == 400,
    "pagination_off_by_one": lambda e: bool(e) and "page=" in str(e[-1].get("url", "")),
    "idempotency_ignored": lambda e: _creates(e) >= 2,
    "empty_filter_500": lambda e: (_last_status(e) or 0) >= 500,
    "refund_after_partial_cancel": lambda e: any(
        str(item.get("url", "")).endswith("/refund") for item in e
    ),
    "cancel_after_shipped": lambda e: any(
        str(item.get("url", "")).endswith("/cancel") for item in e
    ),
}


def _ops(workflow: dict[str, Any]) -> list[str]:
    return [step["op"] for step in workflow["steps"]]


def _steps(workflow: dict[str, Any], op: str) -> list[dict[str, Any]]:
    return [step for step in workflow["steps"] if step["op"] == op]


def _follows(workflow: dict[str, Any], first: str, then: str) -> bool:
    ops = _ops(workflow)
    return first in ops and then in ops[ops.index(first) + 1 :]


def _number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bad_quantity(workflow: dict[str, Any]) -> bool:
    for step in _steps(workflow, "createOrder"):
        items = (step.get("body") or {}).get("items") or []
        for item in items if isinstance(items, list) else []:
            quantity = item.get("quantity") if isinstance(item, dict) else None
            if isinstance(quantity, int) and quantity <= 0:
                return True
    return False


def _partial_then_refund(workflow: dict[str, Any]) -> bool:
    has_partial = any(
        (step.get("body") or {}).get("items") for step in _steps(workflow, "cancelOrder")
    )
    return has_partial and _follows(workflow, "cancelOrder", "refundOrder")


def _any_order_call(workflow: dict[str, Any]) -> bool:
    return bool(set(_ops(workflow)) & ORDER_OPERATIONS)


def _param_above(workflow: dict[str, Any], name: str, limit: int) -> bool:
    for step in _steps(workflow, "listOrders"):
        value = _number((step.get("params") or {}).get(name))
        if value is not None and value > limit:
            return True
    return False


def _idempotent_retry(workflow: dict[str, Any]) -> bool:
    keyed = [
        step
        for step in _steps(workflow, "createOrder")
        if "Idempotency-Key" in step.get("params", {})
    ]
    return len(keyed) >= 2


# Could this workflow catch the bug? Response checks see every response, so any call to an operation
# returning an order can catch the two response-shape bugs.
COVERS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "wrong_validation_status": _bad_quantity,
    "missing_auth_check": lambda w: any(s.get("auth") is False for s in _steps(w, "shipOrder")),
    "pagination_off_by_one": lambda w: _param_above(w, "page", 1),
    "cancel_after_shipped": lambda w: _follows(w, "shipOrder", "cancelOrder"),
    "refund_after_partial_cancel": _partial_then_refund,
    "field_renamed": _any_order_call,
    "extra_required_field": lambda w: any(
        "currency" not in (s.get("body") or {}) for s in _steps(w, "createOrder")
    ),
    "date_format": _any_order_call,
    "idempotency_ignored": _idempotent_retry,
    "empty_filter_500": lambda w: any(
        {"customer", "status"} & set(s.get("params", {})) for s in _steps(w, "listOrders")
    ),
    "page_size_conflict": lambda w: _param_above(w, "page_size", 50),
}


@dataclass(frozen=True)
class Item:
    """One labeled thing a run reported: a failing test or a reviewed finding."""

    label: str
    operations: frozenset[str]
    text: str
    report: str | None
    exchanges: tuple[dict[str, Any], ...] = ()


def _items(results: dict[str, Any], workflows: Iterable[dict[str, Any]]) -> list[Item]:
    by_id = {workflow["id"]: workflow for workflow in workflows}
    items = []
    for test in results["tests"]:
        verdict = test.get("verdict")
        if verdict is None:
            continue
        workflow = by_id.get(test["workflow_id"], {"steps": [], "rule": ""})
        # Only what the verdict says counts. A workflow's rule says what it set out to check, and a
        # failing assertion can quote a whole response, so neither shows this bug was found.
        text = verdict["reason"]
        report = (verdict.get("action") or {}).get("path")
        exchanges = tuple(test.get("exchanges") or ())
        items.append(Item(verdict["label"], frozenset(_ops(workflow)), text, report, exchanges))
    for finding in results.get("findings", []):
        if finding.get("label"):
            text = f"{finding['message']} {finding.get('reason') or ''}"
            report = (finding.get("action") or {}).get("path")
            items.append(Item(finding["label"], frozenset(finding["operations"]), text, report))
    return items


def _matching(bug: dict[str, Any], items: Iterable[Item]) -> list[Item]:
    pattern = re.compile(EVIDENCE[bug["id"]], re.IGNORECASE)
    is_conflict = bug["expected_label"] == "CONFLICT"
    traffic = TRAFFIC.get(bug["id"], lambda _: True)
    return [
        item
        for item in items
        if item.label not in NOT_A_DETECTION
        # A CONFLICT result says the documents disagree, so it can't be a find of another defect.
        and (is_conflict or item.label != "CONFLICT")
        and item.operations & set(bug["operations"])
        and pattern.search(item.text)
        and traffic(list(item.exchanges))
    ]


def _top_label(items: list[Item]) -> str | None:
    """The most common label. A tie counts as no clear label."""
    counts = Counter(item.label for item in items).most_common(2)
    if not counts or (len(counts) == 2 and counts[0][1] == counts[1][1]):
        return None
    return counts[0][0]


def score_buggy(
    results: dict[str, Any], workflows: list[dict[str, Any]], labels: dict[str, Any]
) -> dict[str, Any]:
    """Score one run on the buggy build against the ground truth."""
    items = _items(results, workflows)
    bugs = []
    for bug in labels["bugs"]:
        matched = _matching(bug, items)
        label = _top_label(matched)
        bugs.append(
            {
                "id": bug["id"],
                "expected": bug["expected_label"],
                "detected": bool(matched),
                "label": label,
                "correct": label == bug["expected_label"],
                "covered": any(COVERS[bug["id"]](workflow) for workflow in workflows),
            }
        )
    defects = [bug for bug in bugs if bug["expected"] != "CONFLICT"]
    detected = [bug for bug in defects if bug["detected"]]
    conflict = next(bug for bug in bugs if bug["expected"] == "CONFLICT")
    conflict_bug = next(bug for bug in labels["bugs"] if bug["id"] == conflict["id"])
    on_conflict = _matching(conflict_bug, items)
    return {
        "bugs": bugs,
        "detected": len(detected),
        "defects": len(defects),
        "correct": sum(bug["correct"] for bug in detected),
        "covered": sum(bug["covered"] for bug in defects),
        "conflict_reports": len(
            {i.report for i in on_conflict if i.label == "CONFLICT" and i.report}
        ),
        "conflict_api_bugs": sum(item.label == "API_BUG" for item in on_conflict),
        "cost_usd": results["cost_usd"],
    }


def score_clean(results: dict[str, Any]) -> dict[str, Any]:
    """Score one run on the clean build, where the only real problem is the conflict."""
    items = _items(results, [])
    tests = results["tests"]
    green = sum(test["status"] == "passed" for test in tests)
    return {
        "false_api_bugs": sum(item.label == "API_BUG" for item in items),
        "green": green,
        "tests": len(tests),
        "cost_usd": results["cost_usd"],
    }


def spent() -> list[float]:
    """Every run's cost so far."""
    if not LEDGER.is_file():
        return []
    return [json.loads(line)["cost_usd"] for line in LEDGER.read_text().splitlines()]


def next_run_is_allowed(costs: list[float], limit: float, setup_costs: list[float] = ()) -> bool:
    """Refuse a run that could pass the limit, judging by this setup's dearest run so far.

    A setup with no runs yet is judged by the dearest run of any setup.
    """
    known = list(setup_costs) or costs
    return sum(costs) + (max(known) if known else 0.0) <= limit


def setup_costs(folder: Path) -> list[float]:
    """What this setup's saved runs cost."""
    return [
        json.loads(path.read_text())["cost_usd"] for path in folder.glob("round-*/results.json")
    ]


def _record(build: str, round_number: int, results: dict[str, Any]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "build": build,
        "round": round_number,
        "cost_usd": results["cost_usd"],
        "unpriced_calls": results.get("unpriced_calls", 0),
        "stop_reason": results["stop_reason"],
    }
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _wait_until_up(url: str) -> None:
    for _ in range(100):
        try:
            httpx.get(url, timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"the seeded app didn't start at {url}")


def runs_folder(model: str, review_rounds: int) -> Path:
    """Where one setup's runs are kept, so comparing setups overwrites nothing."""
    name = re.sub(r"[^a-z0-9.-]+", "-", model.lower())
    return LEDGER.parent / "m3" / f"{name}-review-{review_rounds}"


def run_once(
    build: str, round_number: int, folder: Path, review_rounds: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run workflowval on one build, from a folder that can't see the labels or the app."""
    env = {**os.environ, "ORDERS_TOKEN": TOKEN, "SEEDED_BUILD": build}
    url = f"http://127.0.0.1:{PORT}"
    server = subprocess.Popen(  # noqa: S603 - fixed arguments
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--app-dir",
            str(SEEDED),
            "app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        env=env,
    )
    try:
        _wait_until_up(url)
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(  # noqa: S603 - fixed arguments
                [
                    str(Path(sys.executable).with_name("workflowval")),
                    "test",
                    str(SEEDED / "openapi.json"),
                    "--base-url",
                    url,
                    "--docs",
                    str(SEEDED / "docs"),
                    "--auth",
                    str(SEEDED / "auth.json"),
                    "--max-steps",
                    str(MAX_STEPS),
                    "--max-review-rounds",
                    str(review_rounds),
                ],
                cwd=temporary,
                env={k: v for k, v in env.items() if k != "SEEDED_BUILD"},
                check=False,
            )
            work = Path(temporary)
            results = json.loads((work / "results.json").read_text())
            workflows = [
                json.loads(path.read_text()) for path in sorted((work / "workflows").glob("*.json"))
            ]
            kept = folder / f"round-{round_number}-{build}"
            shutil.rmtree(kept, ignore_errors=True)
            shutil.copytree(work, kept, ignore=shutil.ignore_patterns("__pycache__"))
    finally:
        server.terminate()
        server.wait(timeout=10)
    _record(build, round_number, results)
    return results, workflows


def render_table(buggy: list[dict[str, Any]], clean: list[dict[str, Any]]) -> str:
    """The README table: each metric as its median, with its range, and the target."""

    def cell(values: list[float]) -> str:
        return f"{statistics.median(values):g} ({min(values):g} to {max(values):g})"

    costs = [round(a["cost_usd"] + b["cost_usd"], 4) for a, b in zip(buggy, clean, strict=False)]
    accuracy = [100 * r["correct"] / r["detected"] if r["detected"] else 0.0 for r in buggy]
    green = [100 * r["green"] / r["tests"] if r["tests"] else 0.0 for r in clean]
    rows = [
        ("Seeded bugs detected, of 10", cell([r["detected"] for r in buggy]), "8 or more"),
        ("Label accuracy on detected bugs, %", cell(accuracy), "80 or more"),
        (
            "False API_BUG on the clean build",
            cell([r["false_api_bugs"] for r in clean]),
            "1 or fewer",
        ),
        ("Tests green on the clean build, %", cell(green), "90 or more"),
        (
            "Bugs with a workflow that could catch them, of 10",
            cell([r["covered"] for r in buggy]),
            "10",
        ),
        ("CONFLICT reports", cell([r["conflict_reports"] for r in buggy]), "1"),
        ("API_BUG on the conflict rule", cell([r["conflict_api_bugs"] for r in buggy]), "0"),
        ("Cost per round, USD", cell(costs), "reported only"),
    ]
    lines = ["| Metric | Median (range) | Target |", "| --- | --- | --- |"]
    lines += [f"| {name} | {value} | {target} |" for name, value, target in rows]
    return "\n".join(lines) + "\n"


def rescore(folder: Path, labels: dict[str, Any]) -> int:
    """Score one model's saved runs again, with no model and no cost."""
    buggy, clean = [], []
    for path in sorted(folder.glob("round-*-buggy")):
        workflows = [json.loads(item.read_text()) for item in sorted(path.glob("workflows/*.json"))]
        buggy.append(
            score_buggy(json.loads((path / "results.json").read_text()), workflows, labels)
        )
    for path in sorted(folder.glob("round-*-clean")):
        clean.append(score_clean(json.loads((path / "results.json").read_text())))
    table = render_table(buggy, clean)
    (folder / "summary.md").write_text(table, encoding="utf-8")
    (folder / "scores.json").write_text(
        json.dumps({"buggy": buggy, "clean": clean}, indent=2), encoding="utf-8"
    )
    print(table)
    return 0


def main() -> int:
    """Run the rounds, stopping before the spending limit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--limit", type=float, default=2.0, help="Total USD across all calls")
    parser.add_argument(
        "--review-rounds", type=int, default=2, help="0 turns the Reviewer's plan review off"
    )
    parser.add_argument("--rescore", action="store_true", help="Score saved runs, at no cost")
    args = parser.parse_args()
    labels = json.loads((SEEDED / "labels.json").read_text())
    model = os.environ.get("WORKFLOWVAL_MODEL")
    if not model:
        parser.error("set WORKFLOWVAL_MODEL")
    folder = runs_folder(model, args.review_rounds)
    if args.rescore:
        return rescore(folder, labels)
    buggy_scores: list[dict[str, Any]] = []
    clean_scores: list[dict[str, Any]] = []
    for number in range(1, args.rounds + 1):
        for build in ("buggy", "clean"):
            if not next_run_is_allowed(spent(), args.limit, setup_costs(folder)):
                print(f"Stopping: another run could pass ${args.limit}. Spent ${sum(spent()):.4f}.")
                return 3
            results, workflows = run_once(build, number, folder, args.review_rounds)
            if results.get("unpriced_calls"):
                print("Stopping: a run had calls with no known price, so spend isn't reliable.")
                return 3
            if build == "buggy":
                buggy_scores.append(score_buggy(results, workflows, labels))
                print(json.dumps(buggy_scores[-1]))
            else:
                clean_scores.append(score_clean(results))
                print(json.dumps(clean_scores[-1]))
    table = render_table(buggy_scores, clean_scores)
    (folder / "summary.md").write_text(table, encoding="utf-8")
    (folder / "scores.json").write_text(
        json.dumps({"buggy": buggy_scores, "clean": clean_scores}, indent=2), encoding="utf-8"
    )
    print(table)
    print(f"Total spent, across all calls: ${sum(spent()):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
