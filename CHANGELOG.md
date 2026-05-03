# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog.

## [Unreleased]

### Added
- Behavior tier: fourth fingerprint (`behavior`) forming containment hierarchy `exact ⊇ behavior ⊇ boundary`; hashes user-declared behavioral-contract files; `behavior` slice mode; advisory warning when `behavior.paths` is not a superset of `boundary.paths`.
- Provider architecture Phases 1–3: `BoundaryProvider` protocol with `resolve()`, `validate_config()`, `explain_diff()`; `JsonCanonicalProvider` (RFC 8785); `OpenApiCanonicalProvider` (strips non-contract content).
- Custom provider loading: `--allow-custom-providers` flag; `custom.*` namespace enforcement; module/class validation.
- `boundver why <component>`: shows which facets drifted, change classification, modified files.
- `boundver discover`: detects npm/pnpm workspaces, pyproject.toml, Cargo.toml, go.mod.
- `boundver init --discover`: auto-generates config from discovered components.
- Shell completions (bash/zsh/fish) via `boundver completions` subcommand.
- Glob patterns in boundary source paths (`*`, `?`, `[`).
- Config format support: `boundary.config.yaml` and `boundary.config.toml` alongside JSON.
- Config includes/extends design (not yet implemented).
- Batch git reads: `git cat-file --batch` for O(1) subprocess count.
- Standalone `.pyz` build via `scripts/build_standalone.py`.
- Docker image for CI without Python.
- GitHub Action (`action.yml`) for marketplace.
- Pre-commit hooks: `boundver-verify` and `boundver-generate`.
- Lockfile migration: `boundver migrate-lock [--dry-run]`.
- JSON schemas for CLI outputs (`spec/cli-output.*.schema.json`).
- `--fail-fast` flag on `verify`.
- Color TTY output with automatic suppression for piped/JSON output.

### Fixed
- Source-purity: all three modes (head/index/working-tree) are fully source-pure for enumeration and content reading.
- Binary blob reads: `_git_cat_blob` and `_git_batch_cat` use bytes mode (no text-mode CRLF conversion).
- Version extraction is source-aware via `_SourceAccessor.version_read_file`.
- Option injection guards on git subprocess calls.
- Size guardrails on file reads (10 MiB hash, 50 MiB blob).
- Path traversal prevention with `_is_within` checks.
- Structured exception hierarchy replacing broad `except Exception`.

### Changed
- Exit code semantics: 1 = drift detected, 2 = usage/input error (config, missing files).
- `generated_at` removed from lockfiles; deterministic output always.
- `--json` flag replaced with `--format json|text`.

## [0.9.1] - 2026-05-03

### Fixed
- TOML regex fallback: anchor end-of-line to reject invalid TOML on Python 3.8–3.10 (no built-in `tomllib`).
- Symlink hash parity: working-tree accessor now reads `os.readlink()` for symlinks, matching git blob storage.
- Python 3.8 type annotation compatibility in test helpers.
- CI: use `source=head` in examples test to avoid CRLF/LF hash mismatch across platforms.

### Added
- Boundary extraction status model (`ok` / `partial` / `error`) per component in generated lockfiles.
- Basic project governance docs: `LICENSE`, `CONTRIBUTING.md`.
- Tests for boundary extraction status behavior.
