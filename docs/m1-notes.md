# M1 notes

## Decisions that change the requirements

| Area | Requirements say | M1 does | Why |
| --- | --- | --- | --- |
| Writing tests | The Writer writes one test file per workflow | Code renders each file from its workflow, and the Writer only fixes test code | Free, repeatable, and text in a workflow can't become code |
| Fixing failures | M2 adds Reviewer labeling; M1 fixes TEST_BUG only | Basic Reviewer labeling is in M1. Only `TEST_BUG` gets fixed; a real failure is never changed | A fix must not hide a real bug |
| Who fixes what | The Writer fixes TEST_BUG | The Planner fixes a wrong workflow, and the Writer fixes test code | Only the Planner may edit workflows |
| Demo in CI | A milestone is done when its demo passes in CI | The demo runs by hand locally | Owner's decision: this is a utility, with no keys in GitHub |
| GitHub Actions | The repo has `.github/workflows/ci.yml` | No workflow file. Lint, type checks and tests run locally, using the commands under "Develop" in the README | Owner's decision: skip CI for now. It's easy to add back, and needs no model or key |
| "Green" | The M1 demo produces a green suite | Each test passes, or has a real failure labeled with evidence | A real API bug must not be changed to look green |
| Missing success codes | Not covered | When the spec lists no success code, the Planner picks the usual one and marks the workflow `assumed` | Petstore lists only errors for some operations, so there's no documented code to expect |

## Demo results

Run by hand on 2026-11-02 with a low-cost model through OpenRouter. Results vary by model and run.

| Target | Result | Cost | Not covered |
| --- | --- | --- | --- |
| restful-booker, with real login | 4 of 4 pass | about $0.008 | |
| Petstore | 7 of 12 pass, 5 labeled `API_BUG` | about $0.026 | `uploadFile`, `loginUser`, `logoutUser` |

All 5 Petstore failures are one real bug, confirmed by hand: on `swaggerapi/petstore3:unstable`,
`POST /pet` with no `id` returns 500, even though the spec doesn't require an `id`. Each of the 5 tests
creates a pet first. They were left failing on purpose.

The fix loop was also tested live: a planted wrong expectation of 201 was corrected to 200 and printed
as `CHANGED`.

## Running without a model

| Works with no model or key | Needs a model and its key |
| --- | --- |
| `inspect`, `run`, and plain `pytest` on generated tests | `plan` and `test` (`test` refuses to start with no model set) |
| Lint, type checks, build, and the test suite, which uses `FakeModel` and a local stub API | Reviewer labels, fixes, and `pytest -m live` |

Re-running on CI needs the generated tests committed and a reachable API. If that API is a container,
the job needs a runner with Docker, which in practice means Ubuntu. Petstore's 5 tests will fail there
until M2 marks known bugs `xfail`.

Not built: replaying a recorded model run through `FakeModel`, which would let the whole pipeline run
with no key.

## Additions

- Workflow placeholders `{unique}` and `{unique_int}` give each run fresh data.
- `expect.body` and `save` accept a simple JSONPath subset: `$.a.b[0]`.
- After planning, code lists operations no workflow calls, and the Planner gets one more chance. What's
  left is printed as `NOT COVERED`.
- A rewrite that changes what a workflow checks is logged to `.workflowval/expectation_changes.jsonl`
  and printed as `CHANGED`.
- `--no-cost-tracking`, or `WORKFLOWVAL_COST_TRACKING=off`. `stats.json` also has cost per workflow and
  per role. Cost is the figure the provider returns with each reply, so tracking it adds no calls.
- The dependency is `litellm[mcp]`, because LiteLLM imports `mcp` as soon as a request uses tools.
- `write_verdict` lowers high confidence to medium on an `assumed` workflow.
- `write_workflow` refuses changes or deletes by a fixed value, a fixed `id` in a request body, and a
  create with no cleanup when the resource can be deleted (NFR-3). Reads may use fixed values.
- Generated tests get only the env vars they need, so provider keys never reach them.

## Known limits

- **Login token style:** decided from the auth config, then the spec's security section, then
  `Authorization: Bearer`. Working it out from the docs or by trying the API isn't built yet.
- **`write_test` checks are not a sandbox.** They restrict imports and calls in model-edited tests, but
  generated tests still run on your machine with your user's permissions. Only run this against
  specs and docs you trust.
- **`--allow-host`** only checks the base URL. Full enforcement on every request arrives in M3.
- **Not built, and refused rather than ignored:** `--read-only`, `--ci` (M3), `--report`, and the
  `drift` and `report` commands (M2). This means `--ci-max-api-bugs` and `--ci-max-spec-drift` have no
  effect yet.
- **Tools that are still stubs:** `write_bug_report`, `propose_spec_patch` and `http_probe` (M2), and
  `write_review` and `answer_review` (M3). `run_tests` is too, because the pipeline runs tests directly.
- **Responses aren't checked against the spec's schemas yet.** A test checks only the status and body
  values its workflow names. The schema check (`validate_response`) arrives in M2.
- **A labeled `API_BUG` test keeps failing.** It isn't marked `xfail` until M2, so `run` exits 1 until
  the API is fixed.
- **`run` only passes on the env vars named by `--auth`.** Without `--auth`, tests that log in fail with
  `environment variable ... is not set`, even if it is set in your shell. Plain `pytest` inherits your
  whole environment, so it doesn't need this.
- **`run` doesn't label failures,** since labeling takes a model.
- **`--max-review-rounds`** is accepted but has no effect until plan review arrives in M3.
- **Results vary by model and run**, since the Planner chooses the workflows.
