# Gradual Adoption Guide

boundver is designed to be adopted incrementally. You don't need to configure every component on day one. This guide describes a practical path from "zero config" to full coverage.

## The core principle

**Start with one slice. Gate one CI job. Expand from there.**

Each step delivers real value. Later steps amplify it.

---

## Stage 1 — One component, one slice, `implicit` provider

**Time: 15 minutes. Risk: none.**

Pick the component that changes most often and has the most downstream consumers. Configure it with the `implicit` provider so you get `exact` fingerprinting right away without needing to declare boundary files.

```json
{
  "project": "my-project",
  "components": {
    "auth-service": {
      "path": "services/auth",
      "boundary": { "provider": "implicit", "paths": [] }
    }
  },
  "slices": {
    "all": {
      "mode": "exact",
      "components": ["auth-service"]
    }
  }
}
```

Generate and commit:

```bash
boundver generate --source working-tree
git add boundary.config.json boundary.lock.json
git commit -m "feat: add boundver with auth-service"
```

Add `boundver verify` to your PR workflow. Done. The lockfile now tracks whether `services/auth` changed between commits.

**What you get at Stage 1:**
- PRs that touch `services/auth` will require a lockfile update.
- PRs that don't touch it pass silently.
- The `exact` fingerprint is stable and cross-platform.

---

## Stage 2 — Declare boundary files, upgrade to `boundary` slice

**Time: 30 minutes. Value: API-change signaling.**

Once you know which files constitute the public contract (OpenAPI spec, `__init__.py`, `.d.ts` index, `schema.json`), upgrade the provider and declare paths:

```json
"auth-service": {
  "path": "services/auth",
  "boundary": {
    "provider": "openapi",
    "paths": ["openapi.yaml"]
  }
}
```

Change the slice mode to `boundary`:

```json
"slices": {
  "auth-api": {
    "description": "Auth service public API",
    "mode": "boundary",
    "components": ["auth-service"]
  }
}
```

Regenerate:

```bash
boundver generate --source working-tree
```

**What you get at Stage 2:**
- Implementation-only changes (bug fixes, refactors) no longer change the `boundary` fingerprint.
- Consumers can track the `boundary` fingerprint to know if the API contract changed — without rebuilding for every `exact` change.
- The `boundary_status` field on each component confirms boundary extraction succeeded.

> **Note:** Built-in providers hash boundary files as raw bytes. Comment-only or whitespace-only changes in boundary files will still change the fingerprint.

---

## Stage 3 — Add version tracking

**Time: 5 minutes per component. Value: SemVer compat signals.**

If your components have version numbers in manifest files, wire them up:

```json
"auth-service": {
  "path": "services/auth",
  "version_source": { "file": "package.json", "field": "version" },
  "boundary": { "provider": "openapi", "paths": ["openapi.yaml"] }
}
```

Or from git tags:

```json
"version_source": { "git_tag_prefix": "auth-service-v" }
```

**What you get at Stage 3:**
- The `compat` fingerprint changes only when the major version changes.
- Consumers can track `compat` fingerprint to know if a breaking change was declared.
- `diff` output shows old/new versions alongside changed fingerprints.

---

## Stage 4 — Expand component coverage

**Time: Incremental. Value: whole-repo visibility.**

Add remaining components. Use `boundver init --discover` to auto-generate stubs, then refine:

```bash
boundver init --discover --force   # overwrites existing config — use carefully
```

Or add components manually to the existing config. You can add a new component and run `boundver generate --components new-component` to regenerate only that component's entry without touching others.

**Tips for multi-component expansion:**
- Keep `implicit` provider for components with no stable API boundary yet.
- Add components to the `all` exact slice first, then promote them to `boundary` slices when appropriate.
- Use `boundver validate-config` after each addition to catch errors early.

---

## Stage 5 — Use slice fingerprints as cache keys

Once you have slices that represent stable deployment or consumer groups, use their fingerprints to drive CI decisions:

```bash
# Only rebuild consumers if the API slice actually changed
SLICE_FP=$(python -c "
import json
lock = json.load(open('boundary.lock.json'))
print(lock['slices']['auth-api']['fingerprint'][:12])
")

if [ "$SLICE_FP" != "$CACHED_SLICE_FP" ]; then
  echo "API changed — triggering downstream builds"
  # rebuild consumers, update cache key, etc.
fi
```

See [CI cookbook](ci-cookbook.md) for more patterns.

---

## Common mistakes to avoid

### Mistake: Declaring boundary paths that don't exist

`validate-config` will catch this, but double-check after moving or renaming boundary files.

```bash
boundver validate-config   # run this before committing
```

### Mistake: Using `boundary` slice mode with `implicit` provider

The `implicit` provider doesn't produce a `boundary` fingerprint. A `boundary` slice requires all its components to have declared boundary paths with an explicit provider. You'll get a validation error that explains exactly which components need upgrading.

### Mistake: Forgetting to regenerate after config changes

If you add a new component but don't regenerate, `verify` will report the new component as missing from the lockfile. Run:

```bash
boundver generate
```

### Mistake: Checking in lockfile with `--source working-tree` in CI

CI should always use `--source head` (the default) to hash committed content. Use `working-tree` locally for fast iteration before committing.

---

## Rollback

boundver is opt-in. To stop using it, delete `boundary.config.json` and `boundary.lock.json` and remove the CI step. No other side effects.
