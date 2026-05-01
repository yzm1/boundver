# Distilled Work Items from Recent Reviews

## Progress Tracker

- [x] **P0.1 Packaging/installability**
  - **Status:** Completed on 2026-05-01.
  - **Notes:** Moved runtime implementation into importable package module (`src/boundver/core.py`) and updated CLI to import from package path.
  - **Verification:** `pip install -e .`, `python -c "import boundver"`, and `boundver --help` now succeed.
- [x] **P0.2 Normalize exact digest across source modes**
  - **Status:** Completed on 2026-05-01.
  - **Notes:** Updated `exact` hashing to use the same canonical SHA-256 content digest across `head`, `index`, and `working-tree` sources.
- [x] **P0.3 Behavioral fingerprint tests**
  - **Status:** Completed on 2026-05-01.
  - **Notes:** Added tests for internal-only changes, boundary changes, compat changes, strict missing-boundary failure, unrelated-component slice stability, and `head/index/working-tree` comparability.

## P0 — Blockers (do first)

1. **Fix packaging so installed CLI works**
   - Move runtime implementation into `src/boundver/` (e.g., `src/boundver/core.py`) **or** make setuptools include `boundary_lock.py` from the actual location.
   - Ensure `boundver` console script resolves in a fresh virtualenv after `pip install .`.
   - Add a packaging smoke test in CI: install wheel/sdist, run `boundver --help`.
   - **Acceptance criteria:** `pip install boundver` and `boundver status` work without repo-root hacks.

2. **Normalize `exact` digest across source modes (`head`, `index`, `working-tree`)**
   - Replace mixed hash domains (Git object hash vs custom SHA-256) with one canonical digest algorithm for all modes.
   - Canonical input: stable file ordering + normalized path + file bytes.
   - Update docs to describe exact algorithm and comparability guarantees.
   - **Acceptance criteria:** identical content across modes produces identical `exact` digest.

3. **Add behavioral tests for fingerprint guarantees**
   - Add integration tests (temp git repos) for:
     1) internal source change ⇒ `exact` changes, `api` unchanged
     2) boundary file change ⇒ `exact` + `api` change, `compat` unchanged
     3) major version bump ⇒ `compat` changes
     4) unrelated component change ⇒ existing slice unchanged
     5) missing boundary in strict mode ⇒ fails with clear status/errors
     6) `head/index/working-tree` digest behavior is documented and validated
   - **Acceptance criteria:** tests pass locally + CI; failures give actionable diffs.

## P1 — Correctness & semantics

4. **Clarify or rename `api` fingerprint semantics**
   - If it hashes declared boundary files, document it explicitly as non-semantic.
   - Consider rename path:
     - `api` → `boundary` (preferred), or
     - split into `boundary_raw` and future `boundary_semantic`.
   - **Acceptance criteria:** terminology matches behavior; no implied semantic API claims.
   - **Status:** Partially complete on 2026-05-01 (README wording clarified; internal field rename not started).

5. **Align implementation plan with shipped behavior**
   - Mark completed items (e.g., boundary status model) as done.
   - Separate “implemented” vs “planned” sections to reduce drift.
   - **Acceptance criteria:** plan checkboxes reflect current repo reality.
   - **Status:** Completed on 2026-05-01.

6. **Introduce provider vocabulary in boundary config**
   - Migrate from `boundary.kind` to `boundary.provider` naming.
   - Remove legacy `kind` support after migration window.
   - Example providers: `openapi.v1`, `python-exports.v1`, `json-file.v1`, `custom.hsl.service-definition.v1`.
   - **Acceptance criteria:** core engine operates on provider output, not hard-coded org concepts.
   - **Status:** Completed on 2026-05-01 (`boundary.provider` required; legacy `kind` rejected by validation).

10. **Remove hidden CWD coupling from git operations**
   - Ensure git commands run against the intended repository even when process CWD differs (e.g., use `git -C <repo_root>` consistently).
   - Add tests that call `generate_lockfile(..., source=\"head\"|\"index\")` while CWD is outside repo root.
   - **Acceptance criteria:** source-mode digest behavior is independent of caller working directory.
   - **Status:** Completed on 2026-05-01.

## P2 — Adoption and operational maturity

7. **Enable real CI (not dispatch-only placeholder)**
   - Trigger on PR + push for main branches.
   - Minimum gates: lint, unit/integration tests, packaging smoke install.
   - Add Python version matrix from stated support policy.
   - **Acceptance criteria:** CI runs automatically and blocks regressions.

8. **Add JSON Schema for `boundary.config.json`**
   - Publish schema in repo and document usage.
   - Validate config at runtime with clear, user-facing errors.
   - **Acceptance criteria:** invalid config fails early with precise validation messages.
   - **Status:** In progress on 2026-05-01 (schema file added; runtime validation expanded for required fields/provider, full schema-engine validation still pending).

9. **Scrub proprietary/default references from public docs**
   - Move org-specific examples under custom provider namespace or separate docs.
   - Keep README vendor-neutral.
   - **Acceptance criteria:** public docs read as general-purpose tooling first.

## Issues Discovered While Executing Work Items

- Packaging metadata used `package-dir = {"" = "src"}` together with `py-modules = ["boundary_lock"]` while `boundary_lock.py` lived at repo root, causing install/import failures for packaged use.
- `git_tree_hash`/`git_root()` rely on process CWD being inside the target repository; add a follow-up to use `repo_root`-anchored git invocations (or `git -C`) to remove hidden CWD coupling.
- Runtime config checks remain hand-rolled; consider wiring JSON Schema validation directly (stdlib-only fallback + optional strict validator path) to prevent drift between schema and code.

## Suggested sequencing (small, high-leverage increments)

- **Sprint 1:** #1 packaging + #7 minimal CI + packaging smoke test
- **Sprint 2:** #2 hash normalization + #3 tests for source-mode comparability
- **Sprint 3:** #4 terminology cleanup + #5 plan sync + #8 config schema
- **Sprint 4:** #6 provider migration + #9 documentation polish

## Definition of Done for next release candidate

- Install works from wheel/sdist in clean env.
- `exact` hash is source-mode comparable by design and by tests.
- CI is automatic and passing.
- Fingerprint field names/docs are semantically honest.
- Plan/docs accurately reflect implemented state.
