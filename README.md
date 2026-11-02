# workflowval

Model-agnostic CLI agent that reads an OpenAPI spec, infers multi-step workflows, writes and runs
pytest tests against a live API, and triages every failure as a test bug, an API bug or spec drift.

**Status: M0 skeleton.** The CLI commands are stubs. The loop, budgets, model adapters and data
formats work; nothing tests a real API yet. Requirements are in `workflowreq.md`. Open decisions for
the data-format review are in `docs/m0-review.md`.

## Develop

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run pytest --cov=workflowval.loop --cov-fail-under=80
uv run workflowval --help
```

Unit tests use `FakeModel` and block network sockets, so they need no API key.

## Models and API keys

Live runs go through LiteLLM. Pick a model with `--model provider/name` or `WORKFLOWVAL_MODEL`, and
override per role with `--role-model writer=provider/name`. Provider keys come from environment
variables only, for example `ANTHROPIC_API_KEY`. Local Ollama models (`ollama/<name>`) need no key.

To check the adapter against a real provider (costs a fraction of a cent):

```
export ANTHROPIC_API_KEY=...
uv run pytest -m live
```

## License

MIT
