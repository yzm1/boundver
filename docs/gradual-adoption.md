# Gradual adoption guide

You do not need to model an entire repository on day one. Start with one
component, make one signal trustworthy, then expand the contract deliberately.

This guide uses the current `boundary-lock/v3` +
`boundver-semantic-config/v2` contract. If the repository has a v3/v1 lock
from 0.11 or a v1/v2 lock, [upgrade](reference.md#upgrading) before mixing
old and new writers.

## Stage 1: record exact drift

Choose one non-root component directory. Use `implicit` while its public
boundary is still unknown:

```json
{
  "project": "my-project",
  "defaults": {
    "verify_facets": ["exact"]
  },
  "components": {
    "auth-service": {
      "path": "services/auth",
      "version_source": null,
      "boundary": {"provider": "implicit", "paths": []}
    }
  },
  "slices": {
    "auth-source": {
      "mode": "exact",
      "components": ["auth-service"]
    }
  }
}
```

```bash
boundver validate-config
boundver generate --source working-tree
git add boundary.config.json boundary.lock.json
git commit -m "chore: record auth-service contract baseline"
boundver verify --source head --facets exact
```

At this stage, any tracked content, executable-bit, or regular-file/symlink
identity change below `services/auth` rotates the exact digest. `implicit`
intentionally produces no separate boundary digest, so do not use it in a
boundary slice.

The optional boundary and component `note` fields are human-facing annotations
and do not rotate the semantic configuration digest. The same is true of a
component's optional `ecosystem` classification.

## Stage 2: declare the public artifact

Replace `implicit` once you know which files consumers actually observe:

```json
{
  "components": {
    "auth-service": {
      "path": "services/auth",
      "version_source": null,
      "boundary": {
        "provider": "openapi-canonical",
        "paths": ["openapi/**/*.yaml"]
      }
    }
  }
}
```

Change or add a boundary slice:

```json
{
  "slices": {
    "auth-api": {
      "description": "Auth service public API",
      "mode": "boundary",
      "components": ["auth-service"]
    }
  }
}
```

The selector grammar is conventional and case-sensitive:

- `openapi/*.yaml` selects direct children only.
- `openapi/**/*.yaml` selects direct children and all deeper descendants.
- `**/*.yaml` also includes YAML files at the component root.

Every declaration must match at least one file. Raw and canonical providers use
the same matcher. Choose a raw provider when every byte-level artifact change is
significant; choose a canonical provider when its documented normalization is
appropriate. Neither option proves backward compatibility.

## Stage 3: classify behavioral contracts

Add defaults, configuration, migrations, route policy, or contract tests that
can change observable behavior without changing the public shape:

```json
{
  "components": {
    "auth-service": {
      "path": "services/auth",
      "version_source": null,
      "boundary": {
        "provider": "openapi-canonical",
        "paths": ["openapi/**/*.yaml"]
      },
      "behavior": {
        "paths": [
          "openapi/**/*.yaml",
          "config/**/*.json",
          "migrations/**/*.sql",
          "middleware.py"
        ]
      }
    }
  }
}
```

```json
{
  "slices": {
    "auth-behavior": {
      "mode": "behavior",
      "components": ["auth-service"]
    }
  }
}
```

In v3, the behavior digest is an envelope containing the declared behavior
digest and the boundary digest. A boundary change therefore always rotates a
configured behavior fingerprint, even if a selector was accidentally omitted.
Still include the boundary patterns in `behavior.paths`: it keeps diagnostics
and the intended input set readable.

## Stage 4: add version-family tracking

For a manifest inside the component:

```json
{"version_source": {"file": "package.json", "field": "version"}}
```

Or for reachable Git tags:

```json
{"version_source": {"git_tag_prefix": "auth-service-v"}}
```

With the default major compatibility mode, `compat` rotates when the SemVer
major family changes. `semver_major_minor` is available when the repository's
policy treats a minor family as the coordination boundary. This is a declaration
signal, not an inference that the code is compatible.

A component without `version_source` has no compatibility fingerprint. The
implicit fallback policy simply gates the facets that are available, but an
explicit CLI, component, or default policy that selects `compat` fails with
usage exit `2`. Either declare a file or `git_tag_prefix` source, or exclude
`compat` in that component's `verify_facets`. Constant and sibling-component
version identities remain possible future extensions; boundver does not invent
one for an unversioned component.

Tag lookup is evaluated against the commit captured for the operation. A tag on
an unreachable orphan history cannot become the component version. Prefixes
are literal Git tag prefixes rather than wildcard patterns; the exact grammar
and shallow-clone behavior are documented in
[reference](reference.md#what-each-facet-needs).

## Stage 5: declare the consumer graph

Add configured downstream components as graph edges, and outside systems as
typed terminals:

```json
{
  "consumers": ["login-web"],
  "external_consumers": ["external-audit-service"]
}
```

This assumes `login-web` has its own entry in `components`; add every internal
consumer as a component before declaring the edge.

Every `consumers` value must name another configured component, so typos fail
validation instead of silently shrinking impact. `external_consumers` values
are unique, non-empty opaque labels and cannot alias configured components.
boundver reports direct impact for boundary and compatibility drift. Use
`verify --transitive` or `why --transitive` to follow the declared internal
edges and collect external terminals at every reached component. Traversal is
cycle-safe; graph discovery is not automatic.

## Stage 6: expand component coverage safely

Preview manifest-based suggestions:

```bash
boundver discover
```

Root manifests need one unambiguous tracked package directory or conventional
`src`, `lib`, or `app` directory. If none exists, discovery leaves the project
for manual configuration instead of inventing a root component.

Add a component manually or with `boundver add`, then update an existing v3
lock with a focused command:

```bash
boundver add billing-service services/billing --provider implicit
boundver validate-config
boundver generate --components billing-service --source working-tree
```

Component-scoped generation is safe only with an existing valid v3 lock. It
recomputes the complete candidate state, refuses to preserve any stale
unselected component, replaces the selected entry as one unit, reconciles
component removals, and recomputes all slices. Run a full `boundver generate`
for the first baseline or after a broad semantic configuration change.

## Stage 7: tighten CI policy

Start with consumer-facing policy on components that provide those facets, and
give pathless implicit, leaf, or unversioned components a meaningful override:

```json
{
  "defaults": {"verify_facets": ["boundary", "compat"]},
  "components": {
    "auth-service": {
      "path": "services/auth",
      "version_source": {"file": "package.json", "field": "version"},
      "boundary": {
        "provider": "openapi-canonical",
        "paths": ["openapi/**/*.yaml"]
      }
    },
    "docs-site": {
      "path": "apps/docs",
      "version_source": null,
      "boundary": {"provider": "leaf", "paths": []},
      "verify_facets": ["exact"]
    }
  }
}
```

Promote behavior or exact only after the observation stream is useful rather
than noisy:

```bash
boundver verify --source head
boundver verify --source head --components auth-service \
  --facets behavior,boundary,compat
```

Precedence is CLI `--facets`, component `verify_facets`, then defaults. An
explicit CLI policy intentionally replaces all component-specific choices.

Drift outside the selected gate remains an observation. Configuration,
provider-metadata, malformed-lock, and digest errors are safety failures
regardless of facet policy.

When a change is accepted locally:

```bash
boundver verify --source working-tree --update
git diff -- boundary.lock.json
```

`--facets` controls gating and reporting. The update still writes all facets for
each regenerated component; it never preserves an old exact or behavior field
merely because that field was non-gating.

## Stage 8: use slices as stable integration keys

A slice combines one facet across an explicit component set, or across a
declared consumer closure:

```json
{
  "slices": {
    "auth-impact": {"mode": "exact", "closure_of": "auth-service"}
  }
}
```

`closure_of` resolves to the seed and every transitive downstream configured
component. Exactly one of `components` or `closure_of` is allowed. The resolved,
sorted membership is persisted in the lock, so graph changes are reviewable.
Use a mode every resolved member provides—usually `exact` for a mixed closure.
Strict configuration validation checks the resolved membership before
generation, including leaf, pathless implicit, unversioned, and behavior-free
members.
The fingerprint can key caches or downstream jobs without rotating for
unrelated components:

```bash
python - <<'PY'
import json

with open("boundary.lock.json", encoding="utf-8") as handle:
    lock = json.load(handle)
print(lock["slices"]["auth-api"]["fingerprint"])
PY
```

Treat that value as a deterministic change signal, not as a replacement for
consumer tests when the signal rotates.

## Upgrading

Every upgrade regenerates the lock from reviewed source; hash-bearing locks are
never relabelled. The procedure is in [reference](reference.md#upgrading), and
what the regeneration diff should show on each upgrade path is in
[migration and ratcheting](migration-and-ratcheting.md).

## Common mistakes

### Using the repository root as a component

The lock and config normally live at the root, creating self-reference. Choose
a non-root source directory. Discovery is deliberately conservative here.

### Assuming `*` is recursive

It is not. Use a whole `**` segment for recursive matching, and remember that it
also matches zero directory levels.

### Forgetting Git tracking

After the first commit, untracked files are excluded. Stage new files before an
index baseline and make them Git-known before a working-tree baseline.

### Mixing source modes

Generate and verify against the same snapshot. For `index` and `head` that
snapshot supplies the config and the lock as well, which is stricter than 0.10.
See [reference](reference.md#source-modes).

### Assuming `--allow-partial` hides extraction failures

It only allows intentional null slice inputs, such as a boundary slice that
temporarily includes an `implicit` component. Missing declared paths, provider
errors, broken version sources, and vendored-copy errors remain fatal. See
[reference](reference.md#-allow-partial).

### Fingerprinting a stale generated artifact

Boundver does not know that an OpenAPI file, GraphQL schema, or descriptor was
generated from something else, so a stale committed artifact verifies clean.
Give the generator a deterministic `--check` mode and run it before
`boundver verify`. See
[reference](reference.md#generated-artifacts-are-not-bound-to-their-generator).

### Treating canonical output as compatibility proof

Canonical providers remove only their documented noise. Run consumer,
schema-evolution, and integration tests after relevant drift.

## Rollback

boundver is repository-local. To stop using it, remove the config, lock, and CI
or hook entry. It does not mutate source files or external consumers.
