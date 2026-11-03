<!-- version: 3 -->
Write the test plan for this API.

Operations:
<<operations>>

Locked workflows, which you must leave alone: <<locked>>

Use `read_spec` with an operation_id for each operation you use, and `read_docs` to find business
rules. Write workflows for: the CRUD lifecycle of each resource, state transitions, doing things in the
wrong order, pagination and filtering, missing credentials, invalid input, and each business rule in the
docs. Where the spec documents an expected error, cover it. Where the spec and docs disagree, write a
workflow that expects what the docs say, and say so in its rule. Then stop.
