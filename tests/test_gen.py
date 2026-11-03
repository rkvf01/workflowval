from __future__ import annotations

import ast
from typing import Any

import pytest

from workflowval.gen import (
    GenerationError,
    format_source,
    generate,
    lint_source,
    render_conftest,
    token_style,
    workflow_hash,
)
from workflowval.schemas import AuthConfig, LoginAuth, Workflow
from workflowval.spec import Operation, Parameter, SpecGraph

GRAPH = SpecGraph(
    operations=[
        Operation(operation_id="create", method="POST", path="/items", resource="items"),
        Operation(
            operation_id="get",
            method="GET",
            path="/items/{id}",
            resource="items",
            parameters=[
                Parameter(name="id", location="path", required=True),
                Parameter(name="verbose", location="query"),
                Parameter(name="X-Trace", location="header"),
                Parameter(name="sid", location="cookie"),
            ],
        ),
    ]
)


def workflow(steps: list[dict[str, Any]], **changes: Any) -> Workflow:
    data = {"id": "w", "source": "planner", "basis": "assumed", "rule": "A rule", "steps": steps}
    return Workflow.model_validate({**data, **changes})


def create_step(**changes: Any) -> dict[str, Any]:
    return {"op": "create", "expect": {"status": 201}, **changes}


def render(steps: list[dict[str, Any]], **changes: Any) -> str:
    return generate(workflow(steps, **changes), GRAPH).source


def test_rendered_file_is_valid_named_and_clean() -> None:
    generated = generate(workflow([create_step()]), GRAPH)
    assert generated.path == "tests/wf/test_w.py"
    assert "def test_w(api):" in generated.source
    assert "response.status_code == 201" in generated.source
    assert ast.parse(generated.source)
    lint_source(generated.source)


def test_rule_becomes_the_module_docstring() -> None:
    tree = ast.parse(render([create_step()]))
    assert ast.get_docstring(tree) == "A rule"


def test_params_go_to_the_right_place_and_saved_values_carry_over() -> None:
    source = render(
        [
            create_step(save={"item": "$.id"}, body={"name": "n-{unique}", "num": "{unique_int}"}),
            {
                "op": "get",
                "params": {"id": "{item}", "verbose": True, "X-Trace": "t"},
                "expect": {"status": 200, "body": {"$.id": "{item}", "$.tags[0]": "a"}},
            },
        ]
    )
    assert 'saved["item"] = api.value(response, "$.id")' in source
    assert 'path={"id": saved["item"]}' in source
    assert 'params={"verbose": True}' in source
    assert 'headers={"X-Trace": "t"}' in source
    assert 'api.fill("n-{unique}", saved)' in source
    assert '"num": api.unique_int' in source
    assert 'assert api.value(response, "$.id") == saved["item"]' in source
    lint_source(source)


def test_booleans_and_null_compare_by_identity() -> None:
    source = render(
        [
            create_step(
                expect={"status": 201, "body": {"$.a": True, "$.b": False, "$.c": None, "$.d": 1}}
            )
        ]
    )
    assert 'assert api.value(response, "$.a") is True' in source
    assert 'assert api.value(response, "$.b") is False' in source
    assert 'assert api.value(response, "$.c") is None' in source
    assert 'assert api.value(response, "$.d") == 1' in source


def test_unauthenticated_steps_say_so() -> None:
    assert "auth=False" in render([create_step(auth=False)])
    assert "auth=" not in render([create_step()])


def test_no_saved_values_means_no_unused_variable() -> None:
    source = render([create_step()])
    assert "saved" not in source
    lint_source(source)


def test_cleanup_runs_in_finally_only_for_data_that_was_created() -> None:
    source = render(
        [create_step(save={"item": "$.id"})],
        cleanup=[{"op": "get", "params": {"id": "{item}"}}],
    )
    tree = ast.parse(source)
    assert any(isinstance(node, ast.Try) and node.finalbody for node in ast.walk(tree))
    assert 'if "item" in saved:' in source
    assert "api.cleanup(" in source
    lint_source(source)


def test_cleanup_without_saved_values_has_no_guard() -> None:
    source = render([create_step()], cleanup=[{"op": "create"}])
    assert "api.cleanup(" in source
    assert "if " not in source


def test_text_in_a_workflow_can_never_become_code() -> None:
    attack = '"; import os; os.system("x"); "'
    source = render(
        [create_step(body={"name": attack, attack: 1})], rule=f"rule {attack}\nimport os"
    )
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.Import | ast.ImportFrom) for node in ast.walk(tree))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert calls == {"call"}


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ({"op": "missing", "expect": {"status": 200}}, "isn't in the spec"),
        (create_step(params={"nope": 1}), "no path, query or header"),
        (
            {"op": "get", "params": {"id": 1, "sid": "x"}, "expect": {"status": 200}},
            "no path, query or header",
        ),
    ],
)
def test_impossible_steps_are_refused(step: dict[str, Any], message: str) -> None:
    with pytest.raises(GenerationError, match=message):
        render([step])


def test_hash_changes_with_the_workflow() -> None:
    first = workflow([create_step()])
    assert workflow_hash(first) == workflow_hash(workflow([create_step()]))
    assert workflow_hash(first) != workflow_hash(workflow([create_step()], rule="Other"))


def login(**changes: Any) -> LoginAuth:
    return LoginAuth(type="login", op="login", body_env={}, token_path="$.t", **changes)


@pytest.mark.parametrize(
    ("schemes", "expected"),
    [
        ({}, ("Authorization", "Bearer ")),
        ({"a": {"type": "http", "scheme": "bearer"}}, ("Authorization", "Bearer ")),
        ({"a": {"type": "apiKey", "in": "header", "name": "X-Token"}}, ("X-Token", "")),
        ({"a": {"type": "apiKey", "in": "cookie", "name": "token"}}, ("Cookie", "token=")),
        ({"a": {"type": "oauth2"}}, ("Authorization", "Bearer ")),
    ],
)
def test_token_style_comes_from_the_spec(
    schemes: dict[str, Any], expected: tuple[str, str]
) -> None:
    assert token_style(login(), SpecGraph(security_schemes=schemes)) == expected


def test_token_style_config_wins_over_the_spec() -> None:
    graph = SpecGraph(security_schemes={"a": {"type": "apiKey", "in": "cookie", "name": "token"}})
    assert token_style(login(token_header="X", token_prefix="Y "), graph) == ("X", "Y ")


def test_conftest_is_clean_and_holds_no_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOKER_PASS", "planted-secret-value")
    graph = SpecGraph(
        operations=[
            Operation(operation_id="login", method="POST", path="/auth", resource="auth"),
        ],
        security_schemes={"a": {"type": "apiKey", "in": "cookie", "name": "token"}},
    )
    auth = AuthConfig.model_validate(
        {
            "scheme": {
                "type": "login",
                "op": "login",
                "body_env": {"password": "BOOKER_PASS"},
                "token_path": "$.token",
            }
        }
    )
    source = render_conftest("http://localhost:1", auth, graph)
    assert "planted-secret-value" not in source
    assert "BOOKER_PASS" in source
    assert '"token_header": "Cookie"' in source
    assert '"token_prefix": "token="' in source
    assert '"path": "/auth"' in source
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "workflowval" not in roots
    lint_source(source)


def test_conftest_without_auth() -> None:
    assert "AUTH: dict[str, Any] | None = None" in render_conftest(
        "http://localhost:1", None, SpecGraph()
    )


def test_conftest_refuses_an_unknown_login_operation() -> None:
    auth = AuthConfig(scheme=login())
    with pytest.raises(GenerationError, match="isn't in the spec"):
        render_conftest("http://localhost:1", auth, SpecGraph())


def test_format_source_rejects_broken_code() -> None:
    from workflowval.gen import SourceError

    with pytest.raises(SourceError, match="can't be formatted"):
        format_source("def (:\n")
