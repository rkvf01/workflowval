# Model comparison

Three rounds each on the seeded app, run on 2026-11-03, with up to 2 review rounds. One model filled
every role. Each round is one run on the buggy build and one on the clean build. Results vary by run,
and three rounds is a small sample.

| Metric | Target | Haiku 5.5 | Qwen 3.8 Max (`0902`) |
| --- | --- | --- | --- |
| Seeded bugs detected, of 10: median (range) | 8 or more | 10 (9 to 10) | 9 (5 to 10) |
| Label accuracy on detected bugs, %: median (range) | 80 or more | 70 (67 to 90) | 80 (60 to 89) |
| Correct labels, all rounds, of 30 | | 22 | 18 |
| False `API_BUG` on the clean build: median (range) | 1 or fewer | 1 (1 to 1) | 1 (1 to 4) |
| Tests green on the clean build, %: median (range) | 90 or more | 98 (96 to 98) | 96 (90 to 96) |
| Bugs with a workflow that could catch them, of 10 | 10 | 10 | 10 |
| CONFLICT reports: median (range) | 1 | 0 (0 to 0) | 1 (1 to 1) |
| Cost per round, USD: median (range) | | 0.31 (0.30 to 0.35) | 8.57 (6.71 to 10.17) |
| Step limit per run | | 200 | 350 |
| Buggy runs that used every step | | 0 of 3 | 3 of 3 |

Qwen Max meets every target on the medians and Haiku misses two, label accuracy and CONFLICT reports.
That overstates the gap on labels: across all rounds, the share of detected bugs labeled correctly is
76% for Haiku (22 of 29) and 75% for Qwen Max (18 of 24).

## Correct labels by bug, of 3 rounds

| Bug | Expected | Haiku 5.5 | Qwen 3.8 Max |
| --- | --- | --- | --- |
| `wrong_validation_status` | `API_BUG` | 2 | 1 |
| `missing_auth_check` | `API_BUG` | 3 | 2 |
| `pagination_off_by_one` | `API_BUG` | 3 | 2 |
| `cancel_after_shipped` | `API_BUG` | 3 | 2 |
| `refund_after_partial_cancel` | `API_BUG` | 3 | 1 |
| `idempotency_ignored` | `API_BUG` | 3 | 1 |
| `empty_filter_500` | `API_BUG` | 3 | 3 |
| `field_renamed` | `SPEC_DRIFT` | 1 | 3 |
| `date_format` | `SPEC_DRIFT` | 0 | 3 |
| `extra_required_field` | `SPEC_DRIFT` | 1 | 0 |

Haiku labels nearly everything `API_BUG`, so it does well on real bugs and poorly on drift. Qwen Max
separates drift better and wrote a CONFLICT report in all 3 rounds, where Haiku wrote none.

## Why Qwen Max's detection varies

Most of Qwen Max's failing tests stop at their first call, the `POST /orders` that creates the order:
31 of 38 in its 5-of-10 round, against 4 to 6 per round for Haiku. Its stricter checks on that reply
catch the drift, and every test that makes that check stops there, before the later step where another
bug would show. The workflows that could catch those bugs exist, but they never reach them.

All 3 buggy runs also ended at the step limit. Stopping there cut the Reviewer's follow-up work short,
which may have left some of those blocked bugs without the new test that would catch them. The traces
show where each run stopped, not what it would have done with more steps.

## Cost and time

The 6 Qwen Max runs took about 12 hours 40 minutes and cost $25.45, and a round cost about 28 times
Haiku's. In the 3 buggy runs, more than half of the Reviewer's tool calls read the spec or the docs
(198 of 337, 230 of 381, 220 of 364).
