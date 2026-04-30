# Implementation Plan: boundver as a Public Tool

This plan covers what's needed to take boundver from an internal single-file script to a published, installable CLI tool.

---

## Phase 1: Package structure & installability

- [ ] Restructure into a proper Python package:
  ```
  boundver/
  ├── pyproject.toml
  ├── README.md
  ├── LICENSE
  ├── src/
  │   └── boundver/
  │       ├── __init__.py
  │       ├── cli.py          (argparse CLI entry point)
  │       ├── core.py         (generate, verify, diff logic)
  │       ├── git.py          (git helpers)
  │       ├── versions.py     (version extraction)
  │       └── fingerprints.py (hashing utilities)
  └── tests/
      ├── conftest.py
      ├── test_generate.py
      ├── test_verify.py
      ├── test_diff.py
      ├── test_versions.py
      └── fixtures/
          └── (sample repos as git bundles or temp dirs)
  ```
- [ ] Create `pyproject.toml` with:
  - `[project.scripts]` entry: `boundver = "boundver.cli:main"`
  - Minimum Python 3.8
  - Zero runtime dependencies
  - Dev dependencies: pytest, ruff, mypy
- [ ] Add `LICENSE` file (MIT)
- [ ] Support `pipx install boundver` and `pip install boundver`

## Phase 2: Testing

- [ ] Unit tests for each module (canonical JSON, sha256, semver parsing, TOML/YAML extraction)
- [ ] Integration tests using temporary git repos (`git init` + commits in tmp dirs)
- [ ] Snapshot tests for lockfile output (golden file comparison)
- [ ] Edge case tests:
  - Component with no version source
  - Component with `implicit` boundary (api fingerprint = null)
  - Empty slices
  - Vendored copy drift detection
  - Non-existent paths in config
  - Git repo with no commits
- [ ] CI pipeline (GitHub Actions): lint, type-check, test on Python 3.8–3.12

## Phase 3: CLI polish

- [ ] Add `boundver init` command — interactive config scaffolding
- [ ] Add `--format json|text|table` output option for all commands
- [ ] Add `--quiet` / `--verbose` flags
- [ ] Add `--exit-code` option for `verify` (already exits 1 on mismatch — document it)
- [ ] Color output in TTY mode (red for breaking, yellow for API changes, green for unchanged)
- [ ] Add shell completions (bash, zsh, fish)
- [ ] Add `boundver check-config` command for config validation before generation

## Phase 4: Config schema & validation

- [ ] JSON Schema for `boundary.config.json` — publish alongside the tool
- [ ] Validate config on load with clear error messages:
  - Missing required fields
  - Unknown component references in slices
  - Duplicate component paths
  - Invalid boundary kinds
- [ ] Provide schema for editor autocompletion (VS Code, JetBrains)
- [ ] Add `$schema` field support in config files

## Phase 5: Documentation

- [ ] Expand README with:
  - Logo/banner
  - Badges (PyPI version, Python versions, CI status, license)
  - "When to use this" vs alternatives comparison table
- [ ] Create `docs/` site (mkdocs-material or similar):
  - Getting started guide
  - Config reference
  - CI integration cookbook (GitHub Actions, GitLab CI, Jenkins)
  - Conceptual guide: "What are boundary fingerprints?"
  - Migration guide for teams currently using manual versioning
- [ ] Add `CONTRIBUTING.md`
- [ ] Add `CHANGELOG.md` (keep-a-changelog format)

## Phase 6: Publishing & distribution

- [ ] Register `boundver` on PyPI
- [ ] Set up automated release workflow:
  - Tag-triggered: push `v1.0.0` tag → build + publish to PyPI
  - GitHub Release with auto-generated notes
- [ ] Provide standalone single-file download option (for teams that don't want pip):
  - `curl -sSL https://raw.githubusercontent.com/yzm1/boundver/main/boundver.py | python - generate`
- [ ] Docker image for CI environments without Python
- [ ] Homebrew formula (stretch goal)

## Phase 7: Features for v1.0

- [ ] `boundver watch` — file watcher that regenerates on save (dev experience)
- [ ] Config includes/extends — split large configs across files:
  ```json
  { "extends": ["./components/services.json", "./components/libs.json"] }
  ```
- [ ] Glob patterns in `boundary.paths`:
  ```json
  "paths": ["src/**/__init__.py", "!src/**/internal/**"]
  ```
- [ ] Pre-commit hook integration (`pre-commit-hooks.yaml`)
- [ ] `boundver why <component>` — show which slices include a component and what would change
- [ ] GitHub Action wrapper (`uses: yzm1/boundver-action@v1`)
- [ ] Support `boundary.config.yaml` and `boundary.config.toml` as alternatives to JSON

## Phase 8: Ecosystem integrations (post-v1.0)

- [ ] VS Code extension — inline status indicators showing fingerprint state
- [ ] GitHub bot / PR comment — auto-comment with diff summary on PRs that change the lockfile
- [ ] Monorepo framework adapters (Nx, Turborepo, Lerna) — import component topology from existing configs
- [ ] Language-specific boundary analyzers (optional plugins):
  - Python: extract public API from `__all__` / type stubs
  - TypeScript: extract from `.d.ts` generation
  - OpenAPI: validate spec completeness

---

## Priority order for first public release (MVP)

1. **Package structure** — installable via pip/pipx
2. **Tests** — comprehensive enough to accept contributions safely
3. **Config validation** — clear errors for misconfiguration
4. **CI workflow** — lint + test + type-check
5. **PyPI publish** — `pip install boundver` works
6. **README + badges** — discoverable and credible

Everything else can follow in subsequent releases.
