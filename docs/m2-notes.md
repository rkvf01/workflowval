# M2 notes

## Decisions that change the requirements

| Area | Requirements say | M2 does | Why |
| --- | --- | --- | --- |
| Who writes the seeded app | "Written by you" | The coding agent wrote it from the doc's list of bugs, and the owner reviews it | Owner's decision. The score is less independent, so `benchmarks/seeded/README.md` says so |
| Marking a real failure | Mark the test `xfail` | Code writes `known_issue` in the workflow file, and the generated test excuses only that status or check on that step | Owner's decision: any other failure stays visible, and a fix on the API side never turns a run red |
| A marked test that starts passing | Not covered | It passes, the run says `LOOKS FIXED`, and the next `workflowval test` removes the mark | Owner's decision |
| Schema mismatches | "Deterministic checks" before labeling | `workflowval test` checks every response, including those of passing tests | Owner's decision: drift in a field no workflow names would otherwise go unseen |
| `http_probe` | M2 tool | Still a stub, now due in M3 | Recorded traffic was enough for reports and patches |
| GitHub Actions | JUnit results show in GitHub Actions | JUnit is written, but no workflow runs it | Owner's decision to skip CI |

## Benchmark results

Run on 2026-11-02, on one low-cost model through OpenRouter, for 3 rounds. **These miss most of the v1
targets.** Those targets are for v1, and richer workflows and user-side plan review arrive in M3.

| Metric | Median (range) | Target |
| --- | --- | --- |
| Seeded bugs detected, of 10 | 4 (4 to 5) | 8 or more |
| Label accuracy on detected bugs, % | 80 (75 to 100) | 80 or more |
| False API_BUG on the clean build | 0 (0 to 0) | 1 or fewer |
| Tests green on the clean build, % | 100 (100 to 100) | 90 or more |
| Bugs with a workflow that could catch them, of 10 | 6 (5 to 6) | 10 |
| CONFLICT reports | 0 (0 to 0) | 1 |
| API_BUG on the conflict rule | 0 (0 to 0) | 0 |
| Cost per round, USD | 0.0684 (0.0567 to 0.072) | reported only |

Total spent, including one earlier trial round: $0.27, against a $2 limit.

Each result is scored on what it actually found. Some choices made while scoring:

- **The scoring got stricter after the first run, and only ever in that direction.** The first rules counted
  a workflow's stated rule as evidence, which gave 7 of 10 on a trial round. That was wrong, because tests
  had stopped early for another reason. A result must now describe the bug, and for bugs that words can't
  prove, the recorded traffic must show it.
- **Coverage is a stricter check than "some workflow calls this operation".** The workflow has to do what
  could expose the bug, such as asking for a second page.

**What these results show:**

- **Bugs hide bugs.** The renamed field fails most tests at their first step, so they never reach what they
  were written to check. Refund and idempotency workflows existed in most rounds and mostly never got that
  far. This is a design limit, and M3's richer workflows won't fix it by themselves.
- **Nothing tests the conflict, pagination, the missing auth check or the bad quantity.** No workflow in any
  round did what could expose them. So there are no `CONFLICT` reports.
- **The clean build is quiet:** no false `API_BUG`, and every test passed.
- **Labels vary between runs.** Some bugs got a different result in different rounds, including the same
  date bug being judged differently in the trial round.

Bug reports and patches were checked by hand. A report's curl steps reproduced cancelling a shipped order,
and the patches from the trial round applied to the original spec and left it valid.

## How the pieces behave

- **Exit codes:** `test` and `run` exit 0 when every test passed or hit its reviewed failure. Anything
  else exits 1. `drift` exits 1 when a mismatch is confirmed.
- **Summary:** expected failures stay apart from passes, for example
  `7 passed, 5 expected to fail (API_BUG), 1 spec mismatch`.
- **Findings:** each distinct way responses break the spec goes to the Reviewer once, however many tests
  saw it, up to 20 per run. Grouping ignores the bad value itself, so it works across runs. A `TEST_BUG`
  ruling means "not a real problem", and the finding is listed as dismissed.
- **Bug reports:** one per root cause, in `reports/bugs/`. Code writes the curl steps from recorded
  traffic, using env vars in place of secrets. The Reviewer writes the prose.
- **Spec patches:** in `reports/patches/`. A patch is refused unless it applies to the spec as written,
  leaves a valid spec, and makes the observed response fit, without breaking anything else in it.
- **Reports:** `reports/report.md`, `junit.xml` and `results.json`. `--report` picks another folder. Failures
  on assumed rules and open conflicts have their own sections.
- **`drift`:** re-runs saved tests and rules only on spec mismatches. It needs `workflowval test` to have
  run first, and it doesn't label or fix failing tests.

## Known limits

- **Only `workflowval test` and `drift` check responses against the spec.** `workflowval run` and plain
  `pytest` don't load the spec, so a test can pass there while `test` reports drift.
- **Locked workflows are never marked.** A real failure in one stays failing, with its label.
- **A test edited by the Writer is never re-rendered to add a mark.** That failure also stays failing.
- **The curl steps need a person for ids.** A later step's id comes from your own earlier response, as the
  report says.
- **Two reports can cover one bug.** A failing test and a finding are grouped separately, so they can each
  produce a report or patch.
- **A status the spec doesn't list counts as a mismatch,** including on real APIs with loose specs. Petstore
  shows this on `deleteOrder` and `deleteUser`.
