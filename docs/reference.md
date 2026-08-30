# Reference: sources, exit codes, and facet availability

This page is the single source of truth for the mechanical rules that every
other guide depends on. The tutorials and recipes link here rather than
restating them, so there is one place to correct when behavior changes.

## Source modes

Every command that accepts `--source` defaults to `head`: committed state, not
your unstaged edits.

| Source | Path set and content | Use it for |
|---|---|---|
| `head` | One captured commit tree | Pull-request and post-commit CI |
| `index` | One captured index tree | Pre-commit verification |
| `working-tree` | Disk bytes for a captured tracked path set | Local editing before you stage |

Four rules follow from that model:

1. **Generate and verify with the same source.** A lock generated from one
   snapshot does not describe another.
2. **Make files Git-known first.** After the first commit, untracked files are
   excluded from every source. Stage new files before `index`; make them
   Git-known before relying on working-tree glob expansion.
3. **`head` and `index` bind the config and the lock too**, not just component
   content. Both are read from the same captured snapshot. This is stricter
   than 0.10, which could combine staged artifacts with unstaged config.
   `verify --format json` records the exact locations and captured object IDs
   under `inputs`; text output names the selected config and lock explicitly.
4. **Fetch history** before using `--changed-from` or a `git_tag_prefix`
   version source. Shallow clones break both.

A complete staged refresh therefore looks like this:

```bash
# Stage every changed input; include boundary.config.json only when it changed.
git add services/payment/main.yaml services/payment/openapi.generated.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
```

`--changed-from` is reporting and scheduling information, not a shortcut.
Boundver still recomputes full lock integrity, so unchanged paths cannot hide
stale metadata in a component you did not touch.
Ordinary text output always names the resolved changed-component paths and
prints an explicit zero result; structured output carries the same selection in
`changed_components`.

### Diagnostic bases

Fingerprint drift accumulates from the source represented by the lock, not
necessarily from the previous commit. For `source=head`, `why` and `explain`
therefore inspect bounded lock history and default their changed-file comparison
to the commit that introduced the component's current lock entry. This remains
correct when a later partial update changes another component in the same lock.
They print the inferred base and its origin. If bounded history cannot establish
that entry-specific point, they report and use a broader root/lock-history
fallback instead of silently claiming that the previous commit is authoritative.
The inference avoids adding a commit SHA that would make identical locks differ
between `head`, `index`, and `working-tree` generation.

Use `--base-ref REF` to override the inference. `index` and `working-tree`
default to `HEAD`, because their staged or on-disk source does not yet have a
commit identity. Inference is diagnostic evidence, not lock integrity: verify
still recomputes every selected fingerprint from the requested source.

## Exit codes

| Code | Highest selected result |
|---:|---|
| `0` | Selected facets match |
| `1` | Exact or metadata drift |
| `2` | Usage, configuration, or digest error |
| `3` | Behavior drift |
| `4` | Boundary drift |
| `5` | Compatibility-family drift |

When several selected facets drift, the highest severity wins. `--fail-fast`
limits the returned report to one issue; it does not stop boundver from
evaluating every other component first, so the exit code is still the global
result.

Exit `2` is categorically different from `1` and `3`–`5`. The latter mean
"boundver checked, and something drifted." Exit `2` means boundver could not
perform a reliable check at all. Automation should treat it as a build error,
never as drift to be accepted:

```bash
set +e
boundver verify \
  --source head \
  --facets behavior,boundary,compat \
  --format json > boundver-result.json
code=$?
set -e

case "$code" in
  0) echo "Declared contracts are current" ;;
  2) echo "boundver could not perform a reliable check" >&2; exit 2 ;;
  3) echo "Behavior contract changed" >&2; exit 3 ;;
  4) echo "Boundary changed; re-verify affected consumers" >&2; exit 4 ;;
  5) echo "Compatibility family changed" >&2; exit 5 ;;
  *) echo "Unexpected boundver result: $code" >&2; exit "$code" ;;
esac
```

`--format json` exposes issues, non-gating observations, selected facets,
component selection, update status, exact input provenance, and typed
`consumer_impact`. `status`, `why`, `diff`, `slice`, and `discover` also have
`--format json`. `why` distinguishes `observed_drift` from policy-gated
`drifted`; an exact-only observation does not recommend regeneration when
`exact` is not gated.

Human-readable output renders every value as one terminal-safe line. Embedded
newlines, carriage returns, C0/C1 controls, DEL, ANSI/OSC introducers, and
Unicode line separators appear as visible escapes; a value beginning with
`::` cannot become a GitHub Actions workflow command. Ordinary Unicode remains
readable. The composite Action applies the same rule to each `issues` and
`observations` item before joining items with newlines. Structured JSON output,
including `consumer_impact`, preserves the exact machine data instead.

### Consumer graph limits

Machine-readable impact remains complete and schema-valid: a config may contain
at most 10,000 configured components and 10,000 distinct external-consumer
labels repository-wide, and each component name or external label may contain
at most 16,384 characters. `validate-config` rejects larger graphs before
generation or verification; boundver never emits a partial transitive closure.

## What each facet needs

A facet is *available* for a component only when its declaration can produce a
digest. Gating a facet a component cannot supply is a configuration error, not
drift — boundver reports `UNAVAILABLE FACET` and exits `2` rather than
inventing an identity.

| Facet | Available when |
|---|---|
| `exact` | Always, for any component with tracked files |
| `behavior` | `behavior.paths` is declared and non-empty |
| `boundary` | The provider publishes a boundary — any provider except `leaf`, and `implicit` only when it declares paths |
| `compat` | `version_source` is declared |

Two consequences are worth internalizing before you write a policy:

- **A `leaf` component never supplies `boundary`.** That is the point of
  declaring it a leaf: it consumes a contract without publishing one. It still
  supplies `exact`, and it can supply `behavior` when `behavior.paths` is
  non-empty and `compat` when `version_source` is declared.
- **A slice inherits this constraint from its members.** A slice in
  `mode: boundary` needs a boundary digest from *every* member. This bites
  most often with `closure_of`, where you do not choose the membership: the
  resolved downstream closure can pull in exactly the leaf consumers that
  cannot supply the mode. Use `exact` for heterogeneous closures.

`validate-config` applies these rules by default, so an unsatisfiable policy is
reported before `generate` runs.

The current compatibility identity must come from a declared version file or
a reachable `git_tag_prefix`. Inheriting a sibling component's identity or
declaring a validated constant is tracked in
[GitHub issue #40](https://github.com/yzm1/boundver/issues/40); neither spelling
is accepted by the v2 semantic-config contract.

### `--allow-partial`

`--allow-partial` permits *intentional* null slice inputs — a boundary slice
that temporarily includes an `implicit` component during adoption, for example.

It does not suppress failures. Missing declared paths, provider errors, broken
version sources, and vendored-copy mismatches remain fatal with or without it.

## Generated artifacts are not bound to their generator

Boundver hashes a generated contract. It does not know that the file was
generated, what produced it, or whether regenerating would change it. A stale
committed artifact therefore verifies clean.

Give the generator a deterministic `--check` mode and run it *before*
verification:

```yaml
- name: Check generated OpenAPI is current
  run: python ci/generate_platform_openapi.py --check
- name: Verify recorded boundaries
  run: boundver verify --source head
```

The check must fail when regeneration would change the tracked output. For
index workflows, stage the derivation source and the generated output together,
then generate and stage the lock.

There is deliberately no executable `derived_from` field. A checked-out config
is not authorization to execute repository commands, and a sound design also
has to bind tool identity and source materialization. Declarative
derived-artifact support is tracked in
[GitHub issue #39](https://github.com/yzm1/boundver/issues/39).

## Upgrading

Boundver pins configuration and CLI-output schemas to the release tag. A
persisted lock points to the immutable canonical publication of its structural
lock schema (`boundary-lock/v3` currently uses the v0.13.0 schema URL), so a
digest-neutral package upgrade does not dirty the lock merely to rotate a
schema annotation. A structural lock change must advance the lock schema and
select a new canonical publication. The upgrade procedure is:

```bash
python -m pip install --upgrade "boundver[schema,yaml]==0.14.1"
boundver validate-config
# Stage changed config and every changed or newly selected contract input.
git add boundary.config.json services/payment/openapi/new-route.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
git diff --cached -- boundary.config.json boundary.lock.json
```

Replace the illustrative source path with your own changed inputs, and omit
`boundary.config.json` when it did not change. If you prefer a `head` baseline,
commit the config and source changes first and generate afterwards. Update CI,
local tooling, and automation in the same change.

Hash-bearing locks are never relabelled. `boundver migrate-lock` rejects
`boundary-lock/v1` and `v2`, and rejects v3 locks carrying
`boundver-semantic-config/v1`, directing you to regenerate from repository
content instead. For what a regeneration diff *should* look like on each
upgrade path — and how to inspect 0.10 selector changes before you commit to
one — see [migration and ratcheting](migration-and-ratcheting.md).

## Related

- [Why boundver?](WHY_BOUNDVER.md) — the model, and what it cannot detect
- [Getting started](getting-started.md) — first configuration and baseline
- [Gradual adoption](gradual-adoption.md) — staged rollout in an existing repo
- [CI cookbook](ci-cookbook.md) — pipeline recipes
