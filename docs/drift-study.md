# Drift study: three RealWorld backends

RealWorld "Conduit" is one API spec with many implementations. This study ran one set of 58 tests,
written once from the spec, against three backends, so any difference comes from the backend and
not from a different plan. It ran on 2026-11-03 on a low-cost model. Results vary by model and run.

| Backend | Language | Notes |
| --- | --- | --- |
| [Django Ninja](https://github.com/c4ffein/realworld-django-ninja) | Python | SQLite, `DEBUG=True`, which may affect its 500 responses |
| [Nitro + Prisma + Zod](https://github.com/realworld-apps/nitro-prisma-zod-realworld-example-app) | TypeScript | SQLite, dev server |
| [ASP.NET Core](https://github.com/realworld-apps/aspnetcore-realworld-example-app) | C# | SQLite, its own Dockerfile |

Spec: `specs/api/openapi.yml` in [realworld-apps/realworld](https://github.com/realworld-apps/realworld),
version 2.0.0, 19 operations, converted to JSON. No docs were given. See `benchmarks/targets.md` to
run it. The first two backends are listed as spec-compliant by the RealWorld project, so most of
what differs is edge cases.

## What differs

Each row was confirmed by hand with curl. "Caught" means the final run's tests reported it.

| Request | Spec says | Django Ninja | Nitro | ASP.NET | Caught? |
| --- | --- | --- | --- | --- | --- |
| Favorite an article twice | 200 | **500** | 200 | 200 | Yes |
| List articles: author's `bio` | Required | Present | **Missing** | Present | Yes |
| `limit=0` | Minimum 1 | 200 | 200 | 200 | Partly, see below |
| `offset=-1` | Minimum 0 | **500** | **500** | 200 | Nitro only |
| `limit=-1` | Minimum 1 | **500** | 200 | 200 | No |
| `limit=abc` | Integer | 422 | **200** | 422 | No |
| Invalid login token | 401 | **500** | 401 | 401 | No |

The spec's only client-error response here is 422, described as "Unexpected error", and it doesn't say
out-of-range values must get it. So the three `limit` and `offset` rows returning 200 are a fair
reading of the minimum, not a certainty. The Reviewer gave them medium confidence.

## Results

| Backend | Passed | Expected to fail | Bug reports | Run cost |
| --- | --- | --- | --- | --- |
| Django Ninja | 56 | 2 | 2 | $0.02 |
| Nitro | 56 | 2 | 3 | $0.04 |
| ASP.NET Core | 56 | 2 | 2 | $0.01 |

Planning cost a further $0.12, once. Repeated runs against other backends are cheap, because only
failures need the model.

## What it showed about workflowval

- **The same bug can get different results.** All three backends accept `limit=0`. In the final run
  the fix loop treated that as a wrong test on Django Ninja for `/articles`, and on Nitro for the feed,
  and rewrote the expected 422 to 200, so those tests passed. The other cases got `API_BUG`. Each rewrite
  is printed as `CHANGED`. This is a known limit, not fixed.
- **Labels vary between runs.** Nitro's missing `bio` was `SPEC_DRIFT` in one run and `API_BUG` in the next.
- **A test stops at its first failure.** On Django Ninja the paging test never reached `offset=-1`. By design,
  reaching it takes another test, and the Reviewer's review was off here to keep one plan across backends.
  Nitro's `offset=-1` was caught only because the rewrite above let the test carry on.
- **Empty 204 responses are flagged, then dismissed.** The response check reports "missing response data"
  on 204s the spec describes as empty. The Reviewer correctly dismissed it on all three backends, which
  costs a model call each time. Not fixed.

## What the study changed

An earlier pass found three problems, all fixed before the results above:

- A fixed token from an env var expired after 5 minutes on two backends, causing most of that pass's
  failures. Login config can now nest fields, as in `user.email`, so the agent logs in itself.
- Hiding secrets corrupted recorded JSON, such as `{"errors": {"token": ["is missing"]}}`, and every 401 looked
  like `SPEC_DRIFT`. Recorded JSON now stays valid, and a hidden value is not checked against the spec.
- `--no-redact` was added, for local APIs only.

## Limits

- Three backends, one plan and one low-cost model. Labels weren't all reviewed one by one.
- The hand checks covered what the runs raised, plus a few paging and token cases, not the whole API.
- The planned model comparison isn't done yet, so this says nothing about which model is best.
