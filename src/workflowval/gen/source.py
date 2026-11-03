"""Formats, lints and checks generated test code. Everything here runs before code is executed."""

from __future__ import annotations

import ast
import subprocess
import sys

from workflowval.schemas import Workflow

ALLOWED_IMPORTS = frozenset({"pytest", "httpx", "json", "re", "uuid", "time", "datetime"})
FORBIDDEN_NAMES = frozenset(
    {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "getattr", "setattr"}
)
FORBIDDEN_ATTRIBUTES = frozenset({"skip", "skipif", "xfail", "importorskip"})
_RUFF_ARGS = ("--isolated", "--line-length", "100", "--stdin-filename", "test_generated.py")


class SourceError(Exception):
    """Test code was refused. The message says why."""


def _ruff(*args: str, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments, and source goes in through stdin
        [sys.executable, "-m", "ruff", *args],
        input=source,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def format_source(source: str) -> str:
    """Format Python source with ruff."""
    result = _ruff("format", *_RUFF_ARGS, "-", source=source)
    if result.returncode != 0:
        raise SourceError(f"code can't be formatted: {result.stderr.strip()}")
    return result.stdout


def lint_source(source: str) -> None:
    """Refuse source that doesn't pass ruff."""
    result = _ruff(
        "check",
        *_RUFF_ARGS,
        "--select",
        "E,F,I,B",
        "--ignore",
        "E501",
        "--output-format",
        "concise",
        "-",
        source=source,
    )
    if result.returncode != 0:
        raise SourceError(f"ruff found problems: {result.stdout.strip()}")


def count_assertions(tree: ast.AST) -> int:
    """Count the assert statements in parsed source."""
    return sum(isinstance(node, ast.Assert) for node in ast.walk(tree))


def check_test_source(source: str, workflow: Workflow, previous: str | None = None) -> None:
    """Refuse an edited test that could run something unexpected or hide a real failure.

    This is a safety check on model-written code, not a sandbox.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SourceError(f"not valid Python: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or "."]
        else:
            modules = []
        for module in modules:
            if module.split(".")[0] not in ALLOWED_IMPORTS:
                raise SourceError(f"import of {module!r} isn't allowed in a generated test")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise SourceError(f"{node.id!r} isn't allowed in a generated test")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            raise SourceError(f"pytest {node.attr} isn't allowed: a real failure must stay visible")
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    names = [function.name for function in functions if function.name.startswith("test")]
    expected = f"test_{workflow.id}"
    if names != [expected]:
        raise SourceError(f"the file must define exactly one test, {expected}; found {names}")
    constants = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
    for step in workflow.steps:
        if step.expect.status not in constants:
            raise SourceError(f"the test no longer checks status {step.expect.status} ({step.op})")
    if previous is not None:
        before, after = count_assertions(ast.parse(previous)), count_assertions(tree)
        if after < before:
            raise SourceError(f"assertion weakening refused: {before} assertions became {after}")
