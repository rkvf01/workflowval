"""The workflowval command line. Flags that aren't built yet refuse to run, never silently no-op."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer

from workflowval import __version__
from workflowval.loop import Budget, StopReason
from workflowval.models import LiteLLMModel, Model
from workflowval.pipeline import (
    TestConfig,
    TestRunReport,
    auth_env_names,
    results_from_outcomes,
    run_drift_pipeline,
    run_test_pipeline,
)
from workflowval.report import write_reports
from workflowval.schemas import AuthConfig, Label, RunResults, TestStatus, load_json
from workflowval.spec import SpecError, build_graph, load_spec
from workflowval.testrun import run_tests
from workflowval.tools import ROLE_TOOLS

EXIT_NOT_IMPLEMENTED = 3
EXIT_FAILURES = 1
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

HELP = """Infer multi-step API workflows from an OpenAPI spec, test them and triage every failure.

\b
Flags (see each command's --help for the ones it takes):
  --model provider/name       Model for all roles; or set WORKFLOWVAL_MODEL
  --role-model role=provider/name   Override the model for one role
  --max-review-rounds N       Plan review rounds (default 2)
  --max-fix-attempts N        Fixes per test before UNRESOLVED (default 3)
  --max-iterations N          Test-run passes per run (default 10)
  --max-steps N               Model calls per run (default 200)
  --read-only                 Block POST, PUT, PATCH and DELETE
  --allow-host HOST           Host that may be called (default: the base URL's host)
  --allow-remote              Allow hosts other than localhost
  --yes                       Skip the prompt before writes to a non-local host
  --ci                        Non-interactive; exit non-zero on failing results
  --plan-only                 Stop after writing the workflow files
  --verbose                   Echo the trace to the console
  --no-cost-tracking          Don't report cost; or set WORKFLOWVAL_COST_TRACKING=off
"""

app = typer.Typer(help=HELP, no_args_is_help=True, rich_markup_mode=None, add_completion=False)

Spec = Annotated[str, typer.Argument(help="OpenAPI 3.0/3.1 JSON spec: a file path or URL.")]
BaseUrl = Annotated[str, typer.Option("--base-url", help="Base URL of the live API.")]
OptionalBaseUrl = Annotated[
    str | None, typer.Option("--base-url", help="Base URL of the live API.")
]
ModelOpt = Annotated[
    str | None, typer.Option("--model", help="provider/name; or set WORKFLOWVAL_MODEL.")
]
RoleModel = Annotated[
    list[str] | None,
    typer.Option("--role-model", help="role=provider/name, e.g. writer=provider/cheap-model."),
]
Docs = Annotated[Path | None, typer.Option("--docs", help="Folder of Markdown, HTML or text docs.")]
Auth = Annotated[Path | None, typer.Option("--auth", help="Auth config file (JSON).")]
Report = Annotated[Path | None, typer.Option("--report", help="Directory for reports.")]
ReviewRounds = Annotated[
    int, typer.Option("--max-review-rounds", min=0, help="Plan review rounds.")
]
FixAttempts = Annotated[
    int, typer.Option("--max-fix-attempts", min=0, help="Fixes per test before UNRESOLVED.")
]
Iterations = Annotated[
    int, typer.Option("--max-iterations", min=1, help="Test-run passes per run.")
]
Steps = Annotated[int, typer.Option("--max-steps", min=1, help="Model calls per run.")]
ReadOnly = Annotated[bool, typer.Option("--read-only", help="Block POST, PUT, PATCH and DELETE.")]
AllowHost = Annotated[
    list[str] | None,
    typer.Option("--allow-host", help="Host that may be called. Default: the base URL's host."),
]
AllowRemote = Annotated[
    bool, typer.Option("--allow-remote", help="Allow hosts other than localhost.")
]
Yes = Annotated[bool, typer.Option("--yes", help="Skip the prompt before non-local writes.")]
Ci = Annotated[
    bool, typer.Option("--ci", help="Non-interactive; exit non-zero on failing results.")
]
CiApiBugs = Annotated[
    int, typer.Option("--ci-max-api-bugs", min=0, help="API_BUG count --ci tolerates.")
]
CiSpecDrift = Annotated[
    int, typer.Option("--ci-max-spec-drift", min=0, help="SPEC_DRIFT count --ci tolerates.")
]
PlanOnly = Annotated[bool, typer.Option("--plan-only", help="Stop after writing workflow files.")]
Verbose = Annotated[bool, typer.Option("--verbose", help="Echo the trace to the console.")]
Seed = Annotated[int | None, typer.Option("--seed", help="Model seed, where supported.")]
Temperature = Annotated[
    float | None, typer.Option("--temperature", min=0.0, help="Model temperature.")
]
NoCost = Annotated[
    bool,
    typer.Option(
        "--no-cost-tracking", help="Don't report cost; or set WORKFLOWVAL_COST_TRACKING=off."
    ),
]


@dataclass(frozen=True)
class ModelChoice:
    """The default model and per-role overrides."""

    default: str | None
    roles: dict[str, str]

    def for_role(self, role: str) -> str | None:
        """Return the model a role runs on."""
        return self.roles.get(role, self.default)


def _check_model_name(name: str, source: str) -> str:
    provider, _, model = name.partition("/")
    if not provider or not model:
        raise typer.BadParameter(f"{source} must look like provider/name, got {name!r}")
    return name


def resolve_models(
    model: str | None,
    role_models: list[str] | None,
    environ: dict[str, str] | None = None,
) -> ModelChoice:
    """Apply the order: --model, then WORKFLOWVAL_MODEL; --role-model overrides either."""
    env = os.environ if environ is None else environ
    default = model
    source = "--model"
    if default is None and env.get("WORKFLOWVAL_MODEL"):
        default, source = env["WORKFLOWVAL_MODEL"], "WORKFLOWVAL_MODEL"
    if default is not None:
        _check_model_name(default, source)
    roles: dict[str, str] = {}
    for item in role_models or []:
        role, separator, name = item.partition("=")
        if not separator:
            raise typer.BadParameter(f"--role-model must look like role=provider/name: {item!r}")
        if role not in ROLE_TOOLS:
            raise typer.BadParameter(f"unknown role {role!r}; roles are {sorted(ROLE_TOOLS)}")
        if role in roles:
            raise typer.BadParameter(f"--role-model given twice for {role!r}")
        roles[role] = _check_model_name(name, "--role-model")
    return ModelChoice(default, roles)


def _stub(command: str, milestone: str) -> None:
    typer.echo(
        f"workflowval {command} is not implemented yet; it arrives in {milestone}.", err=True
    )
    raise typer.Exit(EXIT_NOT_IMPLEMENTED)


def _refuse_unbuilt(**flags: bool | Path | None) -> None:
    """Refuse a flag that isn't built yet, rather than ignore a safety or reporting promise."""
    milestones = {"read_only": "M3", "ci": "M3"}
    for name, value in flags.items():
        if value:
            flag = "--" + name.replace("_", "-")
            typer.echo(f"{flag} isn't implemented yet; it arrives in {milestones[name]}.", err=True)
            raise typer.Exit(EXIT_NOT_IMPLEMENTED)


def check_host(
    base_url: str, allow_hosts: list[str] | None, *, allow_remote: bool, yes: bool
) -> None:
    """Enforce NFR-1 and NFR-2: only allowed hosts, and nothing remote without explicit consent."""
    host = urlparse(base_url).hostname
    if host is None:
        raise typer.BadParameter(f"--base-url must be a full URL, got {base_url!r}")
    if allow_hosts and host not in allow_hosts:
        raise typer.BadParameter(f"{host} isn't in --allow-host")
    if host not in LOCAL_HOSTS:
        if not allow_remote:
            raise typer.BadParameter(f"{host} isn't local: pass --allow-remote to call it")
        if not yes:
            raise typer.BadParameter(f"tests write to {host}: pass --yes to confirm")


def _load_auth(path: Path | None) -> AuthConfig | None:
    if path is None:
        return None
    try:
        return load_json(AuthConfig, path)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"can't read auth config {path}: {exc}") from exc


def _check_auth_env(auth: AuthConfig | None) -> None:
    missing = [name for name in auth_env_names(auth) if not os.environ.get(name)]
    if missing:
        raise typer.BadParameter(f"auth config needs env vars that aren't set: {missing}")


def _build_models(
    choice: ModelChoice, seed: int | None, temperature: float | None, cost_tracked: bool
) -> dict[str, Model]:
    models: dict[str, Model] = {}
    for role in ROLE_TOOLS:
        name = choice.for_role(role)
        if name is None:
            raise typer.BadParameter(
                f"no model for {role}: pass --model provider/name or set WORKFLOWVAL_MODEL"
            )
        models[role] = LiteLLMModel(
            name, temperature=temperature, seed=seed, track_cost=cost_tracked
        )
    return models


def _cost_tracked(no_cost_tracking: bool) -> bool:
    return not no_cost_tracking and os.environ.get("WORKFLOWVAL_COST_TRACKING", "") != "off"


def _print_report(report: TestRunReport) -> None:
    for test in report.results.tests:
        verdict = test.verdict
        if verdict is None:
            typer.echo(f"PASSED      {test.workflow_id}")
        elif test.status is TestStatus.XFAILED:
            typer.echo(f"EXPECTED    {test.workflow_id} ({verdict.label.value}): {verdict.reason}")
        else:
            typer.echo(f"{verdict.label.value:<11} {test.workflow_id}: {verdict.reason}")
    for change in report.expectation_changes:
        typer.echo(f"CHANGED     {change}")
    for fixed in report.looks_fixed:
        typer.echo(f"LOOKS FIXED {fixed}")
    typer.echo(report.results.summary())
    if report.results.uncovered_operations:
        operations = ", ".join(report.results.uncovered_operations)
        typer.echo(f"NOT COVERED {operations}")
    for finding in report.results.findings:
        label = finding.label.value if finding.label else "NOT REVIEWED"
        if finding.label is not Label.TEST_BUG:
            typer.echo(f"MISMATCH    {label}: {finding.message}")
    typer.echo(report.run.summary)
    typer.echo(f"results: {report.results_path}")


def _run_with(pipeline: Callable[[TestConfig], TestRunReport], config: TestConfig) -> TestRunReport:
    try:
        return pipeline(config)
    except SpecError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _run_pipeline(config: TestConfig) -> TestRunReport:
    return _run_with(run_test_pipeline, config)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"workflowval {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    """Top-level options."""


@app.command("test")
def test_command(
    spec: Spec,
    base_url: BaseUrl,
    docs: Docs = None,
    auth: Auth = None,
    report: Report = None,
    model: ModelOpt = None,
    role_model: RoleModel = None,
    max_review_rounds: ReviewRounds = 2,
    max_fix_attempts: FixAttempts = 3,
    max_iterations: Iterations = 10,
    max_steps: Steps = 200,
    read_only: ReadOnly = False,
    allow_host: AllowHost = None,
    allow_remote: AllowRemote = False,
    yes: Yes = False,
    ci: Ci = False,
    ci_max_api_bugs: CiApiBugs = 0,
    ci_max_spec_drift: CiSpecDrift = 0,
    plan_only: PlanOnly = False,
    verbose: Verbose = False,
    seed: Seed = None,
    temperature: Temperature = None,
    no_cost_tracking: NoCost = False,
) -> None:
    """Plan workflows, write tests, run them against the API and triage every failure."""
    _refuse_unbuilt(read_only=read_only, ci=ci)
    choice = resolve_models(model, role_model)
    check_host(base_url, allow_host, allow_remote=allow_remote, yes=yes)
    auth_config = _load_auth(auth)
    _check_auth_env(auth_config)
    tracked = _cost_tracked(no_cost_tracking)
    outcome = _run_pipeline(
        TestConfig(
            spec_source=spec,
            base_url=base_url,
            workdir=Path.cwd(),
            models=_build_models(choice, seed, temperature, tracked),
            budget=Budget(
                max_review_rounds=max_review_rounds,
                max_fix_attempts=max_fix_attempts,
                max_iterations=max_iterations,
                max_steps=max_steps,
            ),
            docs_dir=docs,
            auth=auth_config,
            reports_dir=report,
            plan_only=plan_only,
            verbose=verbose,
            cost_tracked=tracked,
        )
    )
    _print_report(outcome)
    clean = outcome.run.stop_reason is StopReason.COMPLETED
    if not clean or outcome.results.has_unexpected_failures:
        raise typer.Exit(EXIT_FAILURES)


@app.command("plan")
def plan_command(
    spec: Spec,
    docs: Docs = None,
    model: ModelOpt = None,
    role_model: RoleModel = None,
    max_review_rounds: ReviewRounds = 2,
    max_steps: Steps = 200,
    verbose: Verbose = False,
    seed: Seed = None,
    temperature: Temperature = None,
    no_cost_tracking: NoCost = False,
) -> None:
    """Write reviewable workflows/<id>.json files and stop."""
    choice = resolve_models(model, role_model)
    tracked = _cost_tracked(no_cost_tracking)
    outcome = _run_pipeline(
        TestConfig(
            spec_source=spec,
            base_url="http://localhost",
            workdir=Path.cwd(),
            models=_build_models(choice, seed, temperature, tracked),
            budget=Budget(max_review_rounds=max_review_rounds, max_steps=max_steps),
            docs_dir=docs,
            plan_only=True,
            verbose=verbose,
            cost_tracked=tracked,
        )
    )
    typer.echo(outcome.run.summary)
    if not outcome.planned:
        typer.echo("The spec hasn't changed, so the existing workflow files were kept.")
    if outcome.run.stop_reason is not StopReason.COMPLETED:
        raise typer.Exit(EXIT_FAILURES)


@app.command("run")
def run_command(
    tests: Annotated[Path, typer.Argument(help="Folder of generated tests.")],
    base_url: OptionalBaseUrl = None,
    auth: Auth = None,
    report: Report = None,
    read_only: ReadOnly = False,
    allow_host: AllowHost = None,
    allow_remote: AllowRemote = False,
    yes: Yes = False,
    ci: Ci = False,
    ci_max_api_bugs: CiApiBugs = 0,
    ci_max_spec_drift: CiSpecDrift = 0,
    verbose: Verbose = False,
) -> None:
    """Re-run existing tests without calling a model."""
    _refuse_unbuilt(read_only=read_only, ci=ci)
    if not tests.is_dir():
        raise typer.BadParameter(f"no such folder: {tests}")
    if base_url:
        check_host(base_url, allow_host, allow_remote=allow_remote, yes=yes)
    auth_config = _load_auth(auth)
    _check_auth_env(auth_config)
    outcomes = run_tests(tests, base_url=base_url, env_names=auth_env_names(auth_config))
    if not outcomes:
        typer.echo(f"No tests found in {tests}", err=True)
        raise typer.Exit(EXIT_FAILURES)
    for outcome in outcomes:
        if outcome.looks_fixed:
            typer.echo(f"LOOKS FIXED {outcome.workflow_id}: {outcome.known_issue}")
        elif outcome.expected:
            typer.echo(f"EXPECTED    {outcome.workflow_id}: {outcome.known_issue}")
        else:
            detail = "" if outcome.passed else f": {outcome.failing_assertion}"
            typer.echo(f"{outcome.status.upper():<11} {outcome.workflow_id}{detail}")
    passed = sum(outcome.passed for outcome in outcomes)
    expected = sum(outcome.expected for outcome in outcomes)
    failed = len(outcomes) - passed - expected
    typer.echo(f"{passed} passed, {expected} expected to fail, {failed} failed")
    if report is not None:
        write_reports(results_from_outcomes(outcomes, Path.cwd()), report)
    if failed:
        raise typer.Exit(EXIT_FAILURES)


@app.command("drift")
def drift_command(
    spec: Spec,
    base_url: BaseUrl,
    auth: Auth = None,
    report: Report = None,
    model: ModelOpt = None,
    role_model: RoleModel = None,
    max_steps: Steps = 200,
    read_only: ReadOnly = False,
    allow_host: AllowHost = None,
    allow_remote: AllowRemote = False,
    yes: Yes = False,
    ci: Ci = False,
    ci_max_spec_drift: CiSpecDrift = 0,
    verbose: Verbose = False,
    seed: Seed = None,
    temperature: Temperature = None,
) -> None:
    """Check only whether the live API still matches its spec."""
    _refuse_unbuilt(read_only=read_only, ci=ci)
    choice = resolve_models(model, role_model)
    check_host(base_url, allow_host, allow_remote=allow_remote, yes=yes)
    auth_config = _load_auth(auth)
    _check_auth_env(auth_config)
    outcome = _run_with(
        run_drift_pipeline,
        TestConfig(
            spec_source=spec,
            base_url=base_url,
            workdir=Path.cwd(),
            models=_build_models(choice, seed, temperature, _cost_tracked(False)),
            budget=Budget(max_steps=max_steps),
            auth=auth_config,
            reports_dir=report,
            verbose=verbose,
            cost_tracked=_cost_tracked(False),
        ),
    )
    _print_report(outcome)
    drifted = any(f.label not in {None, Label.TEST_BUG} for f in outcome.results.findings)
    if outcome.run.stop_reason is not StopReason.COMPLETED or drifted:
        raise typer.Exit(EXIT_FAILURES)


@app.command("inspect")
def inspect_command(spec: Spec) -> None:
    """Export the spec's resources, operations and producer-to-consumer links as JSON."""
    try:
        graph = build_graph(load_spec(spec))
    except SpecError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(graph.model_dump(by_alias=True), indent=2))


@app.command("report")
def report_command(
    results: Annotated[Path, typer.Argument(help="results.json from an earlier run.")],
    report: Report = None,
) -> None:
    """Write report.md and JUnit XML from an earlier run's results."""
    if not results.is_file():
        raise typer.BadParameter(f"no such file: {results}")
    try:
        run = load_json(RunResults, results)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(f"{results} can't be read: {exc}") from exc
    for path in write_reports(run, report or Path("reports")):
        typer.echo(f"wrote {path}")
