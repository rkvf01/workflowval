"""The Runner's test execution: pytest in a subprocess, with a timeout (FR-6). No model involved."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from workflowval.redact import redact

PER_TEST_TIMEOUT_SECONDS = 60
# Generated tests may have been edited by a model, so they only get the env vars they need.
_PASSED_THROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


@dataclass(frozen=True)
class Outcome:
    """One workflow's test result."""

    workflow_id: str
    status: str
    duration_s: float = 0.0
    failing_assertion: str | None = None
    stdout: str = ""
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    steps_run: int = 0
    known_issue: str | None = None

    @property
    def passed(self) -> bool:
        """True when the test passed."""
        return self.status == "passed"

    @property
    def expected(self) -> bool:
        """True when the test hit the failure it was already reviewed for."""
        return self.status == "xfailed"

    @property
    def looks_fixed(self) -> bool:
        """True when a test marked as a known issue passed."""
        return self.passed and self.known_issue is not None


def test_id(workflow_id: str) -> str:
    """The id of a workflow's one test."""
    return f"tests/wf/test_{workflow_id}.py::test_{workflow_id}"


def is_reachable(base_url: str) -> bool:
    """Check the API answers at all. Any HTTP response counts, even an error."""
    try:
        httpx.get(base_url, timeout=5)
    except httpx.HTTPError:
        return False
    return True


def _workflow_of_node(node_id: str) -> str | None:
    stem = Path(node_id.split("::")[0]).stem
    return stem.removeprefix("test_") if stem.startswith("test_") else None


def run_tests(
    tests_dir: Path,
    *,
    base_url: str | None,
    workflow_ids: Collection[str] | None = None,
    env_names: Collection[str] = (),
) -> list[Outcome]:
    """Run tests and return one outcome per workflow. A test with no result counts as an error."""
    wanted = (
        sorted(workflow_ids)
        if workflow_ids is not None
        else sorted(path.stem.removeprefix("test_") for path in tests_dir.glob("test_*.py"))
    )
    if not wanted:
        return []
    with tempfile.TemporaryDirectory() as folder:
        capture = Path(folder) / "capture.json"
        env = {
            name: os.environ[name] for name in (*_PASSED_THROUGH, *env_names) if name in os.environ
        }
        env["WORKFLOWVAL_CAPTURE"] = str(capture)
        if base_url:
            env["WORKFLOWVAL_BASE_URL"] = base_url
        files = [f"test_{workflow_id}.py" for workflow_id in wanted]
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--rootdir=.",
            "--confcutdir=.",
            "-p",
            "no:cacheprovider",
            *files,
        ]
        try:
            subprocess.run(  # noqa: S603 - fixed command; test files are checked before they're written
                command,
                cwd=tests_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=PER_TEST_TIMEOUT_SECONDS * len(files),
            )
        except subprocess.TimeoutExpired:
            return [Outcome(w, "error", failing_assertion="the test run timed out") for w in wanted]
        captured = json.loads(capture.read_text(encoding="utf-8")) if capture.exists() else {}
    outcomes: dict[str, Outcome] = {}
    for node_id, result in captured.items():
        workflow_id = _workflow_of_node(node_id)
        if workflow_id is not None:
            outcomes[workflow_id] = Outcome(workflow_id, **redact(result))
    return [
        outcomes.get(w) or Outcome(w, "error", failing_assertion="pytest reported no result")
        for w in wanted
    ]
