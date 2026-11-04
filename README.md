# workflowval

Model-agnostic CLI agent that reads an OpenAPI spec, infers multi-step workflows, writes and runs
pytest tests against a live API, and triages every failure as a test bug, an API bug or spec drift.

**Status:** all six commands work: `test`, `plan`, `run`, `drift`, `inspect` and `report`. A PyPI release is still to come.

## Start here

- **[Requirements](docs/requirements.md):** what workflowval was built to do.
- **[Conclusions](docs/conclusions.md):** how it measured up, what helped and what didn't.
- **[Model comparison](docs/model-comparison.md):** two models on the same benchmark, side by side.
- **[Drift study](docs/drift-study.md):** the same tests against three implementations of one API.
- **Build notes:** decisions in [M0](docs/m0-review.md), then changes and known limits in [M1](docs/m1-notes.md), [M2](docs/m2-notes.md) and [M3](docs/m3-notes.md).

## Quickstart

It isn't on PyPI yet, so install from a clone. It needs Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```
git clone <this repo> && cd workflowval
uv sync
source .venv/bin/activate
```

Then, from the folder where you want the tests written:

```
export OPENROUTER_API_KEY=...
export WORKFLOWVAL_MODEL=openrouter/<vendor>/<model>
workflowval inspect openapi.json
workflowval test openapi.json --base-url http://localhost:8000 --max-cost-usd 2
workflowval run tests/wf
```

This writes `workflows/<id>.json`, `tests/wf/test_<id>.py` and `results.json` in the current folder,
plus `report.md`, `junit.xml`, bug reports and spec patches in `reports/`.
If the API needs a login, pass `--auth auth.json` to both `test` and `run`, and set the env vars that
file names. In a login config, a field like `user.email` nests the login body. See `benchmarks/targets.md` for
the demo targets.

## How it works

`workflowval test` runs these steps, and saves its place after each one:

1. **Plan.** The Planner reads the spec and any docs, and writes one workflow file per multi-step scenario,
   each with the result it expects.
2. **Review the plan.** The Reviewer looks for what the plan missed, as a user of the API would, and the
   Planner adds workflows.
3. **Write tests.** Code, not a model, renders each workflow into a pytest file, so this step is free and
   repeatable.
4. **Run and label.** The tests run against the live API. Only a result that doesn't match what the workflow
   expected goes to the Reviewer, which labels it `TEST_BUG`, `API_BUG`, `SPEC_DRIFT`, `CONFLICT` or
   `UNRESOLVED`.
5. **Fix test bugs only.** The Planner corrects a wrong workflow and the Writer corrects test code. A real
   failure is never changed to make it pass. It stays as a labeled expected failure.
6. **Review the results.** The Reviewer sees the confirmed failures and asks for new workflows, including
   ones that get past a known bug.
7. **Report.** Bug reports, spec patches, `report.md` and `junit.xml` go in `reports/`.

| Part | Where |
| --- | --- |
| Commands | `src/workflowval/cli.py`, `pipeline.py` |
| Agent loop, budgets and stop rules | `src/workflowval/loop/` |
| Model adapter, the only code that imports a provider library | `src/workflowval/models/` |
| Tools the roles may call, limited per role | `src/workflowval/tools/` |
| Data formats | `src/workflowval/schemas/` |
| Spec reading and response checks | `src/workflowval/spec/` |
| Test rendering | `src/workflowval/gen/` |
| Role prompts | `src/workflowval/prompts/` |

## What needs a model

| Needs no model or key | Needs a model and its key |
| --- | --- |
| `workflowval inspect` | `workflowval plan` |
| `workflowval run tests/wf` | `workflowval test` |
| Plain `pytest tests/wf` | `workflowval drift` |
| `workflowval report` | Labeling and fixing failures |
| This repo's own lint and tests | `uv run pytest -m live`, and the benchmark |

So you can generate tests once, commit them, and re-run them with no model cost. A failure already
reviewed as real counts as expected, so it doesn't fail the run. Any other failure makes `run` exit 1,
but nothing labels it; run `workflowval test` to get the diagnosis. `run` and `pytest` still need a
reachable API and, for a login API, its credentials.

## Reading the results

| Result | Meaning | Fails the run? |
| --- | --- | --- |
| `PASSED` | The test passed | No |
| `EXPECTED` | It hit the failure already reviewed as real, and only that failure | No |
| `LOOKS FIXED` | A test marked as a known issue passed | No |
| `MISMATCH` | A response breaks the spec, though the test may pass | No, except in `drift` |
| `UNRESOLVED` | Fix attempts ran out, or the failure has no verdict | Yes |

## Benchmark

Scored against a seeded-bug app with 10 planted defects and one spec-docs conflict, for 3 rounds on one low-cost
model. See `docs/m3-notes.md` for what the numbers mean, and `benchmarks/seeded/README.md` for who wrote the app,
which affects how independent this score is.

| Metric | Median (range) | Target |
| --- | --- | --- |
| Seeded bugs detected, of 10 | 10 (9 to 10) | 8 or more |
| Label accuracy on detected bugs, % | 70 (67 to 90) | 80 or more |
| Bugs a workflow could catch, of 10 | 10 (10 to 10) | 10 |
| False API_BUG on the clean build | 1 (1 to 1) | 1 or fewer |
| Tests green on the clean build, % | 98 (96 to 98) | 90 or more |
| CONFLICT reports | 0 (0 to 0) | 1 |
| Cost per round, USD | 0.31 (0.30 to 0.35) | reported only |

Label accuracy and the conflict report still miss their targets on this model. A second model met them on
the medians, at about 28 times the cost and with less steady detection: see `docs/model-comparison.md`.

## Known limits

- **Results vary by model and run,** since models choose the workflows and labels. Three rounds is a small sample.
- **Labels are the weak spot.** About a quarter of detected bugs got the wrong label on both models tested. Check
  a label before acting on it.
- **A test stops at its first failure.** A bug in an early step can hide later bugs from every test that goes
  through that step. This is deliberate: the failure stays a result, and getting further is the job of another test.
- **Finding a conflict isn't reliable** on the low-cost model, which wrote no CONFLICT reports in the benchmark.
- **The fix loop can be inconsistent.** On the same kind of edge case, it may rewrite what some tests expect and not
  others. Rewrites that change what a test checks are printed as `CHANGED`, so review them.
- **Edge cases get missed,** and a `204` can raise a false alarm. See `docs/drift-study.md`.
- **Generated tests aren't sandboxed.** They run on your machine with your permissions, so use only specs and docs
  you trust.
- **The cost cap can go slightly over,** since it judges by the dearest call so far.
- **A run resumes only after being killed.** One that stopped on a model error, a step limit or a cost limit doesn't
  pick up where it stopped. Running it again starts a new run, which reuses a finished plan for the same spec.
- **The benchmark isn't fully independent,** because of who wrote the seeded app. See `benchmarks/seeded/README.md`.

## Safety and CI

```
workflowval test openapi.json --base-url https://staging.example.com --allow-remote --read-only
workflowval run tests/wf --ci --ci-max-api-bugs 2
```

`--read-only` sends no writes. A non-local host needs `--allow-remote`, then `--yes`, `--ci` or `--read-only`.
Generated tests refuse any other host. `--ci` fails on anything unresolved, or on bugs above the limits. A killed
`workflowval test` picks up at its last step when run again. See `docs/m3-notes.md`.

`--max-cost-usd 2` stops a run before a model call could pass that cost, judging by the dearest call so far, so a
much larger call can still go slightly over. A call with no known price also stops the run, since the limit
can't be kept. Temporary provider errors, such as rate limits, are retried up to 5 times.

Secrets are hidden in everything saved or sent to a model: known values, auth headers and fields with names like
`token`. A hidden value isn't checked against the spec. `--no-redact` keeps recorded response bodies exact, and works
only with a local API, since those bodies may hold real secrets. Everything else stays hidden.

## Develop

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run pytest --cov=workflowval.loop --cov=workflowval.spec --cov=workflowval.triage --cov-fail-under=80
uv run workflowval --help
```

Unit tests use `FakeModel` and block network sockets, so they need no API key.

## Models and API keys

Live runs go through LiteLLM. Pick a model with `--model provider/name` or `WORKFLOWVAL_MODEL`, and
override per role with `--role-model writer=provider/name`. Provider keys come from environment
variables only, for example `OPENROUTER_API_KEY`. Local Ollama models (`ollama/<name>`) need no key.

To check the adapter against a real provider:

```
export OPENROUTER_API_KEY=...
WORKFLOWVAL_LIVE_MODEL=openrouter/<vendor>/<model> uv run pytest -m live
```

## Stats

Every run writes model calls, tool calls and cost, per workflow and per role, to
`.workflowval/stats.json`, and adds a line to `.workflowval/stats_history.jsonl`. After 5 usable runs,
the run summary suggests a `--max-steps` value based on that history. It never changes the limit by
itself. See `docs/m0-review.md`.

Cost is the figure the provider returns with each reply, so tracking it adds no extra calls. To turn
it off, use `--no-cost-tracking` or set `WORKFLOWVAL_COST_TRACKING=off`.

## License

MIT
