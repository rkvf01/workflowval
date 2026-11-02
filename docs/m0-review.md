# M0 data-format review

The requirements ask you to review the data formats before M1 starts. They live in
`src/workflowval/schemas/`. Unknown fields are errors in every file format.

| File | Model | Module |
| --- | --- | --- |
| `workflows/<id>.json` | `Workflow` | `workflow.py` |
| `review.json` | `Review` | `review.py` |
| `results.json` | `RunResults` | `results.py` |
| `benchmarks/seeded/labels.json` | `Labels` | `labels.py` |
| auth config | `AuthConfig` | `auth.py` |
| `.workflowval/state.json` | `RunState` | `state.py` |
| `.workflowval/stats.json`, `stats_history.jsonl` | `RunStats` | `stats.py` |

## Decisions the requirements leave open

M0 uses the choice in each row. Rows marked "open" still need your decision.

| # | Gap | M0 choice | Status |
| --- | --- | --- | --- |
| 1 | `--max-steps` has no default | 200, where one step is one model call | Decided: keep 200 as the start, and learn from stats (below) |
| 2 | The `--ci` threshold for API_BUG and SPEC_DRIFT is undefined | `--ci-max-api-bugs` and `--ci-max-spec-drift`, both default 0 | Decided: keep, default 0 |
| 3 | The Planner reviews generated tests and the Writer fixes them, but neither can read them | New `read_tests` tool for both | Decided: keep |
| 4 | The Reviewer labels failures, but no tool records a label | New `write_verdict` tool | Decided: keep |
| 5 | A CONFLICT report has extra fields compared with an API_BUG report | One `write_bug_report` tool with a `kind` field | Decided: keep; a conflict must include its extra fields |
| 6 | The workflow example can't send a request body | Optional `body` on steps and cleanup | Decided: keep |
| 7 | Workflows can't call a route unauthenticated, which the seeded "missing auth check" bug needs | `auth` on steps, default true | Decided: keep |
| 8 | The Planner must answer `review.json`, but no tool in its list reads it | No new tool: the loop passes the review in the Planner's instruction, built in M3 | Decided: loop passes it in |

## Learning the step limit from stats

Every run writes `.workflowval/stats.json`: model calls and tool calls per workflow, with tool names
and counts only. Work that belongs to no single workflow goes under `run`. Each finished run also adds
a line to `.workflowval/stats_history.jsonl`.

After at least 5 usable runs, a run reports a suggested `--max-steps`, about 1.5 times the largest of
the last 20 runs. If a recent run hit the current limit, it suggests 1.5 times that limit instead.
Runs ended by a provider failure or invalid model output aren't counted, because they stopped early.
The limit only changes if you change it.

## Rules added beyond the requirements

- Workflow ids are lowercase letters, digits and underscores, so an id can't escape `workflows/`.
- A workflow can't use a `{placeholder}` that no earlier step saves.
- `locked: true` needs `source: "user"`.
- `write_workflow` also refuses to overwrite a file it can't parse, since that might be a user's
  locked file with a typo, and refuses to create user-sourced or locked files.
- A verdict on an `assumed` workflow can't have high confidence.
- `rule` is required, and can't be blank: every workflow must state the business rule it checks.
  The format can't tell whether every rule in the docs has a workflow. That's the job of the Planner
  (M1) and the Reviewer's missing-journey check (M3).
- The agents create rules as they write workflows, with or without docs, and there is no separate
  rule list. Failure cases are rules too (for example, "An order with quantity 0 is rejected with
  422"). The M1 Planner prompt must say both.
- A test still open when a budget stops the run is marked UNRESOLVED, so `--ci` fails on it.
- A rule stated explicitly in the spec has `basis: "documented"`. A rule an agent inferred has
  `basis: "assumed"`. A failure on an assumed rule is lowered to medium confidence by the loop in M2,
  and `results.json` rejects high as a backstop.
- Unknown fields are rejected in the workflow file's own structure. `body` and `params` are sent as
  written, so a test can deliberately send bad values or unknown fields.
- For login auth, the agent decides how to send the token, from the spec's security section, then
  the docs, then the login response and what the API accepts. `token_header` and `token_prefix` are
  optional overrides. The agent never sees secret values.

## Decisions for later milestones

- **CI (M2, M3):** failures on `assumed` rules count toward `--ci-max-api-bugs` and
  `--ci-max-spec-drift` the same as any other. With the default of 0, one wrong guess fails the build.
  Raise the thresholds if that proves too noisy.
- **Stopping (M1):** a failing test never stops the run. A major problem, such as the API being
  unreachable, stops it with a clear reason, so failures aren't mislabeled as bugs.

**Review closed 2026-11-02.** Starting M1 needs your go-ahead.
