# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog.

## [Unreleased]

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
