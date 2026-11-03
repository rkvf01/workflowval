# Running the test targets locally

Choose a model first, with `--model provider/name` or `WORKFLOWVAL_MODEL`. The `run` commands below
re-run the saved tests and need no model.

## Swagger Petstore

```
docker run -d --name wfval-petstore -p 127.0.0.1:18080:8080 swaggerapi/petstore3:unstable
workflowval test http://127.0.0.1:18080/api/v3/openapi.json --base-url http://127.0.0.1:18080
workflowval run tests/wf
```

Expect some failures labeled `API_BUG`. This image returns 500 for `POST /pet` with no `id`, so any test
that creates a pet that way fails. After review, these count as expected, so `run` exits 0. See "Demo
results" in `docs/m1-notes.md`.

## Restful-booker

The image is amd64 only, so ARM machines need `--platform linux/amd64`. These credentials are the
practice API's public defaults.

```
docker run -d --platform linux/amd64 --name wfval-booker -p 127.0.0.1:13001:3001 mwinteringham/restfulbooker:latest
export BOOKER_USER=admin BOOKER_PASS=password123
workflowval test examples/restful-booker/openapi.json --base-url http://127.0.0.1:13001 --auth examples/restful-booker/auth.json
workflowval run tests/wf --auth examples/restful-booker/auth.json
```

`run` needs `--auth` here too, or tests that log in fail because they don't get the login env vars.

## Seeded-bug app

```
WORKFLOWVAL_MODEL=provider/name uv run python benchmarks/run_benchmark.py --rounds 3 --limit 2
```

This starts the app itself, runs both builds and prints a table. It keeps total spending in
`.benchmark/spend.jsonl`, across calls, and won't start a run that could pass `--limit`. See
`benchmarks/seeded/README.md`.
