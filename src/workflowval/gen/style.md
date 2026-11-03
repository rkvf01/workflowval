# Generated test style

Tests are rendered from workflow files by `workflowval.gen`, so these rules hold by construction.
The Writer model only edits a test to fix a failure labeled `TEST_BUG`, and `write_test` enforces
the rules below on its edits.

- One file per workflow, `tests/wf/test_<id>.py`, with one function, `test_<id>(api)`.
- Plain pytest + httpx through the `api` fixture in `conftest.py`. No workflowval import.
- Each step's status code and values are checked with a plain `assert`.
- Cleanup runs in `finally`, and only touches data the test created.
- Fresh data per run comes from `api.unique` and `api.unique_int`.
- No secrets in test files: credentials come from env vars inside `conftest.py`.
- Only these imports: pytest, httpx, json, re, uuid, time, datetime.
- No `skip` or `xfail`, and no fewer assertions than before. A real failure must stay visible.
- `ruff` passes (rules E, F, I, B; line length isn't enforced on long literals).
