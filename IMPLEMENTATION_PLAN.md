# Implementation Plan: boundver roadmap

This plan reflects the current prototype assessment: keep the architecture, improve truthfulness first, then generalize.

---

## Phase 0 (Immediate): Correctness and trustworthiness

- [ ] Add `validate-config` command with strict failures for:
  - unknown slice components
  - unknown slice modes
  - unsupported `defaults.compat_mode`
  - `service-definition` with empty `paths`
  - configured boundary paths missing on disk
  - `api` slices containing `leaf`/`implicit` components
- [ ] Remove silent slice fallback from `api`/`compat` to `exact`.
- [ ] Add strict digest selection behavior:
  - `api` mode requires `api` fingerprint (or explicit non-strict mode)
  - `compat` mode requires `compat` fingerprint
- [ ] Add explicit source selection for fingerprint generation:
  - `--source=head`
  - `--source=index`
  - `--source=working-tree`
- [ ] Align `compat_mode` behavior with config (implement and enforce supported modes).

## Phase 1: Boundary model generalization

- [ ] Move from single `boundary` to `boundaries[]` per component.
- [ ] Rename `boundary.kind` to provider identity (`boundary.provider`).
- [ ] Demote HSL-specific concepts to custom providers (e.g. `custom.hsl.service-definition.v1`).
- [ ] Normalize provider outputs to common shape:
  - status
  - raw digest
  - semantic digest
  - version (optional)
  - boundary summary
  - warnings/errors
- [ ] Add boundary-level slice targeting (`component + boundary id`).

## Phase 2: Semantic boundary quality

- [ ] Keep `api_raw_digest` and introduce `api_semantic_digest`.
- [ ] Add canonicalizers/extractors for first-party providers:
  - OpenAPI
  - TypeScript exports (`.d.ts`/API extractor output)
  - Python public symbols/stubs
  - JSON schema/service definition canonical JSON
- [ ] Preserve deterministic hashing across environments.

## Phase 3: Packaging and public release readiness

- [ ] Restructure into package layout (`src/boundver/...`).
- [ ] Add `pyproject.toml` and console script entry point.
- [ ] Add test suite (unit + integration + snapshot).
- [ ] Add CI (lint, type-check, tests across Python versions).
- [ ] Publish install path (`pip`/`pipx`) and release workflow.

## Phase 4: UX and ecosystem

- [ ] `init` scaffolding and config schema tooling.
- [ ] Additional CLI output formats (`json|text|table`) and verbosity controls.
- [ ] Shell completions and docs site.
- [ ] Optional adapters/integrations (GitHub Action, monorepo tooling).

---

## Release gating recommendation

Before using boundver as CI authority, require completion of **Phase 0** and corresponding tests.
