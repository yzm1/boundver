# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A fail-closed `publish_release.py resume` recovery path can resume one
  completed failed publication from its exact tagged commit and original
  retained artifacts. It validates the source run, successful verification
  job, the job log's exact tag/SHA/compatibility-alias policy, artifact names,
  SHA-256 identities, expiry, and run association before dispatching the
  protected publication workflow once from current `main`; it never changes
  the approved alias policy, moves the release tag, or rebuilds and replaces
  the candidate.

## [0.11.0] - 2026-08-14

This release changes the meaning of stored fingerprints and requires a fresh
lockfile. It is a minor release rather than a patch to the immutable `v0.10.0`
release.

### Breaking changes

- Lockfiles use `boundary-lock/v3`. File entries now bind Git mode and object
  type as well as path and content, so executable-bit and regular-file/symlink
  transitions cannot remain invisible.
- v1 and v2 fingerprints cannot be relabelled or mechanically migrated. Run a
  full `boundver generate` from the intended source snapshot after upgrading.
- Declared path patterns now have conventional, case-sensitive path semantics:
  `*`, `?`, and character classes match within one path segment, while a whole
  `**` segment matches zero or more directories. This corrects v0.10 behavior
  in which `*` crossed `/` and `**` did not match the zero-directory case.
- Raw path and canonical provider versions are advanced where their selection,
  parsing, or hash identity changed. Regeneration is required even when current
  contract bytes are unchanged.

### Added

- A semantic configuration digest in every v3 lock. It binds effective
  defaults, components, provider declarations and options, path selectors,
  version sources, vendored copies, typed consumers, component gate policies,
  and slices.
- One shared glob engine for boundary providers, behavior paths, config
  diagnostics, and explain output. Raw and canonical JSON/OpenAPI providers
  accept the same literal and glob declarations.
- Snapshot-level Git identity: `head` captures one commit and `index` captures
  one tree for the complete operation. Tag-derived versions are selected only
  from tags reachable from the captured commit.
- Typed impact graphs: `consumers` defines validated configured-component
  edges, `external_consumers` defines opaque terminals, and `--transitive` on
  `verify`/`why` reports the downstream closure without changing direct output
  by default.
- `closure_of` slice membership, resolved as the seed plus its deterministic,
  cycle-safe downstream component closure.
- Per-component `verify_facets`, with precedence CLI override, component,
  defaults; selecting a configured/CLI facet that is unavailable returns usage
  exit `2`, while the policy-free fallback gates available facets.
- Machine-readable `why --format json` and `slice --format json` output.
  (`status --format json` already existed in 0.10.)
- Focused first-principles, provider-hardening, hashing-v3, and distribution
  contract tests for the corrected invariants.
- A fail-closed maintainer entry point, `scripts/publish_release.py`: `check`
  inventories and verifies every local and public release surface without
  mutation, while explicitly confirmed `start` can dispatch only the protected
  tag-creation workflow with an exact tag, SHA, and compatibility-alias policy.
- A repository-hygiene gate rejects generated caches, unresolved merges,
  case-colliding paths, unsafe executable modes, CRLF/trailing-whitespace drift,
  and conflict markers before CI packaging or release dispatch.

### Changed

- A configured behavior fingerprint cryptographically includes its boundary
  fingerprint. A boundary change therefore always changes behavior, even when
  `behavior.paths` was declared incorrectly.
- `consumers` remains a validated foreign-key graph of direct downstream
  configured components. External systems now use the separate
  `external_consumers` field, resolving the contradiction between v0.10's
  validator and examples without sacrificing typo detection.
- `generate --allow-partial` now relaxes only intentional null slice inputs.
  Missing declarations and provider, version, behavior, exact, or vendored-copy
  computation errors remain fatal.
- `head` and `index` bind configuration and the verification lock to the same
  captured source as component artifacts. Index workflows must stage changed
  source/derived output/config, generate, then stage the lock before verify.
- Component-scoped generation and `verify --components ... --update` recompute
  the current lock, refuse to preserve stale unselected entries, replace each
  selected component as one coherent entry, and recompute all slices.
  `--facets` remains a reporting and gate policy, not a field-level update mask.
- Root-manifest discovery maps a single-package repository to one unambiguous
  tracked package directory or conventional `src`, `lib`, or `app` directory.
  `init --discover` now exits without writing a config when no safe component
  root can be inferred.
- Base-install validation rejects unknown and malformed fields without relying
  on the optional `jsonschema` package, and installed validation prefers the
  schema bundled with boundver over a repository-local file.
- Release candidates are rebuilt and tested before tagging; every contributing
  PR needs current exact-commit review evidence with no blocking state,
  unresolved threads, or pending review requests. The normal gate requires a
  non-author human approval from a collaborator with push access; an
  owner-authored PR in this personal repository may use a trusted Codex review
  whose numeric account ID and latest PR head or merge commit are verified.
  Publication accepts only an explicit immutable version tag and SHA, and
  GitHub release notes come from the matching changelog section. Action,
  Docker, pre-commit, wheel, sdist, and standalone-archive packaging checks now
  exercise their installed forms.
- PyPI publication is gated by a real TestPyPI rehearsal using Trusted
  Publishing. Every stage downloads one immutable numeric artifact ID; exact
  filenames, sizes, SHA-256 values, and CDN bytes are checked, and the
  hash-pinned TestPyPI wheel is installed with index/dependency resolution
  disabled before the same wheel and sdist can reach production PyPI.
- Release builds use the tagged commit timestamp, a fixed build toolchain, and
  canonical archive metadata. Wheel, sdist, and standalone bytes must match a
  second clean build before any candidate artifact can be uploaded;
  retained artifact IDs make failed-job retries reuse that accepted candidate.
- The complete GitHub Release is prepared as a draft with wheel, sdist,
  versioned standalone archive, and `SHA256SUMS` before the owner publishes it
  to Marketplace. Production PyPI then receives the same identified
  distributions, provenance and public bytes are verified, and only the
  compatible `v0.11` Action alias advances; the breaking release leaves `v0`
  on the 0.10 line.
- Release mutation is serialized repository-wide, exact-tag dispatches are
  bound to their ref and SHA, ambiguous dispatch responses are deduplicated,
  partial drafts resume only byte-identical missing assets, and compatibility
  aliases cannot move backward or race an unexpected current target.
- The source distribution now contains only runtime and user-facing source,
  specifications, examples, and community documents; repository tests,
  automation, and maintainer-only audit material remain in GitHub. The runtime
  container uses a multi-stage wheel build so those repository files are not
  retained in image layers.

### Fixed

- Missing or divergent vendored copies are fatal during strict generation and
  cannot be blessed into a lockfile.
- Structured issue classification prevents component names or message text from
  spoofing verification severity, including under `--fail-fast`.
- JSON canonicalization rejects duplicate keys and non-finite numbers and no
  longer claims RFC 8785 conformance.
- JSON config, lock, provider, and version parsing applies the same bounded
  integer contract on every supported Python version, including Python 3.9.
- `why --format json` reports each staged working-tree path once instead of
  duplicating paths present in both the HEAD and cached diffs.
- The release-review audit materializes GitHub review and comment records
  before nested API lookups, so a Windows GitHub CLI invoked from WSL cannot
  consume later exact-commit evidence from the audit loop.
- GitHub Release draft recovery discovers private drafts through the
  authenticated paginated release list and reconciles them by numeric ID;
  draft-only 404 responses from the public tag endpoint no longer strand a
  verified release candidate.
- OpenAPI canonicalization rejects malformed roots, unsafe or external
  references, ambiguous YAML constructs, and invalid map keys; it retains
  extension fields as contract data and handles response keys consistently.
- Empty, absolute, traversing, backslash-separated, and otherwise ambiguous
  declared paths fail consistently across configuration, hashing, and explain
  operations.
- Working-tree normalization is made explicit with repository line-ending
  attributes, avoiding CRLF-only dirty-tree noise in this project.

### Known limitations

- Generated boundary artifacts still have no first-class freshness relation to
  their source or generator. Run a deterministic generator `--check` before
  boundver and stage derivation source/output/config/lock coherently. An
  executable repository-configured `derived_from` command was not added because
  trust, tool identity, and snapshot materialization need a separate design.
- Compatibility fingerprints still require a file or reachable
  `git_tag_prefix` version source. Sibling-derived and constant identities are
  roadmap options; configured/CLI `compat` selection now fails instead of
  comparing null values successfully.

## [0.10.0] - 2026-08-12

`v0.10.0` is immutable. This section describes what the release actually added
relative to `v0.9.1`; it does not attribute later corrections to that release.

### Breaking changes

- Lockfiles moved to `boundary-lock/v2` and had to be regenerated from repository
  content because the v1 hash frame was ambiguous. `migrate-lock` refused the
  conversion and did not relabel old digest bytes.
- Git-backed source modes became tracked-file-only after a repository's first
  commit.
- Verification introduced facet-specific exit codes: `1` for exact/metadata,
  `3` for behavior, `4` for boundary, and `5` for compatibility drift; `2`
  remained an input or usage error.
- The root GitHub Action changed from the free-form `command`, `args`, and
  `version` interface to structured verify-only inputs and outputs.
- Repository configuration alone could no longer authorize Python imports for
  custom providers; trusted callers had to opt in explicitly.
- The supported Python floor moved from 3.8 to 3.9.

### Added

- Facet-scoped verification with `verify --facets` and
  `defaults.verify_facets`; non-selected fingerprint drift was reported as an
  observation.
- `verify --update`, severity-aware reporting, and direct-consumer reporting
  for boundary and compatibility drift.
- `consumers` metadata on components.
- Git-aware hardening for existing manifest discovery, including tracked-file
  enumeration and duplicate-directory suppression.
- Provider identity/version metadata, isolated provider registries, custom
  provider hooks, and stricter provider result validation.
- Installed-package schemas, packaging smoke checks, public project metadata,
  support/security/community files, and a single documented Marketplace Action.

### Changed

- Generation and verification added stricter config, lock, metadata, digest,
  removed-component, removed-slice, and changed-ref checks.
- Partial generation required a valid v2 base, reconciled component removals,
  and recomputed slice entries.
- Config mutation commands refused YAML/TOML output instead of serializing JSON
  into those files.
- Pre-commit verification moved from `head` to `index`; the unsafe automatic Git
  merge-driver workflow was replaced with an explicit post-merge regeneration
  workflow.
- Action input handling, JSON output, packaging membership, and publication
  automation were hardened.

### Fixed

- Hash framing, NUL-delimited Git filename handling, missing/malformed Git
  objects, source-aware version extraction, byte-accurate symlinks, and bounded
  file reads received fail-closed handling.
- Each individual declared path/pattern that matched nothing became fatal
  (0.9.1 already failed when the overall selection was empty). Malformed
  config/lock roots, invalid changed refs, self-referential lock paths, unsafe
  traversal, and stale provider metadata also failed closed.
- Canonical OpenAPI processing preserved more user-named keys that resembled
  annotation fields.

### Known issues discovered after release

- The documented glob grammar did not match v0.10: `*` crossed directory
  separators and `**` did not have conventional zero-or-more-directory
  behavior. Canonical providers did not share one selector implementation.
- v2 file hashes did not bind Git mode/type, so executable-bit changes and some
  regular-file/symlink transitions could verify clean.
- The lock did not bind all semantic configuration choices, and behavior only
  relied on a path-coverage warning instead of including the boundary digest.
- Strict generation could write a lock for a missing or divergent vendored copy
  that immediately failed verification.
- `consumers` validation treated every name as a configured-component foreign
  key, while examples also placed external downstream systems in the field.
  Unknown-name rejection itself worked; the model and documentation conflicted.
- `--allow-partial` could emit a null-containing lock that normal verification
  rejected, and an explicit compatibility gate could pass vacuously for a
  component with no version source.
- Impact output stopped at immediate consumers, verification policy was global,
  and slices could not derive membership from the consumer graph.
- `head`/`index` artifact reads could be combined with working-tree config or
  lock content instead of one source-bound control snapshot.
- There was no first-class derived-artifact freshness check; a stale generated
  contract could retain its recorded boundary digest.
- Component-scoped `verify --update` could regenerate outside the requested
  scope instead of refusing stale unselected entries.
- Canonical JSON/OpenAPI parsing accepted inputs or discarded contract data in
  several edge cases, and root-manifest-only discovery could produce no usable
  component.
- The release tag gate checked the main SHA and package version, but did not
  reject unresolved review threads. A high-priority vendored-copy review on
  PR #10 remained unresolved after merge and before tagging.

## [0.9.1] - 2026-05-03

### Fixed

- Anchored the TOML regex fallback to reject trailing invalid data on Python
  versions without `tomllib`.
- Read working-tree symlinks with `os.readlink()`, matching Git blob storage.
- Restored Python 3.8 compatibility in test-helper annotations.
- Used `source=head` in CI examples to avoid checkout line-ending differences.

### Added

- Boundary extraction status (`ok`, `partial`, or `error`) in generated
  component entries.
- Initial `LICENSE` and `CONTRIBUTING.md` governance files and status tests.

## [0.9.0] - 2026-05-03

### Added

- Initial public release with Git-backed component fingerprints for exact,
  behavior, boundary, and compatibility facets.
- JSON/YAML/TOML configuration, component slices, built-in raw and canonical
  providers, version sources, discovery, generation, verification, diff/status,
  GitHub Action, Docker, pre-commit, PyPI, and standalone archive entry points.

[Unreleased]: https://github.com/yzm1/boundver/compare/v0.11.0...HEAD
[0.11.0]: https://github.com/yzm1/boundver/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/yzm1/boundver/releases/tag/v0.10.0
[0.9.1]: https://github.com/yzm1/boundver/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/yzm1/boundver/releases/tag/v0.9.0
