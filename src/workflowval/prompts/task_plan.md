<!-- version: 1 -->
Write the test plan for this API.

Operations:
<<operations>>

Locked workflows, which you must leave alone: <<locked>>

Use `read_spec` with an operation_id for each operation you use, and `read_docs` to find business
rules. Write one CRUD lifecycle workflow per resource (create, read, update, delete, then verify it's
gone), plus one failure case per resource where the spec documents the expected error. Then stop.
