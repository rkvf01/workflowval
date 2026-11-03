"""Renders pytest files from workflow files, with no model involved (FR-5)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import resources
from typing import Any

from workflowval.gen.source import SourceError, check_test_source, format_source, lint_source
from workflowval.schemas import AuthConfig, LoginAuth, Workflow
from workflowval.schemas.workflow import _PLACEHOLDER, CleanupStep, KnownIssue, Step
from workflowval.spec import SpecGraph

HASH_PREFIX = "# workflow-sha256: "


class GenerationError(Exception):
    """A workflow can't be rendered against this spec."""


@dataclass(frozen=True)
class TestFile:
    """A generated test file."""

    __test__ = False

    path: str
    source: str


def workflow_hash(workflow: Workflow) -> str:
    """Hash a workflow, so a test is regenerated only when its workflow changed."""
    return hashlib.sha256(workflow.model_dump_json().encode()).hexdigest()


def hash_line(workflow: Workflow) -> str:
    """The first line of a generated test, tying it to its workflow."""
    return f"{HASH_PREFIX}{workflow_hash(workflow)}"


def test_path(workflow_id: str) -> str:
    """Where a workflow's test file lives."""
    return f"tests/wf/test_{workflow_id}.py"


def _expr(value: Any) -> str:
    """Render a JSON value as Python. Text goes through repr, so it can never become code."""
    if isinstance(value, str):
        match = _PLACEHOLDER.fullmatch(value)
        if match:
            name = match.group(1)
            return f"api.{name}" if name in {"unique", "unique_int"} else f"saved[{name!r}]"
        return f"api.fill({value!r}, saved)" if _PLACEHOLDER.search(value) else repr(value)
    if isinstance(value, dict):
        items = ", ".join(f"{key!r}: {_expr(item)}" for key, item in value.items())
        return "{" + items + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_expr(item) for item in value) + "]"
    return repr(value)


def _call_arguments(step: CleanupStep, graph: SpecGraph) -> str:
    operation = graph.operation(step.op)
    if operation is None:
        raise GenerationError(f"operation {step.op!r} isn't in the spec")
    groups: dict[str, dict[str, Any]] = {"path": {}, "params": {}, "headers": {}}
    target = {"path": "path", "query": "params", "header": "headers"}
    locations = {parameter.name: parameter.location for parameter in operation.parameters}
    for name, value in step.params.items():
        location = locations.get(name)
        if location not in target:
            raise GenerationError(f"{step.op} has no path, query or header parameter {name!r}")
        groups[target[location]][name] = value
    parts = [repr(operation.method), repr(operation.path)]
    parts += [f"{key}={_expr(group)}" for key, group in groups.items() if group]
    if step.body is not None:
        parts.append(f"body={_expr(step.body)}")
    if isinstance(step, Step) and not step.auth:
        parts.append("auth=False")
    return ", ".join(parts)


def _render_step(step: Step, graph: SpecGraph, issue: KnownIssue | None = None) -> list[str]:
    """Render one step. `issue` is set only on the step with a reviewed, expected failure."""
    lines = [f"response = api.call({_call_arguments(step, graph)})"]
    if issue is not None and issue.check == "status":
        lines += [f"if response.status_code == {issue.status}:", "    api.expected_failure()"]
    lines.append(f"assert response.status_code == {step.expect.status}, response.text")
    checks = [f"saved[{k!r}] = api.value(response, {v!r})" for k, v in step.save.items()]
    for path, value in step.expect.body.items():
        # True, False and None compare by identity, so a truthy 1 can't pass as True.
        operator = "is" if value is None or isinstance(value, bool) else "=="
        checks.append(f"assert api.value(response, {path!r}) {operator} {_expr(value)}")
    if issue is not None and issue.check == "body" and checks:
        lines.append("with api.expected_failure_on_assert():")
        return lines + [f"    {line}" for line in checks]
    return lines + checks


def _render_cleanup(step: CleanupStep, graph: SpecGraph) -> list[str]:
    used = sorted(
        set(_PLACEHOLDER.findall(repr([step.params, step.body]))) - {"unique", "unique_int"}
    )
    call = f"api.cleanup({_call_arguments(step, graph)})"
    if not used:
        return [call]
    guard = " and ".join(f"{name!r} in saved" for name in used)
    return [f"if {guard}:", f"    {call}"]


def generate(workflow: Workflow, graph: SpecGraph) -> TestFile:
    """Render a workflow as one readable pytest file."""
    issue = workflow.known_issue
    body = [
        line
        for index, step in enumerate(workflow.steps)
        for line in _render_step(step, graph, issue if issue and issue.step == index else None)
    ]
    cleanup = [line for step in workflow.cleanup for line in _render_cleanup(step, graph)]
    lines = [hash_line(workflow), repr(workflow.rule), "", "", f"def test_{workflow.id}(api):"]
    if issue is not None:
        lines.append(f"    api.known_issue({f'{issue.label}: {issue.reason}'!r})")
    if "saved" in "\n".join([*body, *cleanup]):
        lines.append("    saved = {}")
    if cleanup:
        lines.append("    try:")
        lines += [f"        {line}" for line in body]
        lines.append("    finally:")
        lines += [f"        {line}" for line in cleanup]
    else:
        lines += [f"    {line}" for line in body]
    try:
        source = format_source("\n".join(lines) + "\n")
        lint_source(source)
    except SourceError as exc:
        raise GenerationError(str(exc)) from exc
    return TestFile(test_path(workflow.id), source)


def token_style(scheme: LoginAuth, graph: SpecGraph) -> tuple[str, str]:
    """Decide how to send a login token: config first, then the spec's security section."""
    header, prefix = scheme.token_header, scheme.token_prefix
    for security in graph.security_schemes.values():
        if header is not None and prefix is not None:
            break
        kind = security.get("type")
        if kind == "http" and str(security.get("scheme", "")).lower() == "bearer":
            header, prefix = header or "Authorization", "Bearer " if prefix is None else prefix
        elif kind == "apiKey" and security.get("in") == "header":
            header, prefix = header or security["name"], "" if prefix is None else prefix
        elif kind == "apiKey" and security.get("in") == "cookie":
            default_prefix = f"{security['name']}="
            header, prefix = header or "Cookie", default_prefix if prefix is None else prefix
    return header or "Authorization", "Bearer " if prefix is None else prefix


def _auth_settings(auth: AuthConfig | None, graph: SpecGraph) -> dict[str, Any] | None:
    if auth is None:
        return None
    scheme = auth.scheme
    settings: dict[str, Any] = scheme.model_dump()
    if isinstance(scheme, LoginAuth):
        operation = graph.operation(scheme.op)
        if operation is None:
            raise GenerationError(f"auth config: operation {scheme.op!r} isn't in the spec")
        settings["path"], settings["method"] = operation.path, operation.method
        settings["token_header"], settings["token_prefix"] = token_style(scheme, graph)
    return settings


def render_conftest(base_url: str, auth: AuthConfig | None, graph: SpecGraph) -> str:
    """Render the shared conftest.py. It holds env var names only, never secret values."""
    template = resources.files(__package__).joinpath("templates/conftest.py.tmpl")
    source = template.read_text(encoding="utf-8")
    source = source.replace("__BASE_URL__", repr(base_url))
    source = source.replace("__AUTH__", repr(_auth_settings(auth, graph)))
    return format_source(source)


__all__ = [
    "GenerationError",
    "SourceError",
    "TestFile",
    "check_test_source",
    "format_source",
    "generate",
    "hash_line",
    "lint_source",
    "render_conftest",
    "test_path",
    "token_style",
    "workflow_hash",
]
