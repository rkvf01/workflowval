# M3 notes

## Decisions that change the requirements

| Area | Requirements say | M3 does | Why |
| --- | --- | --- | --- |
| When the Reviewer reviews | Before tests are written | Before the tests, and again after them, sharing `--max-review-rounds` | Owner's decision: only real results show what tests never reached |
| Going further after a failure | Not covered | Tests that have run stay as they are. A failure is a result, so reaching further takes a new test | Owner's rule: a missed bug means a missing test. Code enforces it: after a run, `write_workflow` only creates new files, and fixes are refused |
| Planner reviews each test | Pipeline step 6 | Skipped | Code renders tests from workflows, so they can't differ |
| GitHub Action | M3 checklist | Skipped | Owner's decision to skip CI. `--ci` works if it's added later |
| `http_probe` | A Reviewer tool | Still a stub, for a later release | Recorded traffic has been enough |

## How the pieces behave

- **Review rounds:** with the default of 2, one runs before the tests and one after. With 1, it runs before
  them. With 0, there's no review. A second run on an unchanged spec makes no review calls.
- **`review.json`** keeps every round. Every item is accepted naming its workflow, or rejected with a reason.
  An item the Planner ignores is recorded as rejected, so nothing is dropped.
- **`--ci`:** exits non-zero on anything unresolved, or on `API_BUG` or `SPEC_DRIFT` above the limits, which
  default to 0. Expected failures still count, so a known bug fails the build until you raise the limit.
- **`--read-only`:** writes aren't sent, and those tests are skipped from the first write onward. Reads still
  run, and a login is still allowed.
- **Hosts:** generated tests refuse any request to a host other than the base URL's or `--allow-host`,
  including under plain `pytest`.
- **Remote writes:** a non-local host needs `--allow-remote`, and then `--yes`, `--ci` or `--read-only`. The run
  lists the planned writes first.
- **Resume:** a killed run carries on from its last step if the spec is unchanged, and says `RESUMED`. Tokens,
  cost and steps carry over, and the stats file's call counts start again.

## Benchmark results

Run on 2026-11-03 on one low-cost model, for 3 rounds per setup, and scored by the rules committed then.

| Metric | M2 | M3, review off | M3, review on | Target |
| --- | --- | --- | --- | --- |
| Seeded bugs detected, of 10 | 4 (4 to 5) | 8 (7 to 9) | 9 (8 to 10) | 8 or more |
| Label accuracy on detected bugs, % | 80 (75 to 100) | 56 (50 to 57) | 75 (56 to 80) | 80 or more |
| Bugs a workflow could catch, of 10 | 6 (5 to 6) | 9 (9 to 10) | 10 (9 to 10) | 10 |
| False API_BUG on the clean build | 0 | 0 | 1 (0 to 1) | 1 or fewer |
| Tests green on the clean build, % | 100 | 100 | 97 (96 to 100) | 90 or more |
| CONFLICT reports | 0 | 0 | 0 | 1 |
| Cost per round, USD | 0.07 | 0.09 | 0.30 | reported only |

The cells show the median, with the range in brackets.

**What these results show:**

- **The broader plan prompt made most of the gain.** Asking for state transitions, wrong order, paging and rules
  from the docs took detection from 4 to 8. The plan review added about 1 more bug, at over 3 times the cost.
  With 3 rounds each, that difference could be chance.
- **The review did find what nothing else did:** the only time the missing auth check got an `API_BUG` label, and
  full coverage in 2 of 3 rounds.
- **Tests reach what they couldn't before.** The refund and pagination bugs were never detected in M2.
- **The conflict is still unreported.** Workflows now send `page_size` above 50 and expect the 200 the spec
  promises, and the API returns it, so the test passes and nothing reaches the Reviewer. One Planner even
  wrote the conflict into its rule. A conflict can only be reported through a failure at the moment.
- **The one false `API_BUG` is a real mistake in the seeded app.** Its clean build accepts a cancel with an empty
  `items` list, while the spec requires at least one item and gives 422. It wasn't planted, and the app is
  ground truth, so it stands as is.
- **A real bug this exposed, now fixed:** a spec's top-level `security` wasn't inherited, so `read_spec` showed
  operations as needing no auth. One further round after the fix had no false `CONFLICT` labels, and detected 9.
  That round isn't included in the table.

**Real APIs, on M3's code with the review off:** restful-booker gave 13 of 14 passing, 1 expected failure and
2 new mismatches, for about $0.05. Petstore wrote 25 tests and hit a 60-step limit set to save money, so 14 stayed
`UNRESOLVED` without review. That says nothing about their causes. A full Petstore run is still to do.

## Known limits

- **Runs are slower and use more steps.** A review-on round took 15 to 20 minutes, with 28 to 55 tests per run.
- **Not retried:** a rate limit from a provider still ends the run.
- **Not explained:** one model sent invalid tool calls during planning, and its runs stopped.
- **The benchmark scores a failed run as 0,** rather than flagging it.
