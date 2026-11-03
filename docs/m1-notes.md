# M1 notes

## Decisions that change the requirements

| Area | Requirements say | M1 does | Why |
| --- | --- | --- | --- |
| Writing tests | The Writer writes one test file per workflow | Code renders each file from its workflow, and the Writer only fixes test code | Free, repeatable, and text in a workflow can't become code |
| Fixing failures | M2 adds Reviewer labeling; M1 fixes TEST_BUG only | Basic Reviewer labeling is in M1. Only `TEST_BUG` gets fixed; a real failure is never changed | A fix must not hide a real bug |
| Who fixes what | The Writer fixes TEST_BUG | The Planner fixes a wrong workflow, and the Writer fixes test code | Only the Planner may edit workflows |
| Demo in CI | A milestone is done when its demo passes in CI | The demo runs by hand locally | Owner's decision: this is a utility, with no keys in GitHub |
| "Green" | The M1 demo produces a green suite | Each test passes, or has a real failure labeled with evidence | A real API bug must not be changed to look green |

## Additions

- Workflow placeholders `{unique}` and `{unique_int}` give each run fresh data.
- `expect.body` and `save` accept a simple JSONPath subset: `$.a.b[0]`.
- After planning, code lists operations no workflow calls, and the Planner gets one more chance. What's
  left is printed as `NOT COVERED`.
- A rewrite that changes what a workflow checks is logged to `.workflowval/expectation_changes.jsonl`
  and printed as `CHANGED`.
- `--no-cost-tracking`, or `WORKFLOWVAL_COST_TRACKING=off`. `stats.json` also has cost per workflow and
  per role.
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
- **Not built, and refused rather than ignored:** `--read-only`, `--ci` (M3) and `--report` (M2).
- **`--max-review-rounds`** is accepted but has no effect until plan review arrives in M3.
- **Results vary by model and run**, since the Planner chooses the workflows.
