# Implementation Plan: boundver as a Public Tool

This plan is for shipping boundver as a public tool.

Immediate execution focus: fix correctness gaps (validation/fallback/source selection) and ensure default usage works without internal or proprietary artifacts.

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

## Phase 2: Near-term correctness & portability (high priority)

- [ ] Add `boundver validate-config` command with strict failures for:
  - unknown slice components
  - unknown slice modes
  - unsupported `defaults.compat_mode`
  - empty `paths` where a provider requires source artifacts
  - configured boundary path missing on disk
  - API slices containing components/boundaries with no API fingerprint
- [ ] Remove silent fallback from `api`/`compat` slice selection to `exact`.
- [ ] Add explicit fingerprint source selection and document defaults:
  - `--source=head`
  - `--source=index`
  - `--source=working-tree`
- [ ] Ensure `compat_mode` behavior matches config and docs.
- [ ] Add a clear error/warning model (`error`, `partial`, `ok`) for boundary extraction status.

### Public portability requirement (non-proprietary baseline)

- [ ] Ensure default CLI/examples/docs do **not** assume internal HSL/TechScout artifacts.
- [ ] Treat `service-definition` and other organization-specific contracts as optional adapters/providers.
- [ ] Ship public examples that rely only on accessible artifacts (OpenAPI, Python exports, TypeScript exports, JSON Schema, etc.).
- [ ] If a config references unavailable proprietary boundary sources, fail with actionable guidance instead of implicit fallback.

## Phase 3: Testing

- [ ] Unit tests for each module (canonical JSON, sha256, semver parsing, TOML/YAML extraction)
- [ ] Integration tests using temporary git repos (`git init` + commits in tmp dirs)
- [ ] Snapshot tests for lockfile output (golden file comparison)
- [ ] Edge case tests:
  - Component with no version source
  - Component with missing/unavailable boundary source
  - Component with `implicit` boundary (api fingerprint = null)
  - Empty slices
  - Vendored copy drift detection
  - Non-existent paths in config
  - Git repo with no commits
- [ ] CI pipeline (GitHub Actions): lint, type-check, test on Python 3.8–3.12

## Phase 4: CLI polish

- [ ] Add `boundver init` command — interactive config scaffolding
- [ ] Add `--format json|text|table` output option for all commands
- [ ] Add `--quiet` / `--verbose` flags
- [ ] Add `--exit-code` option for `verify` (already exits 1 on mismatch — document it)
- [ ] Color output in TTY mode (red for breaking, yellow for API changes, green for unchanged)
- [ ] Add shell completions (bash, zsh, fish)
- [ ] Add `boundver check-config` alias to `validate-config` for discoverability

## Phase 5: Config schema & validation

- [ ] JSON Schema for `boundary.config.json` — publish alongside the tool
- [ ] Validate config on load with clear error messages:
  - Missing required fields
  - Unknown component references in slices
  - Duplicate component paths
  - Invalid/unknown boundary provider names
- [ ] Provide schema for editor autocompletion (VS Code, JetBrains)
- [ ] Add `$schema` field support in config files

## Phase 6: Documentation

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
- [ ] Add a dedicated "public vs proprietary providers" doc with examples.

## Phase 7: Publishing & distribution

- [ ] Register `boundver` on PyPI
- [ ] Set up automated release workflow:
  - Tag-triggered: push `v1.0.0` tag → build + publish to PyPI
  - GitHub Release with auto-generated notes
- [ ] Provide standalone single-file download option (for teams that don't want pip)
- [ ] Docker image for CI environments without Python
- [ ] Homebrew formula (stretch goal)

## Phase 8: Features for v1.0+

- [ ] `boundver watch` — file watcher that regenerates on save (dev experience)
- [ ] Config includes/extends — split large configs across files
- [ ] Glob patterns in boundary sources
- [ ] Pre-commit hook integration (`pre-commit-hooks.yaml`)
- [ ] `boundver why <component>` — show which slices include a component and what would change
- [ ] GitHub Action wrapper (`uses: yzm1/boundver-action@v1`)
- [ ] Support `boundary.config.yaml` and `boundary.config.toml` as alternatives to JSON

---

## Next increment (concrete execution order)

1. Implement `validate-config` and strict error cases listed in Phase 2.
2. Remove `api`/`compat` silent fallback and enforce explicit digest selection.
3. Add and document `--source=head|index|working-tree`, then align behavior/tests.
4. Add packaging skeleton (`pyproject.toml`, console entrypoint) so external users can install/run it.
5. Add tests for the new strict behavior and portability constraints.
