<!-- version: 1 -->
The Reviewer looked at the test plan. Every item needs an answer, so nothing is dropped silently.

Items:
<<items>>

For each one:
- A journey: write it as a new workflow with `write_workflow`, using source "reviewer" and the item's
  `basis`, then call `answer_review` with decision "accepted" and its `workflow_id`. Or reject it with a
  reason if it can't be tested through this API.
- A fix: correct the workflow with `write_workflow`, then accept it naming that workflow, or reject it
  with a reason.
<<note>>
Then stop.
