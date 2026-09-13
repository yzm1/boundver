# Documentation style

Write for the person deciding what to do next. Start with the familiar category,
the question being answered, and the shortest working path.

## House rules

- Prefer concrete subjects and verbs: “Boundver compares the lock” is clearer
  than “comparison of recorded identities is performed.”
- Introduce one Boundver term at a time and link the [glossary](glossary.md).
- Keep limits and non-goals near the claim they qualify.
- Put tutorials in Start, procedures in Guides, facts in Reference, and
  rationale in Project.
- Keep one maintained example in the README and link to deeper recipes.
- State evidence and scope. Avoid “all teams,” “nothing else,” and other claims
  the repository cannot substantiate.
- Do not turn a readability score into a correctness claim.

## Review the result

Build the site with strict link checking, then read each rendered page you
changed at desktop and narrow widths:

```bash
python -m mkdocs build --strict
```

Check the opening, headings, code samples, links, and the route to the next
task. Readability scores can prompt a closer look, but they cannot decide
whether precise technical prose is good. Boundver does not currently make a
prose heuristic part of its test or release gates.

## Generated and specialist pages

The [CLI reference](cli-reference.md) is generated from the live argparse
parser. Edit parser help, run `python -I scripts/generate_cli_reference.py`,
and commit both changes. CI rejects a stale generated page.

Design RFCs and threat models use a specialist profile. Optimize them for
traceability, explicit invariants, and review evidence rather than landing-page
brevity.

## Prose-linter decision

Boundver does not adopt Vale for v0.16. A pilot against the Start pages did not
identify a small objective rule set that justified another release-tool
dependency; most candidate rules enforced taste or conflicted with precise
technical terms. Strict MkDocs builds, generated command documentation, and
human review remain the controls.

Reconsider an established linter only with a short advisory corpus, committed
suppressions for known false positives, and measured examples of defects it
catches. Ratchet individual high-confidence rules before making any prose check
required. Do not add a repository-wide bespoke prose scanner.
