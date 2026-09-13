# Troubleshooting

Start with the exit code and the source snapshot. Boundver fails closed when it
cannot produce a complete result, so operational problems normally return
exit `2` rather than looking clean.

## Exit 2: boundver could not complete the check

Read the first error before the summary. Common causes are invalid
configuration, a missing declared file, stale or unsupported lock metadata,
unavailable Git history, a source mismatch, or a guardrail limit.

```bash
boundver validate-config
boundver status --format json
```

Do not turn exit `2` into success in CI. Correct the input or environment and
run the same command again.

## Local verification ignores my edits

The default source is `head`, which reads committed content. Choose the view
that matches the lifecycle:

```bash
boundver verify --source working-tree  # tracked local files
boundver verify --source index         # staged snapshot
boundver verify --source head          # committed HEAD
```

Source flags apply to one invocation. Repeat the flag on the next command, and
generate and verify from the same source.

## `verify --update` used my old config

`head` and `index` read the config, lock, and component files from one captured
Git snapshot. If the config at the same working-tree path is semantically
different, missing, or unreadable, `verify --update` prints a notice naming both
config identities. It still regenerates from the selected snapshot; it never
mixes the working-tree graph into a committed or staged verification.

Use the source that represents the lock you intend to write:

```bash
# Reconcile local edits directly.
boundver verify --source working-tree --update

# Or stage the complete candidate before reconciling the index.
git add boundary.config.json path/to/changed-contract
boundver verify --source index --update --strict-config-source
```

Add `--strict-config-source` when a stale or missing working-tree config must
block regeneration. The command then exits `2` without modifying the lock. In
JSON output, inspect `notices[*].selected_config`,
`notices[*].working_tree_config`, and `notices[*].source`.

## A new declared file is missing from the index

`--source index` can only read staged files. Stage the new contract artifact
and any config change before generating the staged lock:

```bash
git add boundary.config.json path/to/new-contract.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
```

Review `git diff --cached` before committing.

## A selector matches no files

Empty selectors are errors because silently hashing nothing would leave a
contract unwatched. Check that:

- paths are relative to the component root;
- separators use `/`;
- `*.yaml` means the component root only; and
- `**/*.yaml` includes the component root and nested directories.

Use `boundver status` and `boundver why COMPONENT --source SOURCE` to inspect
the effective declaration.

## A valid config still misses source files

`validate-config` proves that declarations are valid, not complete. Run the
read-only coverage audit to find tracked files outside behavior/boundary
selectors and source directories outside component roots:

```bash
boundver coverage --source head
boundver coverage --source head --strict  # gate uncovered paths in CI
```

Declare `coverage.source_indicators` for repository-level ownership checks.
For intentional omissions, add a repository-relative `coverage.exclusions`
entry with the affected `ownership`, `behavior`, or `boundary` facet and a
reason. The report keeps exclusions separate from uncovered files.

## A generated contract is stale

When a configured derivation receipt reports stale inputs, rerun the trusted
generator and record fresh evidence from the source you intend to verify:

```bash
python path/to/generator.py
boundver record-derivation public-api --source working-tree
boundver verify --source working-tree --update
```

If outputs changed after evidence was recorded, determine what modified them,
then rerun and record rather than editing the receipt. For `index`, stage the
inputs and outputs before recording, then stage the receipt and regenerated
lock. For `head`, all four must already be committed.

The config's `generator` value is data and is never executed. A deterministic
generator `--check` remains useful in trusted CI because the receipt detects
drift but does not attest that the generator itself is correct.

## Range review cannot find its base

`boundver review` needs both immutable endpoint commits and their reconciled
configs and locks. Fetch complete history before checkout: set
`fetch-depth: 0` on `actions/checkout`, or `GIT_DEPTH: 0` in GitLab CI. Then
retry the same review command.

An ambiguous ref, absent merge base, stale endpoint lock, or incompatible
historical contract returns exit `2`.

## Range review rejects an unreconciled endpoint

`review` is a comparison between reconciled checkpoints, not the approval step
before a lock update. Both named endpoint commits must contain locks that match
their immutable source trees. Reconcile the branch tip, commit the lock, verify
`HEAD`, and retry.

A source-tree-drift error names the offending commit and performs a bounded
first-parent search for a reconciled checkpoint. It fully verifies at most the
eight nearest commits, including source-only reconciliations that did not edit
the lock or config, and stops at the first safety guardrail. Endpoints declaring
custom providers skip this search to avoid repeatedly executing repository code.
If the repository updates its lock only periodically, compare those checkpoints
explicitly instead. An unreconciled pull-request tip cannot produce a complete
`boundver-plan/v1`; do not use a failed or partial review result to skip tests.

## A facet is unavailable

`boundary` needs either a non-`leaf`, non-`implicit` provider or an `implicit`
provider with one or more paths. An empty implicit boundary has no boundary
digest. `compat` needs a `version_source`, and `behavior` needs declared
behavior paths. Either add the input or gate only facets the component supplies.

`--update` cannot manufacture an unavailable facet.

## A custom provider is rejected

Custom providers are trusted Python code and stay disabled unless the caller
explicitly opts in:

```bash
boundver verify --allow-custom-providers
```

Enable this only after reviewing the provider and its dependencies. A checked-
out configuration cannot grant itself that authority.

## I need more detail

- [CLI reference](cli-reference.md): generated command syntax and options
- [Behavioral reference](reference.md): selectors, source modes, and exit codes
- [Glossary](glossary.md): project terminology
- [CI cookbook](ci-cookbook.md): maintained CI recipes
- [Migration and ratcheting](migration-and-ratcheting.md): existing repositories
- [Security model](security-model.md): trust boundaries and safer execution

If the behavior still looks wrong, open a
[GitHub issue](https://github.com/yzm1/boundver/issues) with the boundver
version, operating system, Git version, command, exit code, and a minimal
sanitized reproduction.
