# boundver

**Semantic version fingerprints for multi-component repositories.**

boundver answers three questions per component — *did the implementation change?*, *did the public API change?*, *is it still compatible?* — using content-addressed fingerprints derived from Git and declared boundary files. No external dependencies. No build system required.

## Why

In a repository with many components and layered dependencies, you need different answers at different times:

- **CI** needs to know: "did this component actually change, or can I skip the build?"
- **Consumers** need to know: "did the API I depend on change?"
- **Operators** need to know: "is this still compatible with what's deployed?"

Traditional version managers require humans to answer these questions via commit messages or changelogs. boundver derives the answers from repo state — deterministically, automatically, and with no runtime dependencies beyond Git and Python 3.8+.

## How it works

Each component gets three fingerprints:

| Fingerprint | Question it answers | What it hashes |
|---|---|---|
| `exact` | Did anything change? | Git tree hash of the entire component directory |
| `api` | Did the public boundary change? | Hash of only the declared boundary files (e.g. `openapi.yaml`, `__init__.py`) |
| `compat` | Is it still compatible? | Derived from SemVer major version |

Components are grouped into **slices** — named subsets with their own stable fingerprints. Adding an unrelated component changes the full-project hash but leaves existing slice fingerprints untouched.

## Quick start

```bash
# Install (single file, no dependencies)
curl -O https://raw.githubusercontent.com/yzm1/boundver/main/boundary_lock.py
chmod +x boundary_lock.py

# Create a config (see Config Reference below)
cat > boundary.config.json << 'EOF'
{
  "project": "my-project",
  "components": {
    "auth-service": {
      "path": "services/auth",
      "version_source": { "file": "package.json", "field": "version" },
      "boundary": {
        "kind": "openapi",
        "paths": ["openapi.yaml"]
      }
    }
  },
  "slices": {
    "auth-api": {
      "description": "Auth service public API",
      "mode": "api",
      "components": ["auth-service"]
    }
  }
}
EOF

# Generate the lockfile
python boundary_lock.py generate

# Check current status
python boundary_lock.py status

# Verify lockfile matches repo state
python boundary_lock.py verify

# Diff two lockfiles
python boundary_lock.py diff old.lock.json boundary.lock.json

# Inspect a specific slice
python boundary_lock.py slice auth-api
```

## Behavior matrix

| Event | exact | api | compat |
|---|---|---|---|
| Bug fix (no API change) | ✓ changes | unchanged | unchanged |
| New API endpoint added | ✓ changes | ✓ changes | unchanged |
| Breaking change + major bump | ✓ changes | ✓ changes | ✓ changes |
| Internal refactor | ✓ changes | unchanged | unchanged |
| New unrelated component added | slice unchanged | slice unchanged | n/a |

## Config reference

### `boundary.config.json`

```json
{
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
        "kind": "openapi | python-exports | typescript-exports | leaf | implicit",
        "paths": ["openapi.yaml"],
        "note": "optional explanation"
      },
      "vendored_copies": ["path/to/vendored/copy/"]
    }
  },
  "slices": {
    "slice-name": {
      "description": "Human-readable purpose",
      "mode": "exact | api | compat",
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

### Boundary kinds

| Kind | Meaning |
|---|---|
| `openapi` | OpenAPI/Swagger spec defines the API surface |
| `python-exports` | `__init__.py` or `__all__` exports define the boundary |
| `typescript-exports` | `.d.ts` or `index.ts` exports define the boundary |
| `service-definition` | A service definition file (JSON/YAML) defines the contract |
| `sam-routes` | AWS SAM template route definitions |
| `leaf` | No downstream consumers — boundary is the component itself |
| `implicit` | No explicit boundary artifact yet (API fingerprint will be `null`) |

## CI integration

### GitHub Actions — PR verification

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
      - run: python boundary_lock.py verify
      - name: Show diff on failure
        if: failure()
        run: |
          python boundary_lock.py generate --out boundary.lock.new.json
          python boundary_lock.py diff boundary.lock.json boundary.lock.new.json
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

## Design decisions

- **No external dependencies.** Only Git and Python stdlib. Runs anywhere Python 3.8+ and Git are available.
- **Deterministic output.** Canonical JSON (sorted keys, compact separators) ensures two machines produce identical hashes from identical repo state.
- **Git-native exact hashing.** Uses `git rev-parse HEAD:<path>` — fast, built-in, changes exactly when files change.
- **Config/lockfile split.** Config is human-maintained (what exists). Lockfile is machine-generated (current state). Mirrors `package.json` / `package-lock.json`.
- **Language-agnostic boundaries.** Instead of parsing ASTs, you declare which files constitute the public boundary. Works with any language or artifact format.

## Requirements

- Python 3.8+
- Git
- No pip packages needed

## License

MIT
