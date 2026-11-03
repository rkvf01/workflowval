<!-- version: 2 -->
Write the test plan for this API.

Operations:
<<operations>>

Locked workflows, which you must leave alone: <<locked>>

Use `read_spec` with an operation_id for each operation you use, and `read_docs` to find business
rules. Write workflows for: the CRUD lifecycle of each resource, state transitions, doing things in the
wrong order, pagination and filtering, missing credentials, invalid input, and each business rule in the
docs. Where the spec documents an expected error, cover it. Then stop.
