"""Project rules enforced as tests."""

from __future__ import annotations

import ast
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

import workflowval
from workflowval.gen import generate
from workflowval.planner import plan
from workflowval.prompts import load_prompt, prompt_version
from workflowval.report import write_reports
from workflowval.schemas import RunResults, Workflow
from workflowval.spec import SpecGraph, validate_response
from workflowval.triage import classify

SOURCE = Path(workflowval.__file__).parent
PROVIDER_LIBRARIES = {
    "anthropic",
    "boto3",
    "cohere",
    "google",
    "litellm",
    "mistralai",
    "ollama",
    "openai",
    "vertexai",
}


def imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_provider_library_is_imported_outside_models() -> None:
    offenders = {
        str(path.relative_to(SOURCE)): sorted(imported_roots(path) & PROVIDER_LIBRARIES)
        for path in SOURCE.rglob("*.py")
        if path.parent.name != "models" and imported_roots(path) & PROVIDER_LIBRARIES
    }
    assert offenders == {}


def test_the_guard_can_see_a_provider_import(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_text("from openai.types import X\nimport litellm\n")
    assert imported_roots(path) & PROVIDER_LIBRARIES == {"openai", "litellm"}


def test_importing_the_cli_does_not_load_litellm() -> None:
    code = "import sys, workflowval.cli, workflowval.loop; sys.exit('litellm' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0  # noqa: S603


def test_network_is_blocked_during_unit_tests() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET).connect(("127.0.0.1", 9))


@pytest.mark.parametrize("name", ["planner", "reviewer", "writer"])
def test_prompts_have_version_headers(name: str) -> None:
    assert prompt_version(name) >= 1
    assert "version:" not in load_prompt(name)


@pytest.mark.parametrize("loader", [load_prompt, prompt_version])
def test_a_prompt_without_a_header_is_refused(
    loader: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "bad.md").write_text("no header\n")
    monkeypatch.setattr("workflowval.prompts.resources.files", lambda _: tmp_path)
    with pytest.raises(ValueError, match="version"):
        loader("bad")  # type: ignore[operator]


def test_style_rules_live_in_one_place() -> None:
    assert (SOURCE / "gen" / "style.md").is_file()


WORKFLOW = Workflow.model_validate(
    {
        "id": "a",
        "source": "planner",
        "basis": "assumed",
        "rule": "r",
        "steps": [{"op": "o", "expect": {"status": 200}}],
    }
)


@pytest.mark.parametrize(
    ("call", "milestone"),
    [
        (lambda: generate(WORKFLOW), "M1"),
        (lambda: plan(SpecGraph(), []), "M1"),
        (lambda: validate_response("op", None), "M1"),
        (lambda: classify(None), "M2"),  # type: ignore[arg-type]
        (lambda: write_reports(RunResults(stop_reason="completed")), "M2"),
    ],
)
def test_later_milestone_modules_say_when_they_arrive(call: object, milestone: str) -> None:
    with pytest.raises(NotImplementedError, match=milestone):
        call()  # type: ignore[operator]
