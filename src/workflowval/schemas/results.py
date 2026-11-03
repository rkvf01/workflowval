"""`results.json`: per-test outcomes and failure verdicts (FR-6, FR-8, FR-11)."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, computed_field, model_validator

from workflowval.schemas._base import Strict
from workflowval.schemas.workflow import Basis


class Label(StrEnum):
    """Every failure ends as exactly one label."""

    TEST_BUG = "TEST_BUG"
    API_BUG = "API_BUG"
    SPEC_DRIFT = "SPEC_DRIFT"
    UNRESOLVED = "UNRESOLVED"
    CONFLICT = "CONFLICT"


class Confidence(StrEnum):
    """How sure the verdict is."""

    LOW = "low"
    MED = "med"
    HIGH = "high"


class TestStatus(StrEnum):
    """What pytest reported."""

    __test__ = False

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    XFAILED = "xfailed"
    XPASSED = "xpassed"


_OK_STATUSES = frozenset({TestStatus.PASSED, TestStatus.XFAILED})


class ActionKind(StrEnum):
    """What was done about a failure."""

    PATCHED_TEST = "patched_test"
    BUG_REPORT = "bug_report"
    SPEC_PATCH = "spec_patch"
    NONE = "none"


class HttpExchange(Strict):
    """One request/response pair. Secrets must be redacted before this is built."""

    method: str
    url: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    status: int | None = Field(default=None, description="None when no response arrived")
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: Any = None


class Evidence(Strict):
    """What a verdict rests on."""

    exchanges: list[HttpExchange] = Field(default_factory=list)
    spec_excerpt: str | None = None
    docs_excerpt: str | None = None


class Action(Strict):
    """The outcome of a verdict, with the file it produced."""

    kind: ActionKind
    path: str | None = None


class Verdict(Strict):
    """One label for one failure, with confidence, evidence and an action."""

    label: Label
    confidence: Confidence
    reason: str = Field(min_length=1)
    evidence: Evidence = Field(default_factory=Evidence)
    action: Action = Field(default_factory=lambda: Action(kind=ActionKind.NONE))
    fix_target: Literal["test", "workflow"] | None = Field(
        default=None, description="For a TEST_BUG: what's wrong, the test code or the workflow"
    )


class Finding(Strict):
    """A response that breaks the spec, once however many tests saw it (FR-8, FR-10).

    `label` is None until the Reviewer rules on it. `TEST_BUG` means it ruled this isn't a real
    problem, for example a spec that's merely loose.
    """

    id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    operations: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    label: Label | None = None
    confidence: Confidence | None = None
    reason: str | None = None
    action: Action = Field(default_factory=lambda: Action(kind=ActionKind.NONE))


class TestResult(Strict):
    """The result of one test, keeping the last 3 HTTP exchanges (FR-6)."""

    __test__ = False

    id: str = Field(min_length=1, description="pytest node id")
    workflow_id: str
    basis: Basis
    status: TestStatus
    duration_s: float = Field(ge=0)
    failing_assertion: str | None = None
    stdout: str = ""
    exchanges: list[HttpExchange] = Field(default_factory=list, max_length=3)
    fix_attempts: int = Field(default=0, ge=0)
    verdict: Verdict | None = None

    @model_validator(mode="after")
    def _assumed_rules_cap_at_medium(self) -> TestResult:
        if (
            self.basis is Basis.ASSUMED
            and self.verdict is not None
            and self.verdict.confidence is Confidence.HIGH
        ):
            raise ValueError("a failure on an assumed workflow is capped at medium confidence")
        return self


class RunResults(Strict):
    """Everything a run reports."""

    stop_reason: str
    stop_detail: str | None = None
    tests: list[TestResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    uncovered_operations: list[str] = Field(
        default_factory=list, description="Spec operations no workflow calls"
    )
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0, description="Sum over calls with a known price")
    unpriced_calls: int = Field(default=0, ge=0, description="Calls missing from cost_usd")

    @property
    def has_unexpected_failures(self) -> bool:
        """True when a test failed in a way nobody has reviewed and marked as expected."""
        return any(t.status not in _OK_STATUSES for t in self.tests)

    def summary(self) -> str:
        """One line of counts, keeping expected failures apart from passes."""
        passed = sum(t.status is TestStatus.PASSED for t in self.tests)
        parts = [f"{passed} passed"]
        expected = Counter(
            t.verdict.label.value
            for t in self.tests
            if t.status is TestStatus.XFAILED and t.verdict is not None
        )
        parts += [f"{n} expected to fail ({label})" for label, n in sorted(expected.items())]
        failed = len(self.tests) - passed - sum(expected.values())
        if failed:
            parts.append(f"{failed} failed")
        confirmed = sum(f.label not in {None, Label.TEST_BUG} for f in self.findings)
        if confirmed:
            parts.append(f"{confirmed} spec mismatch{'es' if confirmed > 1 else ''}")
        return ", ".join(parts)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def label_counts(self) -> dict[str, int]:
        """Number of tests per label."""
        labels = [t.verdict.label.value for t in self.tests if t.verdict is not None]
        labels += [f.label.value for f in self.findings if f.label not in {None, Label.TEST_BUG}]
        return dict(Counter(labels))
