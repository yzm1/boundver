# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Declared and enforced a telemetry-free invariant for the built-in CLI. The
  runtime has no outbound-network or telemetry-client imports, and every Git
  subprocess is restricted to a local-command allowlist with prompts, lazy
  fetches, tracing, hooks, filters, pagers, signature verification, and
  external diff helpers disabled.
  Added a public privacy policy, a visible voluntary adopter Discussion, and
  privacy-preserving Search Console ownership metadata for the documentation
  site; platform-side aggregate counters remain outside the CLI.
- Added a formal, machine-traceable semantic-provider RFC and adversarial
  threat model. The review-ready proposal keeps implementation and v0.15 work
  blocked until exact-commit proposal reviews pass, separates future capability-confined
  WebAssembly providers from legacy trusted in-process Python, and records the
  mandatory full-source bug, issue, and security gates for v0.15.0 promotion.
  The v0.15 gate is enforced by the local launcher and tag/publication
  workflows using fresh external reviews bound to the exact release tree;
  self-attested scan booleans or candidate identifiers cannot grant authority.
  Review edits and the earliest proposal/release approval expiry remain bound
  through the final tag-mutation handoff, and GitHub repository/owner numeric
  identities prevent namespace transfer or recreation from inheriting evidence.

### Fixed

- Replaced filter-capable working-tree Git comparisons with bounded raw file
  reads, preserved intent-to-add paths and sparse-checkout skip-worktree state
  in captured index membership, and charged repository scan budgets against
  bytes read before line-ending normalization. Dirty-component,
  changed-component, explain, migration, and working-tree fingerprint views
  now agree without executing repository code or misreporting sparse paths as
  deletions.
- Bounded every generated config and lockfile to the same 10 MiB UTF-8
  contract enforced by its loader. Full and scoped generation, verification
  updates, migration, init/add/remove, and the public Python API now reject an
  oversized result before atomic publication and leave an existing target
  byte-for-byte unchanged.
- Escaped repository-controlled control characters and leading GitHub workflow
  command markers in every human-readable CLI value and in the composite
  Action's line-oriented outputs. Normal Unicode remains readable, trusted TTY
  styling is applied only after escaping, and machine JSON retains exact data.
- Rejected lock outputs that alias the selected config or sit inside component
  and vendored-copy roots, including hard-link, symlink, junction, and parent-
  traversal spellings, before generation or update can mutate repository data.
- Allowed custom-provider declarations with a mix of explicit expected names
  and names resolved from provider instances at trusted load time. Static
  cross-reference checks now run only when the explicit-name set is complete;
  authoritative runtime registration still fails closed on missing or
  mismatched providers.
- Reused the uniquely identified, digest-verified retained OCI artifact when a
  release resumes after container publication, instead of rebuilding an
  immutable image and treating wall-clock build drift as a registry conflict.
  Future container builds also remove volatile Debian logs/cache and compile
  Python bytecode under `SOURCE_DATE_EPOCH` in both image stages.
- Allowed failed release runs to resume from their exact retained source
  artifacts after later container jobs add their own strictly named artifacts;
  unknown, stale, malformed, or cross-run artifacts remain fatal. Disabled the
  optional Docker diagnostic build-record upload for future release runs.
- Ran the recovery-control review audit from its checked-out Git repository so
  resumed workflows can validate current `main` before the release checkout
  exists.

## [0.14.1] - 2026-08-26

### Upgrade contract

- Semantic config: `boundver-semantic-config/v2`
- Lock schema: `boundary-lock/v3`
- Fingerprint compatibility: `digest-neutral`
- Lock regeneration: `not-required`

### Changed

- Replaced the generic shield/check branding with a Boundver-specific boundary
  change mark, aligned README/social/Marketplace presentation, and added a
  transparent project-avatar asset plus GitLab Catalog metadata.

### Fixed

- Replaced regex-backed wildcard evaluation with deterministic, budgeted
  matching for component selectors, migration analysis, and non-Git
  `.gitignore` fallback; unborn repositories now ask the installed Git for a
  bounded, version-exact non-ignored file listing. Wildcard segments also have
  explicit size and complexity limits, and shell character-class normalization
  is stable across supported Python versions. Embedded Git repositories are
  treated as opaque and excluded from unborn-repository fingerprints instead
  of being misclassified as hashable files.
- Prevented malformed YAML diagnostics from copying source lines into CLI logs
  or GitHub Action outputs while retaining the parser type and location.
- Disabled implicit network retrieval during optional JSON Schema validation
  and raised the `schema` extra's minimum to `jsonschema>=4.18` for the explicit
  offline registry API.
- Made `verify --changed-from` render its resolved `changed_components`
  scheduling hint in ordinary text output, including an explicit zero result,
  while continuing to validate full lock integrity.
- Corrected the recorded 0.12.0 release date to match its immutable tagged
  commit and public release.
- Accepted the remaining clean status flourishes already observed from the
  trusted Codex review app while retaining a positive verdict allowlist.
- Corrected the GitLab CI/CD Catalog component address to its public
  `boundver-project/boundver` namespace.
- Trusted the exact GitLab checkout directory before invoking Git so the Catalog
  container works with GitLab Runner's mounted repository ownership.

## [0.14.0] - 2026-08-25

### Upgrade contract

- Semantic config: `boundver-semantic-config/v2`
- Lock schema: `boundary-lock/v3`
- Fingerprint compatibility: `digest-neutral`
- Lock regeneration: `not-required`

### Added

- `verify --format json` reports the exact config and lock snapshot provenance
  used by the operation instead of leaving CI to infer those inputs.
- `verify --format json` provides typed `consumer_impact` rows for direct or
  transitive fan-out, separating configured components from external consumer
  terminals; the composite Action publishes the same data as an output.
- `discover` accepts repeatable `--exclude PATH` prefixes while retaining its
  Git-tracked default and bounded non-Git fallback.
- Release notes from 0.14 onward carry a machine-checked upgrade contract
  naming the semantic-config contract, lock schema, fingerprint compatibility,
  and lock-regeneration requirement.

### Changed

- `why` and `explain` infer their default changed-file base from the commit that
  introduced the component's current lock entry instead of assuming the
  previous commit or the latest unrelated partial lock update.
- `why` and `status` now honor effective per-component facet policy in their
  drift classification; non-gating observations remain visible without a
  regeneration recommendation or non-zero diagnostic result.
- Cross-cutting source, facet, partial-lock, generated-artifact, exit-code,
  and upgrade guidance now has one reference page; task-focused guides link to
  that contract instead of maintaining divergent copies.
- Reusable release-recovery validation now lives in a directly tested helper
  instead of large inline workflow programs, while reviewed control code and
  immutable candidate code remain separate trust boundaries.
- Config I/O and discovery, lockfile validation, canonical providers, bounded
  file reads, and CLI parser construction are split into focused modules behind
  compatibility facades, reducing maintenance hotspots without changing the
  public API.

### Fixed

- Exit-code-aware CI guidance now preserves every non-zero verification result
  after handling infrastructure exit `2`, so drift exits `3` through `5` cannot
  be accidentally converted to success.
- The credentialed Action-alias handoff byte-binds its imported
  `release_workflow.py` helper to the reviewed publication commit.
- The release review audit recognizes the trusted Codex app's observed
  `Nice work!`, `Another round soon, please!`, and `Bravo.` clean-verdict
  suffixes through the existing positive allowlist.
- Composite Action payload outputs now have conservative UTF-16-aware bounds,
  explicitly report truncation or unavailable results, retain complete or
  diagnostic JSON at `result-file`, and never expose a partial
  `consumer-impact` closure for CI routing. Portable temporary-result allocation
  supports repeated Action invocations in one Linux, macOS, or Windows job.
- Successful textual Git output now uses the filesystem codec with
  `surrogateescape` instead of the preferred process locale, preserving
  non-ASCII refs and otherwise undecodable bytes without weakening strict
  ASCII validation of object IDs and status fields.
- GitHub Release draft creation now retries bounded authenticated-list
  visibility before failing, then binds reconciliation to the returned numeric
  release ID; terminal failures include the canonical release URL for safe
  recovery.
- Release-note extraction normalizes CRLF/CR transport before writing artifacts
  and rejects an upgrade contract followed only by the `No changes yet.`
  placeholder.
- Facet guidance now states that `leaf` suppresses only the boundary identity;
  versioned or behavior-declared leaves can still gate `compat` or `behavior`.
- `validate-config` now preflights optional built-in provider dependencies and
  names `boundver[yaml]` when explicit `openapi-canonical` YAML selectors cannot
  load PyYAML; ambiguous selectors defer the check until their files resolve,
  so JSON-only directories remain dependency-free. Raw OpenAPI providers remain
  dependency-free.
- `status` validates config structure before constructing facet-policy output,
  keeping malformed component or slice shapes on the controlled diagnostic
  path instead of raising a traceback.
- Consumer-graph validation now enforces the published machine-output bounds
  for component count, repository-wide external terminals, and identifier
  length, so direct or transitive impact remains complete and schema-valid.
- Semantic-contract mismatches now identify whether the lock is newer or older
  than the running installation and give direction-specific upgrade guidance.
- `boundary-lock/v3` uses its immutable v0.13.0 canonical schema URL across
  future digest-neutral releases, avoiding annotation-only lock churn.
- Action compatibility aliases now use an explicit, environment-gated local
  maintainer handoff followed by independent exact-tag verification, because
  GitHub's built-in Actions token cannot update refs that expose workflow-file
  changes. The handoff remains monotonic, ancestral, and force-with-lease.
- Exact-artifact release recovery now binds the retained distribution and
  GitHub Release asset pair to its producing verification attempt, tolerating
  GitHub's duplicated successful-job history after a failed-workflow rerun
  while still rejecting ambiguous jobs or artifact pairs.
- Release-surface verification now expects the published package summary from
  the canonical project metadata, with a CI parity test covering the registry
  name, summary, Python requirement, and project URLs to prevent stale release
  controls from blocking exact-artifact recovery.

## [0.13.0] - 2026-08-23

### Added

- Presentation-only component `note` metadata for migration context, ownership
  guidance, and rationale without semantic-config digest churn, following the
  released v0.12 treatment of component `ecosystem` and boundary `note`.
- Verification baselines with create-only `--write-baseline`, read-only
  `--baseline`, and shrink-only `--update-baseline` workflows so established
  drift can be reviewed once while CI rejects new debt. The composite Action
  exposes only the read-only baseline gate.
- `discover --diff-config` reports discovered roots missing from configuration
  and configured roots absent from discovery; `migrate-lock --explain` audits
  0.10 whole-path glob behavior against current segment-aware selection before
  regeneration.
- A strict, hash-locked GitHub Pages documentation site, runnable
  consumer-impact demo, comparison guide, and distribution guide.
- Protected multi-platform GHCR publication with a read-only OCI build,
  retained digest handoff, anonymous-pull verification, immutable OCI labels,
  and GitHub artifact attestation.
- A deterministic Homebrew formula renderer backed by a self-contained,
  YAML-capable standalone archive, and a typed GitLab CI/CD Catalog component
  that binds each component release to the matching GHCR version.

### Changed

- Strict `validate-config` and generation now reject unavailable slice facets
  and empty explicit built-in path selections before lock computation, while
  `--allow-partial` remains the explicit escape hatch for intentional null
  slice inputs. Source-option help now names `head` as the default snapshot.
- Migration and CI guidance now distinguishes required lock regeneration from
  digest-neutral facet/slice results when selected content is unchanged,
  documents the raw `json-file-raw` to `path-hash` transition, and requires
  persistent developer, hook, and container environments to upgrade and assert
  their exact pinned boundver version.
- Public positioning now describes boundver narrowly as declared contract-drift
  classification and downstream-impact routing, with explicit relationships to
  build-graph, schema-compatibility, and release-automation tools.

### Fixed

- Compatibility-alias mutation now always executes from the immutable release
  tag while recovery loads reviewed publication controls from current `main`,
  keeping the secretless `GITHUB_TOKEN` workflow-tree authorization exact.
  Recovery fails before dispatch when a pre-v0.13 immutable tag lacks the child
  workflow and its approved alias still needs to move.
- Windows test cleanup retries only transient sharing violations for freshly
  closed temporary Git repositories, while persistent or unrelated permission
  errors still fail the supported-platform gate.
- Verification no longer recommends `--update` for unavailable facets that
  configuration cannot produce, explains when the lock remains untouched, and
  tells first-time `--source head` users to commit a generated lock or verify
  the working tree.
- The release review audit recognizes standard GitHub App bot logins and the
  current authenticated Codex clean-verdict wording without relaxing its
  exact-commit or positive-allowlist requirements.
- Read-only lock diffs now accept canonical `boundary-lock/v3` semantic-config/v1
  and v2 inputs and report the contract transition, while incompatible lock
  schemas or unknown contracts still produce one bounded diagnostic.
- Verification preflight identifies a semantic-contract/version mismatch,
  including the running boundver version and regeneration guidance, instead of
  calling a well-formed historical lock malformed.
- Index snapshot failures retain bounded, terminal-safe Git or OS detail from
  `git write-tree`/`ls-tree`, making intermittent capture failures diagnosable.
- CLI help and `--version` now use the stable `boundver` program name on
  Python 3.14 instead of exposing the interpreter and script path.

## [0.12.0] - 2026-08-23

This release advances the semantic configuration digest contract to v2.
Regenerate lockfiles after upgrading; v1 semantic digests cannot be relabelled.

### Breaking changes

- Locks now use `boundver-semantic-config/v2`. Component `ecosystem` and
  boundary `note` are explicitly presentation-only, while every
  contract-affecting field remains bound into the digest. Existing
  `boundary-lock/v3` locks carrying semantic-config/v1 must be regenerated;
  their digests cannot be relabelled.
- Raw and implicit built-in providers advance to v3, `json-canonical` advances
  to v3, and `openapi-canonical` advances to v4 for their bounded, stricter
  selection and validation contracts. Existing provider metadata must be
  regenerated even when selected artifact bytes are unchanged.

### Added

- A fail-closed `publish_release.py resume` recovery path can resume one
  completed failed publication from its exact tagged commit and original
  retained artifacts. It validates the source run, successful verification
  job, the job log's exact tag/SHA/compatibility-alias policy, artifact names,
  SHA-256 identities, expiry, and run association before dispatching the
  protected publication workflow once from current `main`; it never changes
  the approved alias policy, moves the release tag, or rebuilds and replaces
  the candidate.
- Standard GitHub-hosted macOS Intel and arm64 CI jobs; cross-platform
  composite-Action and pre-commit contracts on Linux, Windows, and macOS; an
  undefined-name and unused-code gate; Dependabot maintenance for
  Actions/Python pins; and coverage for every published pre-commit hook.

### Changed

- Git, filesystem, and provider hashing now stream and deduplicate inputs under
  explicit entry, path, transport, diagnostic, and aggregate memory limits;
  concurrent file/symlink changes fail closed, and glob complexity is bounded.
- Release candidates use one shared verifier and binary-only SHA-256 wheel
  locks for the complete Action, CI, and release toolchains. Python startup is
  isolated, and candidate tooling receives a minimal disposable environment.
- Local and protected release checks bound subprocess output, paginated API
  responses, tracked-file traversal, remote metadata, and compressed archive
  members before allocation; their JSON and TOML parsing is independent of
  Python's process-wide integer conversion setting.
- The runtime image pins its multi-platform Python base digest, Debian snapshot,
  and Git package, builds reproducibly from the hash-locked wheelhouse, and
  installs the final environment without network access.
- Repeated source-mode, Action, exit-code, and v2-upgrade guidance now links to
  the authoritative specification, cookbook, and migration procedure.
- Machine-readable schemas changed after a release identify the development
  contract on `main`; generated configs and locks remain pinned to the latest
  immutable release schema until the next release-preparation commit.

### Fixed

- The release-review audit accepts authenticated exact-commit Codex review
  evidence once every finding thread is resolved, lets one later exact verdict
  supersede earlier feedback, and still rejects adverse latest verdicts,
  equal-time ambiguity, unresolved threads, stale commits, and spoofing.
- The public `path-hash` v3 provider is registered as a built-in and documented
  as the format-neutral raw provider for arbitrary declared artifacts.
- Local and protected workflow dispatches bind deduplication to the exact tag,
  release/control commit, compatibility-alias policy, and recovery run. The
  local launcher discovers an accepted exact run even when the CLI response is
  ambiguous, and paginated draft/public Release discovery binds subsequent
  reads to the immutable numeric Release ID.
- Windows release checks resolve Git-for-Windows Bash instead of the WSL
  launcher, and credentialed review checks run from trusted control code with
  an explicit fine-grained read-only token and invoking Python interpreter.
- Windows release smoke environments use bounded short temporary paths so
  deeply nested build-tool files remain installable without weakening the
  isolated packaging checks or requiring legacy path-limit configuration.
- Publication recovery can continue after the exact GitHub Release has already
  become public and immutable, but only after reconciling its metadata and
  retained asset bytes. Public-surface verification runs reviewed control code
  from the recovery commit and treats LF, CRLF, and CR as equivalent transport
  spellings in otherwise exact release notes. A conflicting public Release is
  rejected before any TestPyPI or PyPI mutation.
- Changed-component selection now honors `--source`, immutable Git snapshots,
  both sides of cross-component renames, and repository-root components.
- Strict generation and filtered verification reject incomplete custom-provider
  results and vendored errors anywhere in a lock; malformed extension results
  are contained as controlled provider failures.
- JSON/YAML integers, symlink targets, canonical metadata, and version parsing
  behave deterministically across supported Python versions and platforms;
  deep JSON diagnostics and Windows reparse-point traversal fail closed under
  explicit depth, node, path, and byte ceilings.
- Lock diffs report project/config changes and slice metadata-only changes;
  malformed configs no longer produce tracebacks in `add`, `remove`, or
  `explain`.
- Human CLI output is safe on redirected Windows code pages, atomic rewrites
  preserve existing POSIX permissions, and positional names after `--` are no
  longer mistaken for global verbosity flags.
- Local release verification prefers Git-for-Windows Bash over the incompatible
  WSL launcher while retaining normal Bash discovery on other platforms.
- Trusted Codex release-review evidence must bind the exact reviewed commit;
  standard suggestion reviews count only after all threads are resolved, while
  a later exact verdict supersedes earlier feedback. Evidence is accepted only
  from a PR merged into this repository's `main`, and the mutation boundary
  requires identical timestamped review-state snapshots before and after the
  semantic audit and immediately before push.
- Registry OIDC jobs execute no repository code, and repository mutations use
  isolated reviewed control code; compatibility-alias mutation additionally
  binds an exact publication-control workflow to an immutable release checkout.
  Production PyPI retries reuse the complete exact artifact with duplicate
  tolerance, followed by byte-for-byte public verification, so a partial upload
  cannot make a failed-job retry unsafe.
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

[Unreleased]: https://github.com/yzm1/boundver/compare/v0.14.1...HEAD
[0.14.1]: https://github.com/yzm1/boundver/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/yzm1/boundver/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/yzm1/boundver/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/yzm1/boundver/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/yzm1/boundver/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/yzm1/boundver/releases/tag/v0.10.0
[0.9.1]: https://github.com/yzm1/boundver/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/yzm1/boundver/releases/tag/v0.9.0
