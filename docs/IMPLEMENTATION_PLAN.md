# Implementation Plan — Consolidated Review Backlog

_Last updated: 2026-05-02 (progress updated)_

This plan consolidates the two latest review passes into one prioritized, issue-ready backlog with sequencing and acceptance criteria.

## Progress snapshot

- ✅ Completed: **P0.1** schema/runtime `boundary` mode alignment.
- ✅ Completed: **P0.3** source-aware `head` boundary path resolution.
- ✅ Completed: **P0.4** removed silent `head`/`index` fallback to working tree.
- ✅ Completed: **P0.5** deterministic hashing updates (bytes + POSIX path normalization).
- ✅ Completed: **P1.7** repo-root-scoped `git_latest_tag`.
- ✅ Completed: **P0.6** packaging smoke test automation (`scripts/packaging_smoke.sh`).
- ✅ Completed: **Packaging smoke maintenance** script aligned to current `init --out/--force` CLI contract.
- ✅ Completed: **P1.9** graceful CLI handling for strict generation `ValueError`.
- ✅ Completed: **P1.15 (partial)** schema `$id` updated to real repository URL.
- ✅ Completed: **P2 item from reviews** reachable-tag preference for tag-based version extraction.
- ⏳ In progress: **P0.2** behavioral test baseline expansion.
- ⏳ In progress: **P1.8** parser robustness (TOML now uses `tomllib` when available; YAML still minimal parser).
- ✅ Completed: **P1.10 (partial)** boundary-path traversal validation + read error hardening in digest paths.
- ✅ Completed: **P1.11 (doc path)** hardcoded working-tree ignore behavior now documented.
- ✅ Completed: **P1.14 (partial)** optional `jsonschema` behavior documented in README.
- ✅ Completed: **P1.12** deterministic lockfile mode via `generate --deterministic`.
- ✅ Completed: **P1.13** function rename cleanup (`source_tree_digest`, `boundary_paths_digest`, `list_head_files`).
- ✅ Completed: **P1.8 (partial+)** YAML extraction now supports optional PyYAML robust parsing with fallback parser.
- ✅ Completed: **P1.16 (partial+)** `init` now supports `--out`, `--force`, writes `$schema`, and has CLI tests.
- ✅ Completed: **P2.19** diff summary wording now uses boundary-first phrasing.
- ✅ Completed: **P2.18 (updated)** verify validates lockfile schema presence/support with boundary-only fingerprints.
- ✅ Completed: **P1.15 (partial+)** README config example now includes `$schema` header for editor validation/autocomplete.
- ✅ Completed: **P2.20** provider capability matrix added to README with explicit raw-vs-semantic note.
- ✅ Completed: **P2.24** added `docs/public-vs-custom-providers.md`.
- ✅ Completed: **P2.21 (partial+)** added examples for `openapi`, `json-file`, and `implicit-and-leaf` with expected lockfiles.
- ✅ Completed: **P0.2 (expanded)** added verify/diff behavioral coverage for new/removed/stale components and summary wording.
- ✅ Completed: **Future-proof cleanup** removed legacy helper aliases and deprecated `init --config` alias.
- ✅ Completed: **P1.10 (improved)** digest IO/Git failures now surface explicit component errors instead of only null digests.
- ✅ Completed: **P2.18 (expanded)** verify now checks lockfile schema + structural integrity (components/slices/fingerprints).
- ✅ Completed: **P1.16** `init` flow now validated end-to-end (`init` + `validate-config` with existing component path).
- ✅ Completed: **P1.15** schema/editor UX examples are in README (`$schema` header + schema URL guidance).
- ✅ Completed: **P2.21** examples now include openapi, json-file, implicit-and-leaf, python-package, and typescript-package.
- ✅ Completed: **Example integrity check** automated test now verifies all example expected lockfiles match deterministic generation.
- ✅ Completed: **P2.24+** runtime package now exposes `boundver.__version__`.
- ✅ Completed: **P3.25** manual CI workflow now runs tests/build/install smoke for Python 3.8 and 3.12.
- ✅ Completed: **P3.26** added low-cost path-filtered PR CI workflow (`pr-lite.yml`) for core change paths.
- ✅ Completed: **P3.28** added `generate --dry-run` for non-writing preview.
- ✅ Completed: **P3.25+** machine-readable JSON output added for generate/verify/diff/status command flows.
- ✅ Completed: **P3.26+** added CLI logging controls (`--quiet`, `--verbose`).
- ✅ Completed: **P3.30** added large-repo hash guardrails (max files + max file size) with explicit component errors.
- ✅ Completed: **Core split (partial)** extracted version parsing/extraction into `src/boundver/versions.py`.
- ✅ Completed: **Hidden failure-mode fix** verify now short-circuits on malformed lockfile structure issues to avoid secondary crashes.
- ✅ Completed: **Hidden failure-mode fix** `versions.extract_version` now safely handles git-tag sources when resolver is unavailable.
- ✅ Completed: **Hidden failure-mode fix** hash size guardrail now enforces on Git-sourced content (`head`/`index`), not only working-tree stat paths.
- ✅ Completed: **Hidden failure-mode fix** CLI now enforces `--quiet` and `--verbose` mutual exclusivity.
- ⏳ Pending next: **P3** final release polish wrap-up and optional core module split.

## P0 — Correctness blockers

1. **Schema/runtime alignment for slice mode `boundary`**
   - Update `boundary.config.schema.json` slice mode enum to include `boundary`.
   - Acceptance:
     - A config using `"mode": "boundary"` passes schema validation.
     - Runtime validation also accepts it.

2. **Behavioral test baseline before additional refactors**
   - Add tests for exact digest, boundary digest/status, slice strict behavior, and config validation edge cases.
   - Acceptance:
     - `python -m pytest -q` passes locally.
     - Required coverage exists for digest determinism + slice generation/validation paths.

3. **`source=head` boundary path resolution must not depend on working tree**
   - Make boundary path expansion source-aware (file/dir checks for `head` and `index` must be Git-backed, not disk-presence-backed).
   - Acceptance:
     - If a boundary file exists in `HEAD` but is deleted in working tree, `generate --source=head` output is unchanged.

4. **Fix source-mode contract violations in content reads**
   - Remove silent fallback from Git content reads (`head`/`index`) to working-tree disk reads when Git lookup fails.
   - Surface explicit boundary error/status instead.
   - Acceptance:
     - `--source=head` never fingerprints uncommitted fallback content.

5. **Cross-platform deterministic hashing hardening**
   - Normalize hashed paths to POSIX separators.
   - Hash bytes, not decoded text (`errors="replace"` removal).
   - Acceptance:
     - Same repo state yields identical digest across Windows/Linux/macOS.
     - Binary boundary files are hashed deterministically from raw bytes.

6. **Packaging smoke test (wheel install path)**
   - Add smoke script/target:
     - build package
     - install wheel into clean venv
     - run `boundver --help` and `boundver init --out ...`
   - Acceptance:
     - Wheel install works without repo-local import side effects.

---

## P1 — High-value hardening

7. **`git_latest_tag` must be repo-root scoped**
   - Thread `repo_root` through version extraction and call Git via root-scoped helper.
   - Acceptance:
     - Tag-based version extraction is stable when CWD differs from repo root.

8. **Version extraction parser robustness**
   - Use `tomllib` when available (with documented fallback behavior where needed).
   - Document YAML extractor limitations or add optional robust YAML parser path.
   - Acceptance:
     - Test coverage includes common 3-level TOML paths (`tool.poetry.version`) and representative YAML patterns.

9. **Graceful CLI handling for expected user misconfiguration**
   - Catch generation-time `ValueError` (strict slice missing boundary/compat digest) and print actionable error, no traceback.
   - Acceptance:
     - Misconfigurations fail with user-facing remediation guidance.

10. **Path safety + IO error handling**
   - Validate boundary paths cannot escape component/repo roots.
   - Convert permission/read OS errors into structured boundary errors.
   - Acceptance:
     - No raw traceback for permission/path traversal config mistakes.

11. **Ignore semantics clarity (`.gitignore` vs hardcoded list)**
   - Either:
     - switch working-tree enumeration to Git-aware ignore behavior; or
     - document hardcoded ignore contract explicitly.
   - Acceptance:
     - Users can predict whether ignored/generated files affect digest.

12. **Deterministic lockfile mode**
   - Address `generated_at` idempotency noise (optional deterministic flag or metadata strategy).
   - Acceptance:
     - Regenerate-no-change workflow can produce no-op diffs.

13. **Rename misleading hashing helpers**
   - `git_tree_hash` → `source_tree_digest`
   - `git_hash_files` → `boundary_paths_digest`
   - `_head_files_for_path` → `list_head_files`
   - Acceptance:
     - Names reflect behavior and source semantics.

14. **`jsonschema` behavior explicitly documented + optional extras**
   - Clarify runtime deps remain none; `jsonschema` provides stricter optional schema validation.
   - Optionally add extras (`schema`, `dev`) in project metadata.
   - Acceptance:
     - README clearly distinguishes baseline vs enhanced validation.

15. **Schema metadata/editor UX**
   - Replace placeholder schema `$id` with real URL.
   - Add README example with `"$schema"` header for IDE autocompletion.
   - Acceptance:
     - Schema ID is non-placeholder.
     - Editor integration instructions are copy-paste ready.

16. **`init` contract completeness + tests**
   - Validate default output path, overwrite protection, `--out`, `--force`, terminology (`boundary.provider`), and inclusion of `$schema`.
   - Acceptance:
     - Fresh repo flow: `boundver init` + `boundver validate-config` succeeds after referenced path exists.

---

## P2 — Product maturity

17. **Remove legacy alias surface**
   - Keep boundary-only terminology in runtime, schema, and lockfile output.
   - Acceptance:
     - No new lockfile writes include legacy alias fields.

18. **Lockfile schema enforcement + migration messaging**
   - Explicitly handle known schema (`boundary-lock/v1`) and unknown/missing schema.
   - Acceptance:
     - `verify` reports clear unsupported-schema errors/warnings.

19. **Terminology cleanup in diff summaries**
   - Replace residual API-centric phrasing with boundary-first wording where semantically appropriate.
   - Acceptance:
     - User-visible summaries do not overclaim semantic API compatibility.

20. **Provider scope clarity + capability matrix**
   - Document built-ins as raw-boundary providers (or rename namespace convention in a future breaking change).
   - Add provider capability table.
   - Acceptance:
     - Users understand semantic vs raw behavior and provider expectations.

21. **Examples directory**
   - Add `examples/` with representative configurations and expected lockfiles.
   - Acceptance:
     - New users can run examples end-to-end.

22. **Split `core.py` after test net exists**
   - Extract modules (git source, hashing, validation, versions, lockfile, diffing, output) with no behavior change.
   - Acceptance:
     - Pre/post refactor tests remain green.

23. **Documentation cleanup for legacy/internal artifacts**
   - Update/remove stale docs (`boundary.kind`, old script names, proprietary/internal references).
   - Acceptance:
     - Public docs are terminology-consistent and externally safe.

24. **Runtime package ergonomics**
   - Add runtime `__version__` access path.
   - Acceptance:
     - `import boundver; boundver.__version__` works.

---

## P3 — Release/public polish

25. **Manual CI workflow that actually executes test/build/install checks**
   - Keep manual dispatch for cost control, but add runnable test matrix workflow.

26. **Future low-cost PR CI path filters**
   - Prepare path-filtered auto CI for key files once re-enabled.

27. **Machine-friendly CLI output + logging controls**
   - Add `--json`/`--format json`, `--quiet`, `--verbose` in phased manner.

28. **`generate --dry-run`**
   - Add non-writing preview mode.

29. **Operational guardrails for very large repos/files**
   - Add sensible file-count/size safeguards and diagnostics.

30. **PyPI release pipeline**
   - Publish package and automate release flow (tag-triggered recommended).

---

## Recommended execution order (consolidated)

1. P0.1 schema enum alignment (`boundary` mode).
2. P0.2 testing baseline (digest/slice/validation + verify/diff).
3. P0.3 + P0.4 source-mode correctness (`head`/`index` no fallback, source-aware boundary path expansion).
4. P0.5 deterministic hashing (bytes + POSIX paths).
5. P0.6 packaging smoke test.
6. P1.7 + P1.8 version extraction correctness (`git_latest_tag`, TOML/YAML robustness).
7. P1.9 + P1.10 graceful errors and path safety.
8. P1.11 + P1.12 ignore semantics + deterministic lockfile option.
9. P1.13 naming cleanup.
10. P1.14 + P1.15 schema/docs clarity.
11. P1.16 init contract tests.
12. P2.17–P2.23 maturity docs/policy/examples/refactor.
13. P3 release polish and CI evolution.

## Consolidated major risks

1. Schema-valid and runtime-valid configs may still diverge.
2. `source=head/index` correctness can be violated by fallback or disk-dependent path classification.
3. Test proof is still insufficient for CI-critical commands (`verify`, `diff`, source modes).
4. Cross-platform determinism remains at risk without byte hashing + normalized separators.
5. Documentation/terminology drift can cause user misunderstanding of boundary vs semantic API guarantees.

## Newly uncovered follow-ups

1. ✅ Resolved: migrated `project.license` to SPDX string form (`"MIT"`) to avoid setuptools TOML-table deprecation path.
