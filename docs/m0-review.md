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

## Decisions the requirements leave open

M0 uses the proposal in each row. Tell me which to change.

| # | Gap | M0 proposal |
| --- | --- | --- |
| 1 | `--max-steps` has no default | 200, where one step is one model call |
| 2 | The `--ci` threshold for API_BUG and SPEC_DRIFT is undefined | `--ci-max-api-bugs` and `--ci-max-spec-drift`, both default 0 |
| 3 | The Planner reviews generated tests and the Writer fixes them, but neither can read them | New `read_tests` tool for both |
| 4 | The Reviewer labels failures, but no tool records a label | New `write_verdict` tool |
| 5 | A CONFLICT report has extra fields compared with an API_BUG report | One `write_bug_report` tool with a `kind` field |
| 6 | The workflow example can't send a request body | Optional `body` on steps and cleanup |
| 7 | Workflows can't call a route unauthenticated, which the seeded "missing auth check" bug needs | `auth` on steps, default true |
| 8 | The Planner must answer `review.json`, but no tool in its list reads it | Not added yet: the loop would pass the review in the Planner's instruction. Decide at M3 |

## Rules added beyond the requirements

- Workflow ids are lowercase letters, digits and underscores, so an id can't escape `workflows/`.
- A workflow can't use a `{placeholder}` that no earlier step saves.
- `locked: true` needs `source: "user"`.
- `write_workflow` also refuses to overwrite a file it can't parse, since that might be a user's
  locked file with a typo, and refuses to create user-sourced or locked files.
- A verdict on an `assumed` workflow can't have high confidence.
- `rule` is optional, because a plain CRUD workflow may not check a business rule.
- A test still open when a budget stops the run is marked UNRESOLVED, so `--ci` fails on it.
- The auth config for a login flow has `token_header` and `token_prefix`, since restful-booker
  uses `Cookie: token=<value>`.
