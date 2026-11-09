# Historical lock fixture

`v0.15.2.json` preserves the parsed contents of `boundary.lock.json` from
immutable release commit `b8c886100694a0b9f9d45502d072876f07c43444`
(`v0.15.2`). Whitespace is compacted; values and identities are unchanged.

`v0.16.0.json` likewise preserves the v4/semantic-config-v3 record from immutable
release commit `af31622ee0a3b6f3e447b4d2dadfa51e94eac79f` (`v0.16.0`).

This is an authentic v3/semantic-config-v2 record, not a current lock with a
changed schema label. Tests may explicitly construct labelled synthetic variants
to test cross-contract refusal; those variants are never migration examples.
No test needs to fetch historical tags to read this fixture.
