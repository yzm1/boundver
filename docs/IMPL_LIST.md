# Implementation List (Consolidated Reviews)

## Goal
Turn boundver from a useful Python CLI into a portable, spec-driven contract system that teams can reliably adopt in CI.

## Progress update (2026-05-02)
- ✅ Completed: `spec/boundary.lock.schema.json`.
- ✅ Completed: `spec/HASHING.md`.
- ✅ Completed: `spec/spec.md`.
- ✅ Completed: removed legacy `boundary-lockfile.md`.
- ✅ Completed: added `docs/WHY_BOUNDVER.md` positioning/decision guide.
- ✅ Completed: added `boundver explain <component>` for boundary-relevant change visibility.
- ✅ Completed: added scoped verification via `boundver verify --components ...`.
- ✅ Completed: added partial scoped generation via `boundver generate --components ...`.
- ✅ Completed: added lockfile merge strategy doc + merge-driver script (`docs/LOCKFILE_MERGE.md`, `scripts/boundver-merge-driver.sh`).
- ✅ Completed: added component discovery (`discover` command + `init --discover`).
- ✅ Completed: added bundled GitHub composite action (`.github/actions/boundver/action.yml`) for verify + diff-on-failure.
- ✅ Completed: added tag-triggered PyPI publish workflow (`.github/workflows/publish.yml`).
- ✅ Completed: added portability shell verifier (`scripts/boundver-verify.sh`).
- ✅ Completed: switched working-tree/index enumeration to git-native tracked-file listing (`git ls-files`).
- ✅ Completed: reduced boundary hashing overhead by de-duplicating overlapping boundary path expansions.
- ✅ Completed: added CI-race mitigation helper via `verify --changed-from <git-ref>`.
- ✅ Completed: defined and implemented symlink hashing policy (hash link-target text, not dereferenced bytes).

## Consolidated priorities (captures both review sets)

### P0 — Spec is the product (do first)
1. Publish **lockfile schema** (`spec/boundary.lock.schema.json`).
2. Publish **hashing determinism contract** (`spec/HASHING.md`) with precise rules for:
   - enumeration by source mode (`head`, `index`, `working-tree`),
   - path normalization to POSIX,
   - digest input format `file:{posix_path}\n{bytes}`,
   - sort order, component digest derivation, boundary/compat derivation,
   - slice aggregation + canonical JSON.
3. Publish **core spec doc** (`spec/spec.md`) defining exact/boundary/compat semantics.
4. Standardize file enumeration to git-native commands where possible (especially `index`/`working-tree` consistency), and explicitly document edge cases:
   - binary files included,
   - symlinks behavior,
   - empty directories excluded,
   - permission bits excluded,
   - Git LFS cross-mode caveat.

### P1 — Distribution and zero-friction adoption
5. Ship **GitHub Action** (`uses: yzm1/boundver-action@v1`) that hides runtime setup and supports verify + diff-on-failure.
6. Publish to **PyPI** so README install command is true.
7. Add minimal **shell verifier** (`tools/boundver-verify.sh`) as portability proof/spec compliance test.
8. Remove stale legacy doc `boundary-lockfile.md` (or replace with redirect note) to avoid conflicting guidance.
9. Add a clear “**Why boundver**” decision doc (when to use vs Bazel/Nx/Turborepo/Pants).

### P2 — Day-1 usability blockers (highest real-team pain)
10. Lockfile merge conflict strategy:
    - document “never hand-merge lockfile; regenerate,”
    - optionally provide merge driver/hook and post-merge regenerate helper.
11. Explainability:
    - add `boundver explain` or verbose verify output to show *which files/inputs changed*.
12. Large-repo performance:
    - reduce per-file subprocess overhead,
    - batch git reads where feasible.
13. Subset operations:
    - `generate/verify --components ...` and/or `--slice ...` to avoid full-repo recompute.
14. First-run adoption:
    - `init --discover` / `discover` for common ecosystems (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, etc.).
15. CI race/friction mitigation:
    - guidance and/or scoped verify so unrelated main-branch lockfile churn does not constantly break PRs.
16. Gradual adoption story:
    - document/component-level incremental rollout pattern explicitly.

### P3 — Product-model maturity (from fingerprint tool to decision tool)
17. Real provider architecture:
    - provider interface (`extract`, `normalize`, `digest`, `validate_config`, `explain_diff`).
18. Semantic/canonical providers:
    - `json-canonical`, `openapi-canonical`, configurable `openapi-contract`, TS/public API, Python/public symbol providers.
19. Multi-boundary components:
    - support multiple named boundaries per component (REST/events/CLI/schema/etc.).
20. Dependency/impact model:
    - component dependency graph,
    - `impact`, `affected`, `why` style commands.
21. Richer identity model:
    - clarify long-term separation among exact/boundary/compat/api-version (and possible behavior identity concept).

### P4 — Compatibility, contracts, and governance
22. Migration policy for terminology/schema changes:
    - explicit `api -> boundary` compatibility window,
    - `migrate-config` and `migrate-lock` commands.
23. Stable machine contracts:
    - document JSON schemas for `verify/status/diff` outputs,
    - define exit code policy and compatibility guarantees.
24. CI quality gates:
    - keep manual flow, plus low-cost path-filtered PR checks.
25. Security model (before custom providers become mainstream):
    - custom provider execution policy,
    - CI defaults/allowlists,
    - path/symlink escape constraints.
26. Maintainer sustainability:
    - define ownership/triage expectations and “project continuity” docs so bus factor > 1.

## Strategic checkpoints (non-code but required for correct prioritization)
27. Confirm at least one real team is actively using boundver in CI; treat that team’s friction as top-priority input.
28. Validate target distribution persona:
    - if CI/platform users are the main audience, prioritize Action/docs over runtime rewrites.
29. Dogfood boundver on boundver itself:
    - track CLI/config/lockfile interfaces as boundary examples.
30. Re-evaluate roadmap quarterly against ecosystem overlap (Nx/Turborepo/Bazel/Pants) and keep positioning “simple declarative boundary fingerprinting” sharp.

## Explicit non-goals (for now)
- Do **not** do a rewrite-first plan (Go/Rust) before the spec + adoption path are proven.
- Do **not** add plugin-marketplace complexity before provider interfaces and user demand are clear.
- Do **not** over-split modules purely for style before behavior/contracts are stabilized.
- Do **not** build docs-site ceremony before spec + examples + CI path are solid.

## Suggested execution sequence
- **Phase A (1–2 days):** P0 items 1–4.
- **Phase B (1 day):** P1 items 5–9.
- **Phase C (next sprint):** P2 items 10–16.
- **Phase D (following sprints):** P3/P4 items based on early adopter pain.

## Definition of “qualitative leap”
Boundver should be perceived as:
- a **portable spec** with a reference implementation,
- a **zero-install CI workflow** for teams,
- an **explainable impact tool** (not just hash mismatch reporter).
