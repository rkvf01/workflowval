from __future__ import annotations

import re

import pytest
import typer
from typer.testing import CliRunner

from workflowval import __version__
from workflowval.cli import EXIT_NOT_IMPLEMENTED, app, resolve_models

runner = CliRunner()

COMMANDS = {"test", "plan", "run", "drift", "inspect", "report"}
FR12_FLAGS = {
    "--model",
    "--role-model",
    "--max-review-rounds",
    "--max-fix-attempts",
    "--max-iterations",
    "--max-steps",
    "--read-only",
    "--allow-host",
    "--ci",
}
OTHER_DOC_FLAGS = {"--allow-remote", "--yes", "--plan-only", "--verbose"}


def test_top_level_help_documents_every_command_and_flag() -> None:
    result = runner.invoke(app, ["--help"], terminal_width=120)
    assert result.exit_code == 0
    for item in [*COMMANDS, *FR12_FLAGS, *OTHER_DOC_FLAGS]:
        assert re.search(rf"(^|\s){re.escape(item)}(\s|$)", result.output, re.M), item


def test_test_command_takes_all_the_flags() -> None:
    output = runner.invoke(app, ["test", "--help"], terminal_width=120).output
    expected = {
        *FR12_FLAGS,
        *OTHER_DOC_FLAGS,
        "--base-url",
        "--docs",
        "--auth",
        "--report",
        "--seed",
        "--temperature",
        "--ci-max-api-bugs",
        "--ci-max-spec-drift",
    }
    for flag in expected:
        assert flag in output, flag


def test_no_arguments_shows_help() -> None:
    assert "Commands:" in runner.invoke(app, []).output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert (result.exit_code, result.output.strip()) == (0, f"workflowval {__version__}")


@pytest.mark.parametrize(
    ("args", "milestone"),
    [
        (["test", "openapi.json", "--base-url", "http://localhost:8000"], "M1"),
        (["plan", "openapi.json"], "M1"),
        (["run", "tests/wf", "--ci"], "M1"),
        (["drift", "spec.json", "--base-url", "https://sandbox.partner.com"], "M2"),
        (["inspect", "openapi.json"], "M1"),
        (["report", "results.json"], "M2"),
    ],
)
def test_stubs_exit_non_zero_and_say_when_they_arrive(args: list[str], milestone: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert f"not implemented yet; it arrives in {milestone}" in result.output


@pytest.mark.parametrize("flag", ["--max-iterations", "--max-steps"])
def test_limits_below_one_are_refused(flag: str) -> None:
    result = runner.invoke(app, ["test", "s.json", "--base-url", "http://x", flag, "0"])
    assert result.exit_code == 2


def test_bad_model_name_is_a_usage_error() -> None:
    result = runner.invoke(
        app, ["test", "s.json", "--base-url", "http://x", "--model", "no-provider"]
    )
    assert result.exit_code == 2


def test_model_flag_beats_the_environment() -> None:
    choice = resolve_models("a/flag", None, {"WORKFLOWVAL_MODEL": "b/env"})
    assert choice.default == "a/flag"


def test_environment_is_used_without_the_flag() -> None:
    assert resolve_models(None, None, {"WORKFLOWVAL_MODEL": "b/env"}).default == "b/env"


def test_no_model_anywhere_is_none() -> None:
    assert resolve_models(None, None, {}).default is None


def test_role_model_overrides_only_its_role() -> None:
    choice = resolve_models("a/strong", ["writer=b/cheap"], {})
    assert choice.for_role("writer") == "b/cheap"
    assert choice.for_role("reviewer") == "a/strong"


def test_process_environment_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOWVAL_MODEL", "c/from-process")
    assert resolve_models(None, None).default == "c/from-process"


@pytest.mark.parametrize(
    ("model", "roles", "env", "message"),
    [
        ("nope", None, {}, "provider/name"),
        (None, None, {"WORKFLOWVAL_MODEL": "nope"}, "WORKFLOWVAL_MODEL"),
        ("/x", None, {}, "provider/name"),
        ("x/", None, {}, "provider/name"),
        (None, ["writer"], {}, "role=provider/name"),
        (None, ["runner=a/b"], {}, "unknown role"),
        (None, ["writer=nope"], {}, "provider/name"),
        (None, ["writer=a/b", "writer=c/d"], {}, "twice"),
    ],
)
def test_bad_model_settings_are_refused(
    model: str | None, roles: list[str] | None, env: dict[str, str], message: str
) -> None:
    with pytest.raises(typer.BadParameter, match=message):
        resolve_models(model, roles, env)
