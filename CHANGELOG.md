# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [0.10.0] - 2026-08-12

### Breaking changes

- Lockfiles now use `boundary-lock/v2` and a length-delimited, domain-separated
  hashing format. This removes ambiguous byte framing in v1.
- **Migration:** v1 fingerprints cannot be converted safely. Regenerate them from
  repository content with `boundver generate` after upgrading. The
  `migrate-lock` command reports this requirement instead of relabeling old
  fingerprints.
- Git-backed source modes enumerate tracked files only. Untracked working-tree
  files no longer enter fingerprints implicitly.

### Added

- Facet-scoped verification with `verify --facets` and
  `defaults.verify_facets`. Drift outside the selected gate is reported as an
  observation instead of failing the gate.
- Severity-aware verification exit codes: `1` for exact or metadata drift, `3`
  for behavior drift, `4` for boundary drift, and `5` for compatibility drift;
  `2` remains reserved for usage or input errors.
- `verify --update` for reviewing drift and refreshing the lockfile in one
  command.
- A `behavior` fingerprint and behavior-mode slices for declared behavioral
  contracts such as defaults, configuration, and migrations.
- Glob patterns in `boundary.paths` and `behavior.paths`; newly tracked matching
  files change the corresponding fingerprint.
- Validated component `consumers` relationships. Boundary and compatibility
  drift now identifies declared downstream consumers.
- Git-aware `discover` and `init --discover` support for npm, Python, Cargo, and
  Go manifests. Discovery uses tracked files, skips duplicate component
  directories, and emits ecosystem-specific version fields.
- Built-in and custom boundary-provider protocols, including JSON canonical and
  OpenAPI canonical providers, provider validation, metadata, and diff
  explanations.
- `why`, `discover`, shell-completion, `validate-config`, `check-config`, and
  `migrate-lock` commands, plus `--fail-fast` verification.
- JSON, YAML, and TOML config loading, with a conditional `tomli` dependency for
  Python 3.9-3.10.
- JSON schemas for configuration, v2 lockfiles, and machine-readable CLI output.
- Distribution options for PyPI, a standalone `.pyz`, Docker, pre-commit, and a
  hardened composite GitHub Action suitable for Marketplace use.
- Public project metadata and community files for security reports, support,
  contributions, issue reports, and pull requests.

### Changed

- Lockfile output is deterministic and no longer includes `generated_at`.
- Machine-readable commands use `--format json`; color is limited to interactive
  text output.
- Partial component generation reconciles removed components and recomputes all
  configured slices, preventing stale aggregate fingerprints.
- Config mutation commands refuse YAML or TOML output instead of silently
  rewriting those files as JSON.
- Strict config validation uses the schema bundled in installed wheels, not only
  a schema found in a source checkout.
- The GitHub Action now accepts structured inputs, installs the tagged action
  source, preserves JSON output, and exposes issues, observations, and the
  severity exit code.
- The supported Python floor is now 3.9. Python 3.8 is upstream-EOL and cannot
  use the Setuptools version required for modern SPDX package metadata.

### Fixed

- Generation now fails when exact, behavior, boundary, or compatibility inputs
  cannot be computed; verification no longer accepts matching null digests.
- Invalid `--changed-from` refs fail closed, and config-file changes select all
  components for verification.
- Hash framing no longer permits different path/content layouts to produce the
  same digest.
- NUL-delimited Git parsing preserves non-ASCII and unusual filenames across
  HEAD, index, status, and diff operations.
- Missing, malformed, truncated, oversized, and non-blob Git objects are
  reported instead of being hashed as empty content.
- OpenAPI canonicalization removes documentation fields only where they are
  annotations; schema properties named `description`, `example`, or `x-*`
  remain contract-significant.
- Verification now checks component metadata, digest errors, filtered slices,
  removed components, and removed slices as well as fingerprint values.
- Custom providers use an isolated registry per operation and cannot be enabled
  by repository configuration alone; callers must opt in explicitly.
- Malformed non-object config and lockfile roots produce usage errors instead of
  uncaught attribute errors.
- Version extraction is source-aware, binary and symlink content remains
  byte-accurate, and large-file guardrails fail with actionable errors.
- GitHub Action inputs are passed through environment variables and shell arrays
  to prevent command injection and JSON/stderr corruption.

## [0.9.1] - 2026-05-03

### Fixed

- TOML regex fallback: anchor end-of-line to reject invalid TOML on Python
  3.8-3.10 when the built-in `tomllib` is unavailable.
- Symlink hash parity: working-tree access reads `os.readlink()`, matching Git
  blob storage.
- Python 3.8 type annotation compatibility in test helpers.
- CI examples use `source=head` to avoid cross-platform checkout conversion
  differences.

### Added

- Boundary extraction status (`ok`, `partial`, or `error`) for generated
  component entries.
- Basic project governance documents: `LICENSE` and `CONTRIBUTING.md`.
- Tests for boundary extraction status behavior.

[Unreleased]: https://github.com/yzm1/boundver/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/yzm1/boundver/releases/tag/v0.10.0
