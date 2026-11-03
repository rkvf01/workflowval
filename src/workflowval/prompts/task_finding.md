<!-- version: 1 -->
A live response broke the spec, in a test's traffic. No test failed because of it. Rule on it.

Finding id: <<finding_id>>
What broke: <<message>>
Operations that showed it: <<operations>>
Status: <<status>>
Example response body: <<body>>
Where the spec describes this response: <<pointer>>
Credentials configured for this run: <<credentials>>

Read the spec with `read_spec` and the docs with `read_docs` if they help, then record one verdict
with `write_verdict`, using the finding id as the test id:
- `SPEC_DRIFT`: the API works and the spec no longer describes it.
- `API_BUG`: the spec and docs are right and the API is wrong.
- `CONFLICT`: the spec and docs contradict each other on this.
- `TEST_BUG`: this isn't a real problem.
