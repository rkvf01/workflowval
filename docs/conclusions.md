# Conclusions

What was built, how it measured up against `docs/requirements.md`, and what was learned. The results come from
3 rounds per setup in November 2026, a small sample. Models choose the workflows and labels, so results vary by
model and run.

## What was built

workflowval reads an OpenAPI spec and optional docs, plans multi-step workflows, turns them into plain pytest files,
runs them against a live API, and labels every failure. It writes bug reports, spec patches and reports. All six
commands work, it runs with any provider through one adapter, and saved tests re-run with no model.

## Did it meet the targets?

Scored on a seeded app with 10 planted defects and one spec-docs conflict. Cells show the median, with the range in
brackets. See `docs/model-comparison.md`.

| Metric | Target | Haiku 5.5 | Qwen 3.8 Max |
| --- | --- | --- | --- |
| Seeded bugs detected, of 10 | 8 or more | **Met:** 10 (9 to 10) | **Met:** 9 (5 to 10) |
| Label accuracy on detected bugs, % | 80 or more | **Missed:** 70 (67 to 90) | **Met:** 80 (60 to 89) |
| False `API_BUG` on the clean build | 1 or fewer | **Met:** 1 (1 to 1) | **Met:** 1 (1 to 4) |
| Tests green on the clean build, % | 90 or more | **Met:** 98 (96 to 98) | **Met:** 96 (90 to 96) |
| Bugs a workflow could catch, of 10 | 10 | **Met:** 10 | **Met:** 10 |
| CONFLICT reports | 1 | **Missed:** 0 | **Met:** 1 |
| Cost per round, USD | Reported only | 0.31 | 8.57 |

Who wrote the seeded app matters here: the same coding agent that built the tool wrote it, so the scores are less
independent than an outside benchmark. See `benchmarks/seeded/README.md`.

On real APIs, the same 58 tests ran against three RealWorld backends, for $0.07 plus $0.12 of planning. They caught
real differences between backends, such as a `500` on favoriting an article twice on one of them, and also missed some
edge cases found by hand. See `docs/drift-study.md`.

## What we concluded

1. **The approach works.** A low-cost model finds multi-step bugs reliably, for about $0.31 per round on the seeded app.
2. **Telling bug types apart is the weak spot.** Across all rounds, both models labeled about 75% of detected bugs
   correctly, so a label needs checking before anyone acts on it.
3. **A dearer model wasn't clearly better overall.** Qwen Max cost about 28 times as much per round, separated spec
   drift better and reported the conflict, but labeled real API bugs worse and detected bugs less steadily. Only two
   models were compared, so this doesn't settle the question in general.
4. **Bugs hide bugs.** A test stops at its first failure, so one early bug can block many tests. This was the cause of
   Qwen Max's 5-of-10 round.

## What helped

- **A broader plan prompt:** the biggest gain, taking detection from 4 to 8 of 10.
- **User-side review:** about 1 more bug at over 3 times the cost, which could be chance with 3 rounds. It also closed
  the coverage gap, to 10 of 10.
- **Code rendering the tests:** free, repeatable, and a test can't differ from its workflow.
- **A real API study:** it exposed token expiry and secret-hiding faults that the seeded app never showed.

## What didn't help

- **A dearer model, for detection:** see above.
- **More steps:** with 350 steps instead of 200, all 3 of Qwen Max's buggy runs still used every step.
- **More label prompt tuning:** clearer definitions fixed some labels and worsened others, and further tuning risked
  fitting the answer key, so it stopped.

## Where the build differs from the first plan

| First plan | What was built | Why |
| --- | --- | --- |
| The Writer writes each test | Code renders tests, and the Writer only fixes them | Free and repeatable, and text in a workflow can't become code |
| Review only before tests | Review before and after tests | Only real results show what tests never reached |
| `xfail` for a real failure | `known_issue` in the workflow, excusing only that failure | Any other failure on that test still shows |
| The owner writes the seeded app | The coding agent wrote it, by the owner's choice | The score is less independent, and this is disclosed |
| CI and a GitHub Action | `--ci` and JUnit output, with no workflow file | The owner chose to run checks locally |
| Cost cap deferred | `--max-cost-usd` built | Needed before spending more on model runs |

## Still open

The README lists the known limits. The ones most worth working on:

- Label accuracy, without tuning to the seeded app's answers.
- The Planner rarely tests a conflict on the low-cost model.
- The fix loop rewrites similar edge-case tests inconsistently.
- An untested idea: better Reviewer prompts might bring Qwen Max's drift and conflict gains to the low-cost model.

## Cost

Building and measuring all of this cost $34.20 in model calls, of which the Qwen Max comparison took $25.45.
