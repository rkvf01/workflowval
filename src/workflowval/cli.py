"""The workflowval command line. In M0 every command validates its flags, then says it's a stub."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from workflowval import __version__
from workflowval.tools import ROLE_TOOLS

EXIT_NOT_IMPLEMENTED = 3

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
) -> None:
    """Plan workflows, write tests, run them against the API and triage every failure."""
    resolve_models(model, role_model)
    _stub("test", "M1")


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
) -> None:
    """Write reviewable workflows/<id>.json files and stop."""
    resolve_models(model, role_model)
    _stub("plan", "M1")


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
    _stub("run", "M1")


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
    resolve_models(model, role_model)
    _stub("drift", "M2")


@app.command("inspect")
def inspect_command(spec: Spec) -> None:
    """Export the spec's resources, operations and producer-to-consumer links as JSON."""
    _stub("inspect", "M1")


@app.command("report")
def report_command(
    results: Annotated[Path, typer.Argument(help="results.json from an earlier run.")],
    report: Report = None,
) -> None:
    """Write report.md and JUnit XML from an earlier run's results."""
    _stub("report", "M2")
