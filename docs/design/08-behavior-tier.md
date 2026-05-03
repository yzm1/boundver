# 08 — Behavior Tier

## Problem

The current three-tier fingerprint model (exact/boundary/compat) has a gap between "exact" and "boundary":

- **exact** catches every file change — but does not distinguish an internal refactor from a behavioral contract change.
- **boundary** only tracks declared surface files (openapi.yaml, `__init__.py`) — so it misses changes to defaults, config, migrations, error behavior, or any semantic change that doesn't alter the API *shape*.

The result: a change that modifies a timeout default, replaces a retry policy, alters a database migration, or changes an error code is reported identically to a harmless refactor — "exact changed, boundary stable." Consumers have no signal that behavior affecting them has changed.

## Solution: `behavior` fingerprint

A fourth fingerprint tier — `behavior` — that hashes a user-declared set of files representing the component's **behavioral contract**: everything that affects observable behavior for consumers, beyond just the API surface shape.

### Containment hierarchy

```
exact ⊇ behavior ⊇ boundary
```

- Every change that affects `boundary` also affects `behavior` and `exact`.
- A change that affects `behavior` but not `boundary` is a **behavioral contract change with stable API shape** — the signal this tier provides.
- A change that affects only `exact` is a pure internal refactor.

### Config

```json
{
  "components": {
    "billing-service": {
      "path": "services/billing",
      "boundary": {
        "provider": "openapi",
        "paths": ["openapi.yaml"]
      },
      "behavior": {
        "paths": [
          "openapi.yaml",
          "config/defaults.json",
          "migrations/",
          "tests/contract/"
        ]
      }
    }
  }
}
```

`behavior.paths` follows the same rules as `boundary.paths`:
- Relative to the component path.
- Supports glob patterns.
- Files are sorted, CRLF-normalized, and hashed identically to boundary paths.

The `behavior.paths` list SHOULD be a superset of `boundary.paths`. If it isn't, a config validation warning is emitted (not an error — users may have reasons).

### Lockfile output

```json
"fingerprints": {
  "exact": "a1b2c3...",
  "behavior": "d4e5f6...",
  "boundary": "g7h8i9...",
  "compat": "j0k1l2..."
}
```

When `behavior` is not configured for a component, the field is `null`.

### Change classification

| exact | behavior | boundary | compat | Classification |
|---|---|---|---|---|
| changed | unchanged | unchanged | unchanged | Pure internal refactor |
| changed | changed | unchanged | unchanged | **Behavioral contract change** (API shape stable) |
| changed | changed | changed | unchanged | Boundary shape change |
| changed | changed | changed | changed | Breaking change |

### Slice support

Slices gain a new mode: `"behavior"`.

```json
"slices": {
  "billing-contract": {
    "mode": "behavior",
    "components": ["billing-service"]
  }
}
```

### What `behavior` paths should include

Guidance for users:

- **Always include boundary paths** — behavior is a superset of surface.
- **Config/defaults files** — `config/defaults.json`, `.env.example`, feature flag definitions.
- **Database migrations** — `migrations/`, `alembic/versions/`.
- **Contract test fixtures** — `tests/contract/`, `tests/fixtures/responses/`.
- **Wire format examples** — sample request/response files used as golden tests.
- **Error code definitions** — error catalogs, status code mappings.

### What `behavior` paths should NOT include

- Implementation files (use `exact` for that).
- Test files that test internals, not contracts.
- Documentation (use `boundary` for doc-linked API files).

## Limitations

The `behavior` tier reduces the gap but does not eliminate it. It detects behavioral changes **only if the change touches a file the user declared in `behavior.paths`**.

Changes that are invisible to all four tiers:

| Change type | Why invisible |
|---|---|
| Transitive dependency behavior change | The component's files are unchanged. Boundver does not model a dependency graph. |
| Environment/infrastructure change | External to the repository. |
| Build toolchain change | Boundver tracks source, not artifacts. |
| Behavioral change in an undeclared file | The user didn't include it in `behavior.paths`. |

These are **conscious scope boundaries**, not bugs. Boundver provides declared-file-based change classification. It does not (and should not) attempt to understand runtime semantics.

For teams that need runtime behavioral verification, see the custom provider approach below.

## Extension: test-output fingerprinting via custom provider

For teams that want to detect behavioral changes that no static file analysis can catch, a custom provider can hash the output of a deterministic test suite:

```json
{
  "providers": [
    { "module": "my_providers", "class": "TestOutputProvider" }
  ],
  "components": {
    "billing-service": {
      "boundary": {
        "provider": "custom.test-output",
        "paths": [],
        "options": {
          "command": "pytest tests/contract/ --snapshot-update && cat tests/contract/snapshots/*.json",
          "timeout": 30
        }
      }
    }
  }
}
```

The custom provider would:
1. Execute the declared command in a subprocess.
2. Capture stdout.
3. Return the output as a single `ResolvedBoundary` entry for hashing.

This crosses from "static file analysis" into "requires running code," which is why it's a custom provider rather than a built-in. The core stays fast and side-effect-free; teams that need runtime verification opt in explicitly.

### Provider implementation sketch

```python
class TestOutputProvider:
    name = "custom.test-output"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        command = ctx.boundary_cfg.get("options", {}).get("command")
        timeout = ctx.boundary_cfg.get("options", {}).get("timeout", 30)
        if not command:
            return ResolvedBoundary(status="error", errors=["No command specified"])
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, timeout=timeout,
                cwd=str(ctx.repo_root / ctx.component_path),
            )
        except subprocess.TimeoutExpired:
            return ResolvedBoundary(status="error", errors=["Command timed out"])
        if result.returncode != 0:
            return ResolvedBoundary(
                status="error",
                errors=[f"Command failed (exit {result.returncode}): {result.stderr.decode()[:200]}"],
            )
        return ResolvedBoundary(
            entries=[("test-output:stdout", result.stdout)],
            status="ok",
        )

    def validate_config(self, boundary_cfg, component_path, repo_root):
        if not boundary_cfg.get("options", {}).get("command"):
            return ["custom.test-output requires options.command"]
        return []

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return "test output changed — behavioral contract may have changed"
```

**Security note:** This provider executes arbitrary commands. It requires `--allow-custom-providers` and the `custom.` name prefix, same as any custom provider. It should never be enabled in untrusted contexts.

## Implementation plan

1. Add `behavior` to config schema (`boundary.config.schema.json`).
2. Add `behavior` to lockfile schema (`spec/boundary.lock.schema.json`).
3. Update `_lockfile.py:generate_lockfile()` to compute behavior digest using the same `PathHashProvider` mechanism.
4. Update `_diff.py:_summarize_change()` to recognize the new tier.
5. Update `verify_lockfile()` to check behavior fingerprint.
6. Add `"behavior"` as a valid slice mode.
7. Update `_output.py:why_component()` to report behavior drift.
8. Add tests.
9. Update spec.md.
