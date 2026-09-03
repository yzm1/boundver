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

## A generated contract is stale

Boundver fingerprints the generated artifact, not the command that produced
it. Run the project's deterministic generator check first:

```bash
python path/to/generator.py --check
boundver verify --source head
```

If the generator has no check mode, generate into a temporary location and
compare it in CI before verification. Do not place an executable command in
the boundver config.

## Range review cannot find its base

`boundver review` needs both immutable endpoint commits and their reconciled
configs and locks. Fetch complete history before checkout: set
`fetch-depth: 0` on `actions/checkout`, or `GIT_DEPTH: 0` in GitLab CI. Then
retry the same review command.

An ambiguous ref, absent merge base, stale endpoint lock, or incompatible
historical contract returns exit `2`.

## A facet is unavailable

`boundary` needs either a non-`leaf` provider or an `implicit` provider with
one or more paths. An empty implicit boundary has no boundary digest. `compat`
needs a `version_source`, and `behavior` needs declared behavior paths. Either
add the input or gate only facets the component supplies.

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

- [Reference](reference.md): commands, selectors, source modes, and exit codes
- [Glossary](glossary.md): project terminology
- [CI cookbook](ci-cookbook.md): maintained CI recipes
- [Migration and ratcheting](migration-and-ratcheting.md): existing repositories
- [Security model](security-model.md): trust boundaries and safer execution

If the behavior still looks wrong, open a
[GitHub issue](https://github.com/yzm1/boundver/issues) with the boundver
version, operating system, Git version, command, exit code, and a minimal
sanitized reproduction.
