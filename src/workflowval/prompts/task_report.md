<!-- version: 1 -->
A failure was judged <<label>>: <<reason>>
Tests that show it: <<tests>>

Write one report with `write_bug_report`, using kind "<<kind>>" and test_id "<<test_id>>". Code adds
the curl steps and the affected tests from what was recorded. Give a clear title, what was expected
and from where, what actually happened, and a spec reference such as an operation id. For a conflict,
also quote the docs in `docs_excerpt`, name the affected workflows and suggest a resolution. Read the
spec with `read_spec` and the docs with `read_docs` as needed. Then stop.
