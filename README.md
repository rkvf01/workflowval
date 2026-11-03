# workflowval

Model-agnostic CLI agent that reads an OpenAPI spec, infers multi-step workflows, writes and runs
pytest tests against a live API, and triages every failure as a test bug, an API bug or spec drift.

**Status: M1.** `test`, `plan`, `run` and `inspect` work against a live API. Bug reports, spec
patches, reports and `drift` arrive in M2, and `--ci` and `--read-only` in M3. Requirements are in
`workflowreq.md`, decisions in `docs/m0-review.md`, and changes, demo results and known limits in
`docs/m1-notes.md`.

## Quickstart

```
export OPENROUTER_API_KEY=...
export WORKFLOWVAL_MODEL=openrouter/<vendor>/<model>
workflowval inspect openapi.json
workflowval test openapi.json --base-url http://localhost:8000
workflowval run tests/wf
```

This writes `workflows/<id>.json`, `tests/wf/test_<id>.py` and `results.json` in the current folder.
If the API needs a login, pass `--auth auth.json` to both `test` and `run`, and set the env vars that
file names. See `benchmarks/targets.md` for the demo targets.

## What needs a model

| Needs no model or key | Needs a model and its key |
| --- | --- |
| `workflowval inspect` | `workflowval plan` |
| `workflowval run tests/wf` | `workflowval test` |
| Plain `pytest tests/wf` | Labeling and fixing failures |
| This repo's own lint and tests | `uv run pytest -m live` |

So you can generate tests once, commit them, and re-run them with no model cost. A failing test makes
`run` exit 1, but nothing labels the failure; run `workflowval test` to get the diagnosis. `run` and
`pytest` still need a reachable API and, for a login API, its credentials.

## Develop

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run pytest --cov=workflowval.loop --cov=workflowval.spec --cov-fail-under=80
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
