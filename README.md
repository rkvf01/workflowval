# workflowval

Model-agnostic CLI agent that reads an OpenAPI spec, infers multi-step workflows, writes and runs
pytest tests against a live API, and triages every failure as a test bug, an API bug or spec drift.

**Status: M3.** All six commands work: `test`, `plan`, `run`, `drift`, `inspect` and `report`. Requirements
are in `workflowreq.md`, decisions in `docs/m0-review.md`, and changes and known limits in `docs/m1-notes.md`,
`docs/m2-notes.md` and `docs/m3-notes.md`.

## Quickstart

```
export OPENROUTER_API_KEY=...
export WORKFLOWVAL_MODEL=openrouter/<vendor>/<model>
workflowval inspect openapi.json
workflowval test openapi.json --base-url http://localhost:8000
workflowval run tests/wf
```

This writes `workflows/<id>.json`, `tests/wf/test_<id>.py` and `results.json` in the current folder,
plus `report.md`, `junit.xml`, bug reports and spec patches in `reports/`.
If the API needs a login, pass `--auth auth.json` to both `test` and `run`, and set the env vars that
file names. See `benchmarks/targets.md` for the demo targets.

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

Label accuracy and the conflict report still miss their targets.

## Safety and CI

```
workflowval test openapi.json --base-url https://staging.example.com --allow-remote --read-only
workflowval run tests/wf --ci --ci-max-api-bugs 2
```

`--read-only` sends no writes. A non-local host needs `--allow-remote`, then `--yes`, `--ci` or `--read-only`.
Generated tests refuse any other host. `--ci` fails on anything unresolved, or on bugs above the limits. A killed
`workflowval test` picks up at its last step when run again. See `docs/m3-notes.md`.

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
