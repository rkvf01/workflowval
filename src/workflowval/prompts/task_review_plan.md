<!-- version: 2 -->
Review this test plan as someone who uses the product, not as a test linter.

Read the workflows with `read_workflows`, the operations with `read_spec` and the docs with
`read_docs`. For each user the docs name, or a generic API consumer if they name none, ask: what would
this user actually do, in what order, and what would they expect to happen?

Then record one review with `write_review`:
- `fixes`: workflows that are wrong or weak, such as a missing step, a wrong expected status or a check
  that's too loose, each with its reason.
- `missing_journeys`: user flows no workflow covers, each with the user, the steps and the rule it
  checks. Consider state transitions, doing things in the wrong order, paging and filtering, repeating a
  request, missing credentials, invalid input and every business rule in the docs. Set `basis` to
  "documented" if the rule comes from the spec or docs, and "assumed" if you inferred it.
- Where the spec and docs disagree and no workflow expects what the docs say, propose a journey that does.

Give every item a short unique id, and use 1 for `round`. If nothing's missing, record empty lists.
