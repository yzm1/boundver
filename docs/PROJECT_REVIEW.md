# Project review and remediation record

- Date: 2026-08-12
- Baseline: `55dd961` (`main`, package version `0.9.1`)
- Remediation target: current working tree (planned package version `0.10.0`)

This document records the pre-release review of boundver's correctness,
security, packaging, onboarding, documentation, automation, and public project
surface. “Resolved” means the repository now contains the implementation,
regression coverage, or repeatable source-level verification named below. It
does not mean the release candidate has passed external CI or been published.

## Executive summary

All findings identified in the baseline review and the subsequent focused
security/correctness passes are resolved in the current tree. Fingerprint
generation and verification now fail closed, hashing uses an unambiguous v2
wire format, Git paths are handled as NUL-delimited literal data, custom Python
providers require caller-controlled opt-in, and the public Action treats inputs
as data. Lock/config validation, partial updates, metadata/provider checks,
source modes, and OpenAPI canonicalization have focused regression coverage.

The usability and visibility work is also represented in the repository: the
README has an immediate workflow, examples use the installed CLI, one public
Action is documented, package metadata and community files are present, and
the supported Python floor is consistently 3.9. Release readiness remains
conditional on the unchecked execution and publication gates at the end of
this document; no claim is made here that external CI, GitHub Marketplace, or
PyPI has completed those gates.

## Baseline findings (BV-001–BV-042)

| ID | Severity | Area | Resolution evidence | Status |
|---|---|---|---|---|
| BV-001 | Critical | Generation | Strict generation rejects `version_errors`, `exact_errors`, `behavior_errors`, and boundary errors; missing/oversized/raced-file regressions are in `tests/test_hashing_contract.py` and `tests/test_boundary_lock.py`. | Resolved |
| BV-002 | Critical | Verification | `changed_components_since_ref` rejects invalid refs and conservatively covers config/unmapped changes and slices; see `test_verify_invalid_changed_from_exits_usage` and changed-from tests in `tests/test_cli_main.py`. | Resolved |
| BV-003 | Critical | Hashing | `_hash_framed_entries` uses a versioned, domain-separated, length-prefixed binary frame; collision and known-vector tests are in `tests/test_hashing_contract.py`. | Resolved |
| BV-004 | High | Git paths | Git filenames use NUL-delimited byte output plus filesystem decoding; Unicode, newline, non-UTF-8, and literal-backslash regressions are in `tests/test_hashing_contract.py`. | Resolved |
| BV-005 | High | Canonical OpenAPI | `_strip_openapi` distinguishes annotation fields from user-named maps; `tests/test_provider_contract.py` covers annotation-looking schema/property names. | Resolved |
| BV-006 | High | Guardrails | `_git_batch_cat` rejects missing, malformed, truncated, non-blob, and oversized responses; focused tests are in `tests/test_hashing_contract.py` and `tests/test_coverage_gaps.py`. | Resolved |
| BV-007 | High | Custom providers | `_resolve_allow_custom` accepts only caller flag/environment authorization; `load_custom_providers` validates module/class/name inputs. Coverage is in `tests/test_providers.py`, `tests/test_edge_cases.py`, and `tests/test_cli_main.py`. | Resolved |
| BV-008 | High | GitHub Actions | Root `action.yml` passes inputs through environment variables and a quoted Bash array, validates enums, and installs from `github.action_path`; `.github/workflows/ci.yml` contains a hostile-input contract check. | Resolved |
| BV-009 | High | Source modes | `_list_files_for_source` treats the successful Git index as authoritative and working-tree mode as tracked-only; see the corresponding tests in `tests/test_hashing_contract.py`. | Resolved |
| BV-010 | High | Lock semantics | `COMPONENT_METADATA_FIELDS`, structure validation, and `verify_lockfile` cover path, version, provider identity/version, status, SemVer, consumers, metadata, vendored data, and recorded errors. | Resolved |
| BV-011 | High | Partial generation | `generate_lockfile_for_components` requires a valid v2 base, recomputes current entries, rejects stale unselected entries, reconciles removals, and recomputes all slices; see partial-generation tests in `tests/test_core_branches.py`. | Resolved |
| BV-012 | Medium | Installed validation | The config schema is package data under `src/boundver/`; `scripts/packaging_smoke.sh` inspects the wheel and validates an installed copy in an unrelated repository. | Resolved |
| BV-013 | Medium | Config loading | Config and lock loaders reject non-object roots, and hand validation covers malformed nested data; see `tests/test_core_branches.py`, `tests/test_boundary_lock.py`, and `tests/test_cli_main.py`. | Resolved |
| BV-014 | Medium | Config mutation | `_ensure_json_mutation_path` prevents `init`, `add`, and `remove` from serializing JSON into YAML/TOML paths. | Resolved |
| BV-015 | Medium | Discovery | Git-aware discovery uses manifest-specific version sources and deduplicates directories; see `test_discover_components_uses_tracked_manifests_and_deduplicates_dirs`. | Resolved |
| BV-016 | Medium | First run | `init --discover` discloses an empty result instead of inventing `src`, and working-tree validation rejects missing component roots; covered in discovery/init and config-validation tests. | Resolved |
| BV-017 | Medium | Status UX | `print_status` shows component identity, path, version, provider/status, short fingerprints, consumers, and all error categories with corrected guidance; status tests cover text and JSON. | Resolved |
| BV-018 | Medium | Action contract | The duplicate repository-local Action was removed; root `action.yml` is the documented interface and emits command-aware JSON outputs. | Resolved |
| BV-019 | Medium | CLI protocol | Runtime JSON and schemas agree, including status warnings; `tests/test_cli_output_schemas.py` validates real command payloads. | Resolved |
| BV-020 | Medium | Provider protocol | Provider metadata, validation, and explanations are wired, and operations use isolated registries; contract tests are in `tests/test_provider_contract.py`. | Resolved |
| BV-021 | Medium | Completions | Completion scripts cover every parser subcommand and supported options; see completion tests in `tests/test_boundary_lock.py` and `tests/test_cli_main.py`. | Resolved |
| BV-022 | Medium | Module entry point | `src/boundver/__main__.py` exists, and `scripts/packaging_smoke.sh` exercises installed `python -m boundver --version`. | Resolved |
| BV-023 | Medium | Python support | `tomli` is conditional for Python below 3.11, and the supported/build/test floor is consistently Python 3.9 in `pyproject.toml`, docs, and workflow matrices. External matrix execution remains a release gate. | Resolved |
| BV-024 | Medium | Release safety | `.github/workflows/publish.yml` validates exact tag/package-version equality, runs tests, builds, checks distributions, and publishes only the verified artifact job output. | Resolved |
| BV-025 | Medium | Documentation | `README.md` leads with the canonical workflow; `docs/ci-cookbook.md` uses the public Action or explicit installation, and packaging smoke covers the installed workflow. | Resolved |
| BV-026 | Medium | Documentation accuracy | README/getting-started/CI guidance pairs source modes and describes declared-artifact drift rather than semantic compatibility proof. | Resolved |
| BV-027 | Medium | Examples | Example READMEs use `boundver`; `test_examples_expected_lockfiles_are_current` verifies their expected lockfiles. | Resolved |
| BV-028 | Medium | Package metadata | `pyproject.toml` contains keywords, classifiers, project URLs, and publication-safe documentation links. Rendered PyPI verification remains a publication gate. | Resolved |
| BV-029 | Medium | Public repository | `SECURITY.md`, `CODE_OF_CONDUCT.md`, `SUPPORT.md`, issue forms, and the pull-request template are present. | Resolved |
| BV-030 | Low | Test portability | Subprocess tests use the active interpreter (`sys.executable`) or platform-appropriate installed commands instead of assuming a `python` shim. | Resolved |
| BV-031 | Low | Distribution contents | Package-data/MANIFEST configuration includes runtime schema, type marker, specs, policies, and tests needed by the sdist; `scripts/packaging_smoke.sh` inspects wheel and sdist members. | Resolved |
| BV-032 | Low | Packaging lifecycle | `pyproject.toml` uses the SPDX `license = "MIT"` form plus `license-files`, and its Python 3.9 floor is compatible with the security-patched `setuptools>=78.1.1` build requirement. | Resolved |
| BV-033 | Medium | Diff reporting | `_diff.py` compares the shared `COMPONENT_METADATA_FIELDS` as well as fingerprints and reports metadata-only changes. | Resolved |
| BV-034 | Medium | Shell verifier | The divergent standalone shell verifier was retired; `spec/HASHING.md` and Python v2 hashing are the supported contract. | Resolved |
| BV-035 | Medium | Line endings | All source modes use the same text CRLF normalization while preserving binary bytes; cross-source and CRLF tests are in `tests/test_hashing.py`, `tests/test_hashing_contract.py`, and `tests/test_providers.py`. | Resolved |
| BV-036 | High | CI policy | CLI/config `verify_facets`, non-gating observations, JSON output, and Action inputs separate exact-only observations from gated facets; see facet/update tests in `tests/test_cli_main.py`. | Resolved |
| BV-037 | High | Exit protocol | `core.py` defines distinct usage, behavior, boundary, and compatibility exit codes and chooses the highest gated severity; CLI tests cover boundary, behavior, and compatibility exits. | Resolved |
| BV-038 | High | Consumer impact | Config validation and lock metadata support `consumers`; verify/why report affected consumers. `MainSeverityAndConsumerTests` provides end-to-end coverage. | Resolved |
| BV-039 | Medium | Discovery scale | Discovery prefers NUL-safe `git ls-files`, deduplicates directories, excludes known dependency/build/vendor directories, and retains a bounded non-Git fallback; `test_discover_components_excludes_ignored_dirs` covers the exclusions. | Resolved |
| BV-040 | Medium | Contract additions | Glob behavior is documented and tested for matching, traversal rejection, newly added files, and content changes in `tests/test_providers.py`. | Resolved |
| BV-041 | Medium | Merge workflow | The unsound merge-driver script was retired; `docs/LOCKFILE_MERGE.md` specifies post-merge full regeneration and verification. | Resolved |
| BV-042 | Medium | Update UX | `verify --update` recomputes successfully before atomically replacing the lock via `_write_text_atomic`; update behavior is covered in `tests/test_cli_main.py`. | Resolved |

## Follow-up security and correctness findings (BV-043 onward)

These findings were discovered during adversarial re-review after the baseline
remediation. They are listed separately to preserve the audit trail rather than
folding them invisibly into the broader baseline items.

| ID | Severity | Area | Finding and resolution evidence | Status |
|---|---|---|---|---|
| BV-043 | High | Changed selection | A component configured at `.` was not selected for root-file changes. `_git.py::changed_components_since_ref` now handles root paths; `ChangedFromRootComponentTests` verifies the mapping. | Resolved |
| BV-044 | High | Provider path identity | Root-component label slicing could drop the first filename character, making a rename hash-insensitive. `_component_relative_path` now derives exact labels; `RootPathBoundaryIdentityTests` verifies rename sensitivity. | Resolved |
| BV-045 | High | POSIX filenames | Replacing backslashes in Git-returned paths collapsed distinct POSIX names such as `a\b` and `a/b`. Labels now preserve literal backslashes; `test_literal_backslash_filename_is_not_treated_as_a_separator` covers it. | Resolved |
| BV-046 | High | Git pathspecs | Component names beginning with Git pathspec magic, such as `:(literal)foo`, could select a different tree. Hashing and diagnostics now pass `--literal-pathspecs` in `_git.py` and `_output.py`. | Resolved |
| BV-047 | High | Partial locks | Component-scoped generation could create an incomplete first lock or relabel v1 digests as v2. It now requires an existing structurally valid v2 lock; `test_missing_existing_lockfile_requires_full_generation` and `test_non_v2_existing_lockfile_requires_full_generation` cover both cases. | Resolved |
| BV-048 | High | Partial locks | A valid-looking partial update could retain stale unselected component/config/provider data. Partial generation now recomputes the full current lock and rejects stale unselected entries before merging, then rebuilds every slice. | Resolved |
| BV-049 | High | Declared paths | Providers treated the declaration set as valid when one path matched even if another literal/glob did not. Raw and canonical providers now track every unmatched declaration and return an error; provider tests cover missing literals and globs. | Resolved |
| BV-050 | High | Versions | A configured version source that was missing, unparsable, or non-SemVer could produce `null` compatibility data without failing strict generation. `_compute_component_entry` records `version_errors`, and `parse_semver` uses full-string validation; version-source and trailing-junk tests cover it. | Resolved |
| BV-051 | High | Tag versions | `--changed-from` could omit tag-derived components because tags do not appear in a file diff. `_git.py` always includes components using `git_tag_prefix`; the tagged selector assertion in `test_verify_changed_from_checks_unselected_component_metadata` exercises it. | Resolved |
| BV-052 | High | Verification preflight | Unknown explicit `--components` entries could be intersected away by `--changed-from` and return clean. `_cmd_verify` validates requested names before selection; `test_verify_unknown_components_exits_2` covers the controlled error. | Resolved |
| BV-053 | High | Verification preflight | Unknown facets, malformed locks, component/slice set drift, and recorded lock errors could be bypassed by an empty changed set. `_cmd_verify` performs these preflight checks before changed-path scheduling; CLI malformed/facet/ref tests cover the paths. | Resolved |
| BV-054 | Medium | Malformed locks | Nested v2 fields such as fingerprints, SemVer, consumers, error arrays, vendored metadata, and slice members could crash verify/status/slice/why. `_lockfile_structure_issues` now validates consumed types and command handlers use it; `MainMalformedV2LockTests` verifies controlled errors. | Resolved |
| BV-055 | Medium | Malformed config | Nested non-object/non-string config values could reach provider/path logic when optional `jsonschema` was absent. Hand validation now guards defaults, providers, components, boundaries, behaviors, versions, consumers, vendored paths, and slices; malformed-config tests run with the schema engine disabled. | Resolved |
| BV-056 | High | Source modes | Public/core API source typos silently behaved like working-tree mode. `_normalize_source` now accepts only `head`, `index`, or `working-tree`, and all lock operations/accessors call it. | Resolved |
| BV-057 | High | Self-referential locks | A lock output inside a component, especially a root component, became part of its own exact fingerprint. Config rejects root components and `_ensure_lock_outside_components` guards CLI and public API generate/verify paths. | Resolved |
| BV-058 | High | Traversal and symlinks | Component, boundary, behavior, version-source, and vendored paths could traverse or follow working-tree symlinks outside the repository. `_config.py`, `_SourceAccessor`, and hashing containment checks reject unsafe paths or hash Git symlink blobs as link text; focused containment tests cover component roots, version files, vendored paths, boundary paths, and cross-source symlink-blob parity. | Resolved |
| BV-059 | High | Public API | `boundver.generate()` and `boundver.verify()` bypassed full config/source validation, robust lock loading, and self-lock guards. `src/boundver/__init__.py` now shares `validate_config`, `_load_lockfile`, and `_ensure_lock_outside_components` with the CLI. | Resolved |
| BV-060 | Medium | Source-aware validation | `head`/`index` operations validated required files only against the working disk, rejecting valid committed snapshots after local deletion. `validate_config(source=...)` now defers snapshot existence to Git; `SourceAwareValidationTests` covers a deleted working-tree component still present at HEAD. | Resolved |
| BV-061 | High | Project metadata | A missing or changed lockfile project could pass when fingerprints matched. Lock structure requires a non-empty project and verification compares it with config before component work. | Resolved |
| BV-062 | High | Changed-from integrity | With no selected path changes, `--changed-from` returned before current metadata/provider versions were recomputed. It now falls through to full integrity verification; `test_verify_changed_from_no_paths_still_checks_provider_version` covers the failure. | Resolved |
| BV-063 | High | Changed-from integrity | The first fix still skipped unselected entries whenever any component was selected (for example, a tag-versioned component). Changed paths are now reporting-only while all entries are recomputed; `test_verify_changed_from_checks_unselected_component_metadata` covers the two-component tagged/tampered case. | Resolved |
| BV-064 | High | Canonical OpenAPI | Additional arbitrary-name maps—including paths/webhooks, component maps, security requirements, callbacks, links, server variables, schema maps, headers, encodings, and mappings—could lose keys named `description`, `example`, or `x-*`. `_OPENAPI_COMPONENT_MAPS` and `_OPENAPI_NAMED_MAP_KEYS` enumerate those contexts; `tests/test_provider_contract.py` explicitly exercises schema/property/definition and callback/link/variable names. | Resolved |
| BV-065 | Medium | Fail-fast severity | `--fail-fast` could return a lower-severity first component drift while a later component had compatibility drift. Verification now evaluates all selected entries, chooses the global highest-severity issue, and only then limits the report. | Resolved |
| BV-066 | Medium | Slice exits | Slice mismatches omitted their mode, so the exit-code mapper could not assign behavior/boundary/compatibility severity. Slice messages now include `<slice>.<mode>` and `_drift_exit_code` applies the same severity contract. | Resolved |
| BV-067 | High | Git failure handling | A Git listing failure inside a real repository could fall back to approximate filesystem enumeration and produce a false-clean fingerprint. `_list_files_for_source` now re-raises in real repositories and reserves the bounded fallback for non-Git/unborn setup. | Resolved |
| BV-068 | Medium | Diagnostics | Explain/why diagnostics used line-delimited Git names and non-literal pathspecs, corrupting quoted, newline, or pathspec-magic filenames. `_output.py` now uses `--name-status -z`, `_parse_name_status_z`, filesystem decoding, and `--literal-pathspecs`. | Resolved |
| BV-069 | Medium | Schema-independent identity | Project type and component/slice key types depended on optional `jsonschema`, allowing schema-invalid identities on a base install. Explicit checks were added; `SchemaIndependentConfigValidationTests` patches out the schema engine and verifies rejection. | Resolved |
| BV-070 | Medium | Build/runtime floor | The earlier Python 3.8 support claim conflicted with the setuptools build-backend floor. `pyproject.toml`, CI matrices, README, maintained guides, and `CHANGELOG.md` now consistently declare Python 3.9+. External matrix execution remains a release gate. | Resolved |
| BV-071 | Medium | Atomic writes | Direct lock/config replacement could leave truncated JSON if writing failed. `_write_text_atomic` writes and fsyncs a sibling temporary file before `os.replace`, and generate, verify-update, migration, init, add, and remove route mutations through it. | Resolved |
| BV-072 | Medium | Documentation lifecycle | The historical implementation plan described a retired Action interface and linked deleted design/CI files, while a provider docstring repeated one stale link. The obsolete plan is retired and the provider points to the maintained custom-provider guide. | Resolved |

## Verification index

The main repeatable local evidence is grouped here to keep the finding tables
readable:

- `tests/test_hashing_contract.py`: v2 framing, filename byte safety, tracked
  source semantics, guardrails, malformed Git batch data, and read races.
- `tests/test_provider_contract.py` and `tests/test_providers.py`: provider
  framing/metadata/hooks, registry isolation, OpenAPI named maps, declared path
  matching, canonicalization, and globs.
- `tests/test_security_regressions.py`: root-path identity and selection,
  schema-independent identity validation, and source-aware validation.
- `tests/test_cli_main.py`: verification preflight, changed-from full integrity,
  malformed nested locks, facets/update behavior, exit severity, and consumers.
- `tests/test_boundary_lock.py`, `tests/test_core_branches.py`, and
  `tests/test_edge_cases.py`: lock generation/verification, partial locks,
  metadata, discovery, versions, config validation, source modes, and commands.
- `tests/test_cli_output_schemas.py`: real JSON payloads against the published
  command schemas.
- `scripts/packaging_smoke.sh`: distribution membership and installed CLI/module
  entry-point workflow.

The committed release candidate passed all 834 unit/integration tests in four
balanced shards without an environment shim. Fresh wheel and sdist builds from
that exact commit contained the required runtime/audit assets; the extracted
wheel passed module-entry-point, config validation, generate, verify, and status
smoke checks in an unrelated Git repository. These local results do not
substitute for the supported-version CI matrix or the publication workflow.

## Visibility and adoption baseline

At review time, the public repository had one star, no forks, no GitHub
Releases, and an existing Marketplace listing at version `0.9.1`. PyPI's latest
release was also `0.9.1`, published through trusted publishing. The remediation
tree now configures project links, discovery keywords, clearer outcome-led
messaging, an immediate copyable workflow, and one canonical Action. Those
changes are not described as publicly released until the publication gates
below complete.

The intended audience is teams maintaining polyglot repositories, services,
or dynamically typed libraries whose consumer-facing contracts are represented
by files such as OpenAPI, JSON Schema, or public export modules. Messaging makes
clear that boundver detects drift in declared artifacts; it does not prove
semantic or backward compatibility.

## Release gate

- [x] Every finding above is marked resolved with a verification reference.
- [x] Unit and integration suites pass without environment shims (834 tests).
- [ ] Supported Python versions pass in external CI.
- [ ] Root Action passes its external workflow test with safe input handling.
- [x] Wheel and sdist inspection plus installed-package smoke tests pass for the exact local release commit.
- [ ] Tag exactly matches `pyproject.toml` and `boundver.__version__`.
- [ ] GitHub branch/PR checks pass before the release tag is created.
- [ ] GitHub Release, Marketplace tag, and PyPI publication all point to the same commit.
