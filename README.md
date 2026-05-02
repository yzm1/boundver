# boundver

**Semantic version fingerprints for multi-component repositories.**

boundver answers three questions per component — *did the implementation change?*, *did the declared boundary change?*, *is it still compatible?* — using content-addressed fingerprints derived from Git state and declared boundary files. No external dependencies. No build system required.

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
| `exact` | Did anything change? | Canonical SHA-256 digest of component file paths + file bytes |
| `boundary` | Did the declared boundary files change? | Hash of only the declared boundary files (e.g. `openapi.yaml`, `__init__.py`) |
| `compat` | Is it still compatible? | Derived from SemVer major version |

Components are grouped into **slices** — named subsets with their own stable fingerprints. Adding an unrelated component changes the full-project hash but leaves existing slice fingerprints untouched.

> **Important:** `boundary` is a **declared-boundary file fingerprint**, not a semantic API diff. Formatting, ordering, or comment-only edits in boundary artifacts can change it.
>

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

# Deterministic output (omits generated_at)
boundver generate --deterministic

# Preview generation without writing boundary.lock.json
boundver generate --dry-run

# Check current status
boundver status

# Verify lockfile matches repo state
boundver verify

# JSON output for automation
boundver verify --json

# Logging controls
boundver --quiet status
boundver --verbose verify

# Diff two lockfiles
boundver diff old.lock.json boundary.lock.json

# Inspect a specific slice
boundver slice auth-api
```

## Behavior matrix

| Event | exact | boundary | compat |
|---|---|---|---|
| Bug fix (no API change) | ✓ changes | unchanged | unchanged |
| New API endpoint added | ✓ changes | ✓ changes | unchanged |
| Breaking change + major bump | ✓ changes | ✓ changes | ✓ changes |
| Internal refactor | ✓ changes | unchanged | unchanged |
| New unrelated component added | slice unchanged | slice unchanged | n/a |

## Config reference

### `boundary.config.json`

Schema file: `boundary.config.schema.json` (Draft 2020-12).

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
      "vendored_copies": ["path/to/vendored/copy/"]
    }
  },
  "slices": {
    "slice-name": {
      "description": "Human-readable purpose",
      "mode": "exact | boundary | compat",
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

## Validation dependencies

- **Runtime dependencies:** none (stdlib + git only).
- **Optional enhanced schema validation:** install `jsonschema` for stricter JSON Schema engine checks in `validate-config`.
- **Optional enhanced YAML extraction:** install `PyYAML` for robust YAML parsing in version extraction.

```bash
pip install "boundver[schema]"
pip install "boundver[yaml]"
```

Without `jsonschema`, boundver still runs and applies built-in semantic validation checks.

## Ignore behavior for `--source=working-tree`

For working-tree hashing, boundver currently uses a **built-in ignore list** (this is not `.gitignore`-aware yet):

- dot-prefixed names (e.g. `.cache`, `.venv`)
- `__pycache__`
- `node_modules`
- `*.pyc`
- `dist`
- `build`

For `--source=head` and `--source=index`, content and path enumeration are Git-backed and therefore based on Git object state rather than local traversal ignores.

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
