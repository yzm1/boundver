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

## Advisory report

`scripts/check_prose.py` reports sentences above a configurable word count and
a short list of avoidable filler phrases. It ignores front matter, code fences,
tables, inline code, links, and HTML tags where practical.

```bash
python scripts/check_prose.py
python scripts/check_prose.py README.md docs/index.md --format json
python scripts/check_prose.py C:\other\guide.md --allow-external-paths
```

The report exits `0` by default. Its findings are prompts for review, not
proof that prose is bad. False positives are expected around technical names,
lists, and sentences that need exact qualifications.
File size, file count, retained findings, display paths, and rendered output are
bounded. Crossing a limit returns exit `2` without printing a partial report;
terminal control characters are escaped in human and error output. By default,
the scanner reads regular files inside this repository and rejects symlinks.
External regular files require the explicit `--allow-external-paths` opt-in.
On Windows, the reader also denies concurrent writers and deletion while an
input is open because Windows exposes creation time rather than POSIX change
time through `st_ctime_ns`.

`--fail-on-findings` exists for a future, explicitly reviewed ratchet. Do not
enable it repository-wide until the selected document set has a stable baseline
and contributors can see how to resolve or suppress a false positive.
