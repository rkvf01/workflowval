<!-- version: 1 -->
This was judged SPEC_DRIFT: <<reason>>
Observed response body: <<body>>
Where the spec describes this response: <<pointer>>

Propose the smallest JSON Patch (RFC 6902) that makes the spec describe what the API does, with
`propose_spec_patch` and test_id "<<target>>". Patches apply to the spec as written, including its
`$ref`s, so read the parts you need with `read_spec` and a `pointer`. Follow any `$ref` you meet. If
the tool refuses a patch, read why, correct it and try again. Then stop.
