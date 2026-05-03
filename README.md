# boundver

[![PyPI](https://img.shields.io/pypi/v/boundver)](https://pypi.org/project/boundver/)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://pypi.org/project/boundver/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![No runtime dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](pyproject.toml)

**Automated change-type classification for components that lack static verification.**

boundver answers four questions per component — *did anything change?*, *did the behavioral contract change?*, *did the declared boundary change?*, *is it still compatible?* — using content-addressed fingerprints derived from Git state and declared boundary files. No external dependencies. No build system required.

## Why

When a component has consumers but no compiler or type system verifying its interface — services exposing OpenAPI specs, Python libraries, config-driven systems, internal platforms — there's no machine that tells you whether a change is internal, boundary-affecting, or breaking.

boundver fills that gap. It lets you **declare** what constitutes your component's boundary, then **automatically classifies every change** into one of four categories:

- **Implementation-only** — internals changed, boundary stable, consumers unaffected.
- **Behavioral contract change** — defaults/config/migrations changed, API shape stable, consumers may need to re-verify.
- **Boundary change** — the declared contract changed, consumers should re-verify.
- **Compatibility break** — the compatibility family changed, deployment coordination required.

This is the information that CI, consumers, and operators each need — derived deterministically from repo state, not from human discipline or commit-message conventions.

### When to use boundver

boundver is for any component whose boundary has consumers but **no static verification** — no compiler checking signatures, no type system enforcing contracts. That includes most services, most Python/Go libraries, most YAML/JSON-defined APIs, and most internal platforms.

| Tool | Sweet spot | Skip if… |
|---|---|---|
| **Nx / Turborepo** | JS/TS monorepos with task graphs and caching | You have a polyglot repo or can't adopt a full task runner |
| **Bazel / Pants** | Large-scale build + dependency graph orchestration | Adoption cost exceeds value for your team size |
| **TypeScript / Rust compiler** | Statically verified API contracts within a single language | Your entire stack is one statically-typed language |
| **boundver** | Any language — automated change classification where no static verifier exists | You already have affected-graph + cache-key tooling that satisfies all four questions |

For full tool-selection guidance, see [docs/WHY_BOUNDVER.md](docs/WHY_BOUNDVER.md).

## How it works

Each component gets four fingerprints forming a strict containment hierarchy (`exact ⊇ behavior ⊇ boundary`):

| Fingerprint | Question it answers | What it hashes |
|---|---|---|
| `exact` | Did anything change? | All tracked files in the component path |
| `behavior` | Did the behavioral contract change? | Declared contract files: boundary + config + migrations + contract tests |
| `boundary` | Did the API surface shape change? | Only the declared boundary files (e.g. `openapi.yaml`, `__init__.py`) |
| `compat` | Is it still in the same compatibility family? | Derived from SemVer major version |

This gives you four distinct change classifications:

| What changed | Meaning |
|---|---|
| Only `exact` | Pure internal refactor — consumers unaffected |
| `exact` + `behavior` | Behavioral contract changed (defaults, config, migrations) — API shape stable but consumers may be affected |
| `exact` + `behavior` + `boundary` | API surface changed — consumers must re-verify |
| All four | Breaking change — compatibility family changed |

Components are grouped into **slices** — named subsets with their own stable fingerprints. Adding an unrelated component changes the full-project hash but leaves existing slice fingerprints untouched.

> **Note:** `boundary` and `behavior` are **declared-file fingerprints**, not semantic analysis. They detect changes in files you declare as contract-relevant. The `openapi-canonical` and `json-canonical` providers go further — they strip non-contract content (descriptions, comments, formatting) so only structural changes trigger the fingerprint.

Each component also reports `boundary_status` in lock output:
- `ok`: boundary paths were declared and hashed successfully
- `partial`: boundary provider is `implicit` and no boundary paths are declared (API fingerprint is `null`)
- `error`: explicit boundary provider has no paths, or declared paths produced no API digest

## Quick start

```bash
# Install
pip install boundver

# Create a starter config
boundver init
# Or auto-discover components from common manifests
boundver init --discover
# Custom path / overwrite existing
boundver init --out boundary.config.json --force

# Or create manually (see Config Reference below)
cat > boundary.config.json << 'EOF'
{
  "project": "my-project",
  "components": {
    "auth-service": {
      "path": "services/auth",
      "version_source": { "file": "package.json", "field": "version" },
      "boundary": {
        "provider": "openapi",
        "paths": ["openapi.yaml"]
      },
      "behavior": {
        "paths": ["openapi.yaml", "config/defaults.json"]
      }
    }
  },
  "slices": {
    "auth-api": {
      "description": "Auth service public API",
      "mode": "boundary",
      "components": ["auth-service"]
    }
  }
}
EOF

# Generate the lockfile
boundver generate

# Regenerate only selected components (and affected slices)
boundver generate --components auth-service,billing-service

# Preview generation without writing boundary.lock.json
boundver generate --dry-run

# Check current status
boundver status

# Verify lockfile matches repo state
boundver verify

# Verify only selected components
boundver verify --components auth-service,billing-service

# Verify only components changed since main
boundver verify --changed-from origin/main

# JSON output for automation
boundver verify --format json

# Logging controls
boundver --quiet status
boundver --verbose verify

# Diff two lockfiles
boundver diff old.lock.json boundary.lock.json

# Inspect a specific slice
boundver slice auth-api

# Preview discovered components
boundver discover --format json
```

## Behavior matrix

| Event | exact | behavior | boundary | compat |
|---|---|---|---|---|
| Bug fix (no API change) | ✓ changes | unchanged | unchanged | unchanged |
| Config/default/migration change | ✓ changes | ✓ changes | unchanged | unchanged |
| New API endpoint added | ✓ changes | ✓ changes | ✓ changes | unchanged |
| Breaking change + major bump | ✓ changes | ✓ changes | ✓ changes | ✓ changes |
| Internal refactor | ✓ changes | unchanged | unchanged | unchanged |
| New unrelated component added | slice unchanged | slice unchanged | slice unchanged | n/a |

## Config reference

### `boundary.config.json`

Schema file: `boundary.config.schema.json` (Draft 2020-12).

> **Config format:** boundver accepts `.json`, `.yaml`/`.yml`, and `.toml` config files.
> When no explicit `--config` is given, it probes `boundary.config.json`, then
> `boundary.config.yaml` / `.yml` / `.toml` in order.

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
  "project": "my-project",
  "defaults": {
    "compat_mode": "major"
  },
  "components": {
    "component-name": {
      "path": "relative/path/from/repo/root",
      "ecosystem": "python | typescript | cloudformation",
      "version_source": {
        "file": "package.json",
        "field": "version"
      },
      "boundary": {
        "provider": "openapi | python-exports | typescript-exports | leaf | implicit",
        "paths": ["openapi.yaml"],
        "note": "optional explanation"
      },
      "behavior": {
        "paths": ["openapi.yaml", "config/defaults.json"]
      },
      "vendored_copies": ["path/to/vendored/copy/"]
    }
  },
  "slices": {
    "slice-name": {
      "description": "Human-readable purpose",
      "mode": "exact | behavior | boundary | compat",
      "components": ["component-a", "component-b"]
    }
  }
}
```

### Version source options

```json
// From a JSON/TOML/YAML file field:
"version_source": { "file": "pyproject.toml", "field": "project.version" }

// From git tags:
"version_source": { "git_tag_prefix": "auth-service-v" }

// No version tracking:
"version_source": null
```

### Boundary providers

| Provider | Meaning |
|---|---|
| `openapi` | OpenAPI/Swagger spec defines the API surface |
| `python-exports` | `__init__.py` or `__all__` exports define the boundary |
| `typescript-exports` | `.d.ts` or `index.ts` exports define the boundary |
| `json-file` | Generic JSON boundary artifact defines the contract |
| `custom.example.service-definition.v1` | Example custom provider namespace |
| `leaf` | No downstream consumers — boundary is the component itself |
| `implicit` | No explicit boundary artifact yet (`boundary` fingerprint will be `null`) |

### Provider capability matrix

| Provider | Semantic parser? | Requires `paths` | Empty `paths` allowed | Output |
|---|---:|---:|---:|---|
| `openapi` | No (raw file digest) | Yes | No | Raw boundary digest |
| `python-exports` | No (raw file digest) | Yes | No | Raw boundary digest |
| `typescript-exports` | No (raw file digest) | Yes | No | Raw boundary digest |
| `json-file` | No (raw file digest) | Yes | No | Raw boundary digest |
| `leaf` | n/a | No | Yes | No boundary digest required |
| `implicit` | n/a | No | Yes | `boundary_status=partial` |
| `custom.*` | Depends on implementation | Usually | Depends | Raw digest by default |

> Built-in providers are currently raw-boundary artifact hashers, not semantic API diff engines.


## Near-term implementation focus

boundver remains a public, language-agnostic tool. Near-term work is focused on:

- strict config validation and no silent fingerprint fallback
- explicit source mode behavior (`head`, `index`, `working-tree`)
- portability for external users (no implicit dependency on internal/proprietary boundary artifacts)

Short term deliverables: `validate-config`, strict digest selection, explicit source modes, and public examples that avoid proprietary dependencies.

## CI integration

For lockfile merge conflict handling, see [docs/LOCKFILE_MERGE.md](docs/LOCKFILE_MERGE.md).

### GitHub Actions — PR verification

For a full set of patterns (conditional builds, cache keys, GitLab, pre-commit), see [docs/ci-cookbook.md](docs/ci-cookbook.md).

#### Option A: use bundled composite action

```yaml
name: Boundary check
on: [pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: ./.github/actions/boundver
        with:
          config: boundary.config.json
          lock: boundary.lock.json
          source: head
          show-diff-on-failure: "true"
```

#### Option B: explicit steps

```yaml
name: Boundary check
on: [pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: boundver verify
      - name: Show diff on failure
        if: failure()
        run: |
          boundver generate --out boundary.lock.new.json
          boundver diff boundary.lock.json boundary.lock.new.json
```

### Conditional builds using slice fingerprints

```bash
# Only rebuild if the API slice actually changed
NEW_FP=$(python -c "
import json
lock = json.load(open('boundary.lock.json'))
print(lock['slices']['my-api']['fingerprint'][:12])
")

if [ "$NEW_FP" != "$CACHED_FP" ]; then
  echo "API changed — rebuilding consumers"
  # ... trigger downstream builds
fi
```

### Shell verifier (portability proof)

```bash
# Verifies exact/boundary fingerprints against HEAD using git + jq + sha256sum
scripts/boundver-verify.sh boundary.config.json boundary.lock.json
```

## Environment variables

| Variable | Effect |
|----------|--------|
| `BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1` | Equivalent to passing `--allow-custom-providers` on every invocation. Accepts `1`, `true`, or `yes`. |

## Exit codes

`boundver verify` uses structured exit codes for reliable CI scripting:

| Code | Meaning |
|------|---------|
| `0` | Lockfile matches current repo state |
| `1` | Lockfile is out of date (fingerprint mismatches found) |
| `2` | Usage error (unknown component, config missing, etc.) |

`validate-config` exits `0` on success, `1` on validation errors.
`generate` exits `0` on success, `1` on config/generation error.

## Design decisions

- **No external dependencies.** Only Git and Python stdlib. Runs anywhere Python 3.8+ and Git are available.
- **Deterministic output.** Canonical JSON (sorted keys, compact separators) ensures two machines produce identical hashes from identical repo state.
- **Canonical exact hashing across source modes.** `exact` uses one canonical SHA-256 file-content digest model for `head`, `index`, and `working-tree`, enabling direct cross-source comparison.
- **Config/lockfile split.** Config is human-maintained (what exists). Lockfile is machine-generated (current state). Mirrors `package.json` / `package-lock.json`.
- **Language-agnostic boundaries.** Instead of parsing ASTs, you declare which files constitute the public boundary. Works with any language or artifact format.

## Examples

- `examples/openapi/`
- `examples/json-file/`
- `examples/implicit-and-leaf/`
- `examples/python-package/`
- `examples/typescript-package/`

## Documentation

- [Getting started](docs/getting-started.md) — install, first config, first lockfile, CI setup
- [Gradual adoption guide](docs/gradual-adoption.md) — incremental adoption from one component to full coverage
- [CI cookbook](docs/ci-cookbook.md) — GitHub Actions, cache keys, GitLab, pre-commit
- [Why boundver?](docs/WHY_BOUNDVER.md) — tool comparison and positioning
- [Custom vs public providers](docs/public-vs-custom-providers.md) — when to use `custom.*`
- [Lockfile merge handling](docs/LOCKFILE_MERGE.md) — resolving merge conflicts

## Validation dependencies

- **Runtime dependencies:** none (stdlib + git only).
- **Optional enhanced schema validation:** install `jsonschema` for stricter JSON Schema engine checks in `validate-config`.
- **Optional enhanced YAML extraction:** install `PyYAML` for robust YAML parsing in version extraction.

```bash
pip install "boundver[schema]"
pip install "boundver[yaml]"
```

Without `jsonschema`, boundver still runs and applies built-in semantic validation checks.

## Release

- PyPI publish workflow: `.github/workflows/publish.yml`
- Trigger: push a version tag matching `v*` (for example `v0.3.0`)

## Source modes

| Mode | File list | Content read from | Default for |
|------|-----------|-------------------|-------------|
| `head` | `git ls-tree HEAD` | committed git blobs | `generate`, `verify`, `status`, `why` |
| `index` | `git ls-files --cached` | staged blobs | — |
| `working-tree` | `git ls-files` (tracked) | disk bytes (CRLF→LF) | `explain` |

### Important: `working-tree` only sees tracked files

`--source=working-tree` hashes the **on-disk content** of files that are already tracked by git.
It does **not** include untracked files.  If you just created a new file but haven't run
`git add`, that file will not appear in any fingerprint until it is tracked.

This matters most during:

- **Initial setup** — run `git add .` before `boundver generate --source working-tree`.
- **Adding new boundary files** — a new `openapi.yaml` won't affect digests until tracked.
- **CI with uncommitted generated files** — prefer `--source head` (the default) in CI.

### Ignore behavior

`--source=working-tree` prefers Git-backed tracked-file enumeration (`git ls-files`) when available.
In non-git fallback contexts, local file traversal is used.
Symlinks are hashed as link-target text (not dereferenced bytes) for cross-source consistency.

## Requirements

- Python 3.8+
- Git
- No pip packages needed

## Hash guardrails

To avoid pathological repository scans, hashing enforces built-in guardrails:

- maximum files hashed per digest: `50,000`
- maximum size per hashed file: `50 MiB`

If exceeded, boundver records explicit digest errors on affected components.

## License

MIT
