# Implementation Plan: boundver

_Updated 2026-05-03 — 463 tests passing, 1 skipped (symlinks on Windows). Near-term, CLI polish, code health, distribution, provider Phases 1–3 complete._

---

## Near-term

### Portability

- [x] Decouple `service-definition` boundary kind from core validation — dead code removed.
- [x] Fail with actionable guidance when config references unavailable boundary sources — `version_source` validated in `validate_config`; boundary path error now includes `component/file` path and actionable hint; tests added.

### Testing

- [x] Per-module unit tests (canonical JSON, sha256, semver, TOML/YAML extraction) — `tests/test_versions.py` (parse_semver, JSON/TOML/YAML extraction, extract_version) and `tests/test_hashing.py` (canonical_json, sha256_hex, source_tree_digest) created; 65 new tests.
- [x] Integration tests using temporary git repos (`git init` + commits in tmp dirs).
- [x] Edge cases: no version source, vendored copy drift, repo with no commits.
- [x] Parser coverage: 3-level TOML paths (`tool.poetry.version`), representative YAML patterns — covered in `tests/test_versions.py`.

### Publishing

- [~] Publish to PyPI — **DEFERRED** (tag-triggered workflow exists; register when adoption warrants it).

---

## CLI polish

- [x] `--format json|text` for all commands (`--json` replaced with `--format json|text`, `table` deferred).
- [x] Document `--exit-code` behavior for `verify` — exit code table (0/1/2) added to README; `verify` subparser description updated with structured exit code semantics.
- [x] Color output in TTY mode — `_green`, `_red`, `_yellow`, `_bold` helpers; applied to verify ok/fail, validate-config ok/fail, diff +/-/~, status warnings. Suppressed automatically for piped/JSON output.
- [x] Shell completions (bash, zsh, fish) — `completions` subcommand added; static scripts for bash/zsh/fish embedded in `core.py`; works without a git repo; 6 tests added.

---

## Code health

- [x] Split `core.py` into modules: `_git.py`, `_hashing.py`, `_config.py`, `_lockfile.py`, `_diff.py`, `_output.py`, `_completions.py`. `core.py` is now a 406-line re-export shim + `main()`. All 297 tests pass.
- [x] Batch git reads — `_git_batch_cat` implemented using `git cat-file --batch`; `source_tree_digest`, `boundary_paths_digest`, and `_content_only_digest` all use it for `head`/`index` sources, replacing O(N) subprocesses with O(1).
- [x] Binary blob reads — `_git_cat_blob` uses binary subprocess (no text-mode CRLF conversion); `_read_path_content` for head/index uses it. Working-tree reads normalize CRLF→LF for cross-platform consistency.

---

## Documentation

- [x] README: badges, comparison table, fixed stale `--json` flag references, docs index section.
- [x] `docs/getting-started.md` — install → first config → first lockfile → CI step.
- [x] `docs/gradual-adoption.md` — staged adoption from implicit provider to full boundary + compat coverage.
- [x] `docs/ci-cookbook.md` — GitHub Actions, GitLab, cache keys, pre-commit, JSON output scripting.
- [~] Docs site (mkdocs-material): rendered site with nav, search, and versioning — **DEFERRED**.

---

## Distribution

- [x] GitHub Action (`action.yml`) — composite action; `uses: yzm1/boundver@main`; inputs: `command`, `args`, `version`; outputs: `exit-code`, `issues`.
- [x] Standalone single-file download option — `scripts/build_standalone.py` produces `dist/boundver.pyz` (26 KB, no deps).
- [x] Docker image for CI without Python — `Dockerfile` + `.dockerignore`; `docker run --rm -v "$(pwd):/repo" -w /repo boundver verify`.
- [ ] Homebrew formula (stretch).
- [ ] Publish GitHub Action to marketplace (once PyPI publish happens).

---

## Future (v1.0+)

- [ ] `boundver watch` — regenerate on save.
- [ ] Config includes/extends.
- [x] Glob patterns in boundary sources — `fnmatch`-based glob support in `PathHashProvider.resolve()` and `_config.validate_config()`; `*`/`?`/`[` patterns expand against component files; `..` rejected; 10 new tests.
- [x] Pre-commit hook integration — `.pre-commit-hooks.yaml` at repo root; `boundver-verify` and `boundver-generate` hooks; `language: python`; `always_run: true`.
- [x] `boundver why <component>` — compares current fingerprints against the lockfile; shows which facets drifted (exact/boundary/compat), change-type classification, modified files under component path; exits 0 (up to date) / 1 (drifted) / 2 (error); shell completions updated; 8 new tests.
- [x] Support `boundary.config.yaml` / `.toml` — `find_config_file()` probes alternatives when default `.json` is absent; `load_config_file()` dispatches on extension (JSON built-in, YAML via PyYAML, TOML via `tomllib`/`tomli`); all config-loading sites in `core.py` updated; 11 new tests. **474 tests pass**.

---

## Product-model maturity (post-v1)

- [x] Real provider architecture: `extract`, `normalize`, `digest`, `validate_config`, `explain_diff` interface — designed in `docs/design/07-provider-architecture.md`. Protocol: `BoundaryProvider` (resolve/validate_config/explain_diff), `ProviderContext`, `ResolvedBoundary`; registry; 3-phase migration plan; security constraints for custom providers.
- [x] **Phase 1 — Protocol + built-in wrappers** (`src/boundver/providers.py`): `ProviderContext`, `ResolvedBoundary`, `BoundaryProvider` protocol; `PathHashProvider`, `ImplicitProvider`, `LeafProvider`, `OpenApiProvider`, `JsonFileProvider`, `PythonExportsProvider`, `TypeScriptExportsProvider`; provider registry; `compute_boundary()` (only SHA-256 call for boundary digests); `generate_lockfile()` now delegates to `compute_boundary()`. Added `tests/test_providers.py` (34 tests). **Zero digest drift — 352 tests pass**.
- [x] **Phase 2 — `options` + custom provider loading**: `boundary.options` added to config schema; `providers` top-level config key validated; `load_custom_providers()` in `providers.py`; `--allow-custom-providers` flag on `generate`/`verify`/`validate-config`/`check-config`/`status`; `BOUNDVER_ALLOW_CUSTOM_PROVIDERS` env var; `custom.*` namespace enforced; `generate_lockfile()` raises if providers declared without flag; registry isolation in tests. **463 tests pass**.
- [x] **Phase 3 — Semantic built-ins**: `JsonCanonicalProvider` (`json-canonical`) re-serialises JSON as RFC 8785 canonical form — stable across key reordering and whitespace. `OpenApiCanonicalProvider` (`openapi-canonical`) strips `info`/`servers`/`tags` top-level blocks and recursively removes `description`, `summary`, `externalDocs`, `example`, `examples`, and `x-*` extension keys — digest stable across docs edits, changes on endpoint/parameter/schema changes. Both registered at import time; `known_providers` updated in `_config.py`. 22 new tests (14 unit + 8 integration). **438 tests pass**.
- [ ] Semantic/canonical providers: `json-canonical`, `openapi-canonical`, TS/public API, Python/public symbol.
- [ ] Multi-boundary components (REST/events/CLI/schema per component).
- [ ] Dependency/impact model: component graph, `impact`, `affected`, `why` commands.
- [ ] Richer identity model: clarify exact/boundary/compat/api-version separation.

---

## Governance & contracts (post-v1)

- [x] Migration policy: `migrate_lockfile()` in `_lockfile.py`; `MigrationError` for unknown schemas; `boundver migrate-lock [--lock FILE] [--dry-run]` CLI subcommand — reads, migrates, writes in-place (strips legacy `generated_at`); dry-run prints without writing; 12 new tests. **486 tests pass**.
- [x] Stable machine contracts: JSON schemas for CLI outputs (verify, status, diff, discover) in `spec/cli-output.*.schema.json`; 6 conformance tests added (`tests/test_cli_output_schemas.py`).
- [ ] Security model: custom provider execution policy, CI allowlists, path escape constraints.
- [ ] Custom provider execution model: config shape (`type: command`/`type: python`), sandboxing, `--allow-custom-providers` flag.
- [x] Resolve `generated_at` in deterministic mode — removed entirely; lockfiles are always deterministic; `git log` provides timestamps.
- [ ] Maintainer sustainability: ownership/triage expectations, bus factor > 1.

---

## Strategic checkpoints

- Confirm at least one real team using boundver in CI; treat their friction as top priority.
- Validate persona: CI/platform users → prioritize Action/docs over runtime rewrites.
- [x] Dogfood boundver on itself — `boundary.config.json` tracks `src/boundver/` with all 11 source files as boundary paths; `boundary.lock.json` committed. Validated and generates cleanly.
- Re-evaluate quarterly against Nx/Turborepo/Bazel/Pants overlap.

## Non-goals (for now)

- No rewrite (Go/Rust) before spec + adoption proven.
- No plugin marketplace before provider interface and demand are clear.
- No over-splitting modules before contracts stabilized.
- No docs-site ceremony before spec + examples + CI path are solid.

---

## Decisions

- **CI cost control:** automated CI disabled until v1.0.0; see `docs/CI_REENABLE_PLAN.md`.
- **Schema validation:** optional `jsonschema` for strict mode; stdlib fallback for zero-dep baseline.
- **Terminology:** `boundary` is the only mode name. No `api` alias exists in schema or runtime.
- **Lockfile determinism:** `generated_at` removed entirely. Lockfiles are always deterministic. Use `git log` for generation timestamps.
