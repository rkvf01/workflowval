# Seeded-bug app

An orders API with 10 planted defects and one planted spec-docs conflict, used to score workflowval.
The spec and `labels.json` are ground truth: don't edit them to change a score.

**Who wrote this:** the same coding agent that builds workflowval wrote this app, following the
defect list in `workflowreq.md`, and the project owner chose that. So these scores are less
independent than a benchmark written by someone else. As safeguards, the tool never sees this
folder's labels or source, and prompts aren't tuned around specific seeded bugs.

| File | What it's for | Does the tool see it? |
| --- | --- | --- |
| `openapi.json` | The hand-written spec. It's never generated from the code, so it can't absorb a bug | Yes |
| `docs/` | The user docs, passed with `--docs` | Yes |
| `auth.json` | Names the env var holding the token | Yes |
| `app.py`, `labels.json` | The app and the answers | No |

Run the buggy build, or the clean one with `SEEDED_BUILD=clean`, which has no defects but keeps the
conflict:

```
uv run uvicorn --app-dir benchmarks/seeded app:app --host 127.0.0.1 --port 18090
```

The app accepts the token in `ORDERS_TOKEN`, or `orders-practice-token` if that's unset. Tests need
the same value in `ORDERS_TOKEN`.
