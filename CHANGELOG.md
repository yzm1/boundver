# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.15.0] - 2026-09-04

### Upgrade contract

- Semantic config: `boundver-semantic-config/v2`
- Lock schema: `boundary-lock/v3`
- Fingerprint compatibility: `digest-neutral`
- Lock regeneration: `not-required`

### Added

- Added `boundver review BASE..TARGET`, with equivalent explicit
  `--base`/`--target` inputs and opt-in merge-base semantics, for historical
  exact/behavior/boundary/compat transitions after endpoint locks have been
  reconciled. The versioned `boundver-review/v1` JSON result binds both
  immutable commit trees and config/lock inputs, records effective facet
  policy, reports conservative base/target consumer-edge provenance, supports
  direct or transitive closure, and maps changed or affected components into
  slices. Absent or ambiguous refs, missing shallow history, incompatible
  contracts, incomplete digests, and unreconciled config/lock graphs fail
  closed. Both immutable endpoints are fully recomputed before comparison;
  custom Python providers retain their explicit trusted-code opt-in.
  Successful review is a read-only query and leaves `verify` as the integrity
  gate.
- Added a reproducible 20-component intrinsic-runtime benchmark with
  per-phase timing, Git-process and source-read attribution, first/repeated
  runs, and enforced Linux CI ceilings for clean HEAD and small staged-change
  verification.
- Added bounded provider-aware structural explanations to historical range
  review. An `openapi-canonical` boundary transition now reports deterministic
  added, removed, and changed RFC 6901 paths from format-neutral canonical
  JSON under the versioned `boundver-structural-diff/v1` provider interface,
  bound to both immutable commit/tree identities, provider versions, and
  boundary digests. Reports expose JSON types but never source values and are
  explicitly structural evidence, not a backward-compatibility verdict. Raw
  and unsupported providers remain byte-opaque. Input, nesting, work, path,
  row, and output ceilings fail closed into a typed incomplete explanation
  with no partial rows while leaving the independently complete facet/impact
  review available.
- Added a deterministic public range-review demonstration and sanitized field
  case study modeled on a 17-component, six-slice monorepo. One command now
  proves implementation-only, behavioral, and OpenAPI-boundary transitions
  before lock reconciliation, then commits the reconciled target and asserts
  equivalent historical text/JSON results, exact endpoint provenance,
  direct/transitive consumers, changed slices, and structural paths. CI binds
  the checked-in terminal capture to fixed fixture commits. Reported field and
  per-run timings are labeled as observations rather than benchmark claims,
  and the case study separates product facts from build-graph,
  schema-compatibility, generated-artifact, and declared-graph limitations.
- Added `boundver review --format plan`, a versioned `boundver-plan/v1`
  downstream-test projection generated from the same immutable endpoint
  capture as the full review. The maintained GitHub Action and GitLab Catalog
  component now accept explicit base/target, merge-base, facet-policy, and
  direct/transitive controls; publish complete result files plus bounded,
  explicitly truncated summaries; expose fail-closed component/slice selection
  arrays; diagnose shallow history with platform-specific remediation; and
  retain exact review artifacts. GitHub source annotations are limited to
  precise target files whose commit matches the checked-out `HEAD`.
- Added one stable, fail-closed `required-pr-gate` over the complete supported
  platform, build, public Action, and public installation CI topology. A
  default-branch `workflow_run` validates the exact source run and current pull
  request without executing pull-request code, binds protected-file inspection
  to the immutable validated base/head comparison, rejects changes to its
  workflow or gate controls, and alone publishes the required commit status. A
  checked-in active `main` ruleset requires that exact GitHub Actions status,
  pull requests, squash-only merges, and resolved conversations while blocking
  deletion and force pushes. Release promotion compares the complete effective
  live policy with the checked-in contract and rejects overlapping active
  rulesets, additive classic branch protection, stale statuses, extra merge
  methods, review-policy drift, and bypass changes.
- Declared and enforced a telemetry-free invariant for the built-in CLI. The
  runtime has no outbound-network or telemetry-client imports, and every Git
  subprocess is restricted to a local-command allowlist with prompts, lazy
  fetches, tracing, hooks, filters, pagers, signature verification, and
  external diff helpers disabled.
  Added a public privacy policy, a visible voluntary adopter Discussion, and
  privacy-preserving Search Console ownership metadata for the documentation
  site; platform-side aggregate counters remain outside the CLI.
- Added a formal, machine-traceable semantic-provider RFC and adversarial
  threat model. The review-ready proposal keeps semantic-provider implementation
  blocked until exact-commit proposal reviews pass, separates future capability-
  confined WebAssembly providers from legacy trusted in-process Python, and
  records mandatory full-source bug, issue, and security gates for the planned
  v0.16.0 semantic-provider promotion. The semantic-provider release gate is
  enforced by the local launcher and tag/publication workflows using fresh
  external reviews bound to the exact release tree;
  self-attested scan booleans or candidate identifiers cannot grant authority.
  Review edits and the earliest proposal/release approval expiry remain bound
  through the final tag-mutation handoff, and GitHub repository/owner numeric
  identities prevent namespace transfer or recreation from inheriting evidence.
- Added weekly, push, and pull-request CodeQL analysis with the extended Python
  security query suite. Release promotion now also requires exact-commit CodeQL
  evidence, no open code/secret/dependency alerts, private vulnerability
  reporting, Dependabot security updates, secret-scanning push protection,
  full-SHA Action enforcement, and a read-only non-approving workflow token.

### Changed

- Reworked the README and hosted landing page around the familiar lockfile and
  CI-check category, a short decision path, an early statement of limits, and
  responsive, self-hosted styling. Package, Action, container, Homebrew, and
  release-verifier summaries now share the same product description.
- Published the canonical specification and hashing contract in the searchable
  documentation without copying their source, and added a glossary, security
  model, symptom-led troubleshooting guide, and documentation style guide.
  Every user-facing configuration field now has editor help in both
  byte-identical JSON Schema copies.
- Decoupled v0.15 development and release from semantic-provider proposal
  acceptance. v0.15 contains no semantic-provider implementation and continues
  through the ordinary exact-candidate release controls; the independent
  proposal and release gates remain fail-closed for semantic-provider work and
  the planned v0.16.0 semantic-provider release.
- Raised the supported Python floor to 3.10 so every maintained interpreter can
  install an advisory-free automation toolchain. Refreshed every hash-locked
  Action, CI, and release dependency and made live canonical PyPI vulnerability
  metadata a fail-closed lock-generation, CI, and release preflight.
- Made source scope explicit in ordinary generate, verify, status, explain,
  and why output, with adjacent-source commands when explain finds no changes.
  Source flags remain per-invocation and command defaults and JSON provenance
  are unchanged. Status now separates declared consumer edges from component
  details, while verify and why render typed direct/transitive consumer impact
  in a distinct section.
- Reduced the release-container runtime surface by removing pip, setuptools,
  wheel, and every set-ID bit after installation. CI now exercises the image
  without network access or Linux capabilities, with `no-new-privileges`, a
  read-only root and repository, and no writable temporary filesystem.
  High/critical repository and two-architecture image scans use a
  digest-pinned scanner; temporary no-fix Debian exceptions are package-scoped,
  justified, and expire fail-closed.
- Made automation-tool installation resolve the complete reviewed hash lock
  with pip's dependency solver and no cache reuse. Exact pins, wheel-only
  policy, and checked-in hashes remain mandatory, while incompatible pins now
  fail before automation runs. Updated `pyOpenSSL` to the advisory-free release
  compatible with the locked `cryptography` major version, and made the
  release-only profile reject Python older than its 3.12 toolchain contract
  before contacting the index.

### Security

- Updated the isolated build backend to Setuptools 84.0.0, beyond the
  vulnerable `<83.0.0` range for GHSA-h35f-9h28-mq5c / CVE-2026-59890, and
  refreshed the coordinated test toolchain to Coverage 7.16.0, pytest 9.1.1,
  and Ruff 0.16.5. Generated wheel-only, hash-pinned automation locks remain
  the authority for CI, Action, container, and release installs.

### Fixed

- Bound release-preflight diagnostics to the fixed TestPyPI and PyPI entries,
  and fixed the public Action smoke test for valid reviews that select no
  configured components.
- Kept trusted runtime-benchmark fixture setup outside the production CLI's
  read-only Git allowlist while preserving the same bounded, telemetry-disabled
  subprocess environment. Benchmark command attribution and the staged-index
  process ceiling now account for the deliberate coherent-snapshot checks.
- Closed fail-late resource-exhaustion paths in JSON/YAML/TOML parsing,
  capped JSON rendering, structural provider diffs, range-review graph unions,
  GitHub Action output export, and release-ruleset glob matching. Structural
  explanations now cap retained canonical endpoint input at 32 MiB and become
  explicitly unavailable on overflow without weakening the underlying review.
  Integer and floating-point tokens now share explicit cross-format lexical
  ceilings before conversion in config, baseline, Action, CI-gate, and release
  metadata readers.
- Prevented standalone, Homebrew-formula, and release-artifact outputs from
  traversing a pre-existing symlink, junction, reparse point, or replaced
  parent. Packaging cleanup now removes only the repository's exact generated
  directories, refuses redirected targets, and is an explicit `--clean`
  operation rather than an unbounded shell deletion.
- Sanitized malformed YAML diagnostics so an offending config source line is
  never copied into CLI or CI output. Action-result JSON now also rejects
  duplicate keys, non-finite numbers, oversized integers, and over-budget
  line/selection encodings before allocating their expanded representation.
- Made every runtime Git subprocess non-interactive, local-object-only, free of
  replacement refs, unable to take optional write locks, and unable to invoke
  repository-local hooks or fsmonitor callbacks. The CLI rejects a `git`
  executable selected from inside the inspected repository, closing Windows
  current-directory executable shadowing. Changed-path diffs additionally
  disable external helpers, text conversion, rename analysis, and submodule
  worktree traversal while retaining changed Gitlinks; missing partial-clone
  objects now fail closed instead of triggering an undeclared fetch.
- Replaced the temporary-file stderr spool used by streaming Git listings and
  blob sessions with a concurrent one-byte-sentinel drain. Corrupt repository
  diagnostics now terminate Git at the fixed diagnostic ceiling instead of
  consuming unbounded temporary-disk space before truncation, and read-only
  verification no longer needs a temporary filesystem.
- Hardened release review and compatibility-alias controls against forged Git
  ancestry through replacement refs, repository-shadowed `git`/`gh`
  executables, alternate GitHub CLI hosts, unbounded command output, and hung
  subprocesses. GitHub recovery, proposal, and alias JSON now receives a
  lexical width/depth preflight before parser allocation, and the final
  ruleset matcher no longer delegates star patterns to Python's backtracking
  `fnmatch` implementation.
- Isolated the externally reviewed semantic-proposal checker in a bounded
  child interpreter with a credential-free environment. GitHub review tokens
  and ambient cloud credentials are no longer present while proposal-owned
  validation code executes.
- Applied the same host-tool boundary to repository hygiene, release-candidate
  verification, reproducible-build timestamp discovery, and the standalone
  compatibility-alias validator. These paths reject repository-local
  executables and disable Git hooks, fsmonitor, replacement refs, lazy fetch,
  optional locks, and interactive credential prompts.
- Rejected redirects on the authenticated GitHub status-writing API client so
  its bearer token cannot be replayed to another origin. Both that gate and
  the composite Action now reject over-wide or over-deep JSON before parser
  allocation while preserving the original complete result artifact.
- Refreshed the exact Python 3.12 slim-trixie image digest and deterministic
  Debian snapshot to the current upstream base while retaining the pinned Git
  package and non-root runtime contract.
- Prevented `review --format plan --summary-file` from overwriting either
  endpoint's config or lock through relative, absolute, normalized, resolved,
  or existing-file aliases. Structural tree validation now also keeps only
  nesting-proportional traversal state, so a wide canonical document cannot
  allocate unbudgeted sibling work before the aggregate work limit rejects it.
  The executable consumer-impact demo and its documentation now assert the
  typed component/external rendering introduced by the source-context UX.
- Made no-op `migrate-lock` runs genuinely non-mutating: current normalized
  locks retain their exact bytes, mode, modification time, and file identity.
  Actual current-schema cleanup is reported truthfully, dry-run follows the
  same decision, and output limits are enforced before any real write.
- Rejected impossible `git_tag_prefix` declarations during dependency-free
  config validation and direct generation, before any Git lookup. The schema
  now documents a bounded literal Git-ref grammar while retaining valid
  Unicode and slash-separated prefixes; valid prefixes with no reachable tag
  remain a distinct source-history diagnostic.
- Made the exported `boundver.load_config` API honor its validation contract.
  It now validates working-tree, head, or index input before returning; maps
  missing files, parse/source failures, and semantic invalidity to exported
  `ConfigError`; and statically checks custom-provider declarations without
  importing or instantiating repository code.
- Bounded configuration, generation, and verification diagnostics before
  rendering to 256 entries and 256 KiB of UTF-8 text, with an 8 KiB per-item
  ceiling. Long repository identifiers are shortened before interpolation,
  diagnostic production stops at the budget, and one deterministic
  `DIAGNOSTICS TRUNCATED` sentinel keeps human, JSON, and GitHub Action results
  explicitly failed instead of exhausting memory or silently omitting errors.
- Rejected explicit slices with an empty `components` array in the public and
  packaged schemas, dependency-free validation, direct generation and
  verification APIs, and config mutation commands. Existing empty slices now
  receive guidance to add a configured component or remove the vacuous gate;
  valid explicit and `closure_of` fingerprints are unchanged.
- Rejected configured component names containing commas or surrounding
  whitespace so every valid name remains addressable through scoped CLI,
  GitHub Action, and GitLab Catalog filters. Validation now gives an actionable
  rename diagnostic across component keys, consumer edges, and slices;
  discovery refuses to emit an unaddressable derived name. `boundver add`
  retains its comma-separated `--paths` input and adds repeatable
  `--boundary-path` values for losslessly declaring filenames that contain a
  comma.
- Made discovery in an unborn repository with an empty index ask Git for its
  bounded non-ignored bootstrap corpus. Root and nested ignore files, negation,
  global excludes, ignored directories, and embedded repositories now follow
  the installed Git exactly; provider inference uses that same filtered set.
  Operational Git failures inside a real repository fail closed, while the
  separate bounded non-Git filesystem approximation is explicitly warned.
- Added a 10-million-step operation-wide glob work budget across built-in
  provider resolution, boundary/behavior validation expansion, and change
  diagnostics. Normalized patterns compile once per operation, every candidate
  transition is charged, matching and non-matching declaration/file
  cross-products fail closed with one actionable error, and literal paths keep
  their direct fast path. The existing per-match limit and accepted selector
  semantics/digests are unchanged.
- Reused one bounded, operation-scoped `git cat-file --batch` transport for
  immutable component hashing, provider extraction, behavior, versions, and
  vendored-copy checks. Full verification no longer starts Git once per
  component or file; snapshot identity, per-read and aggregate byte budgets,
  deterministic digests, and fail-closed protocol validation are unchanged.
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
- Rejected `:` in every repository-relative verification-baseline component,
  preventing an NTFS alternate data stream from masquerading as a tracked
  `.json` baseline during reads, creation, or reviewed replacement.
- Bounded semantic-proposal checker pipe shutdown with one shared deadline.
  A checker descendant can no longer keep inherited output handles open and
  hold the review gate past its declared subprocess budget.
- Rejected control characters, NTFS stream syntax, Windows reserved names,
  trailing dot/space components, and Unicode-normalization or case-folding
  collisions in wheel, source-distribution, and standalone-archive release
  validation. Canonicalization and TestPyPI verification now enforce the same
  portable member-name contract before extraction or publication.
- Replaced CI's inline JUnit failure printer with a bounded, stable-file,
  UTF-8-only reporter. Pytest-controlled names, messages, and bodies can no
  longer inject GitHub runner commands, terminal controls, DTD expansions, or
  unbounded diagnostics into a failed workflow. Streaming XML element, depth,
  attribute, and testcase budgets avoid retaining one attacker-sized tree.
- Neutralized every active repository/worktree Git clean, smudge, and process
  filter before runtime Git inspection. A prepared local Git config can no
  longer make `status`, working-tree diffs, or related ordinary operations
  execute repository-selected commands without the explicit custom-provider
  trust opt-in; filter-name discovery is bounded and fails closed. Submodule
  worktrees are now treated as opaque Gitlinks, closing the same execution path
  through a nested repository's local filter configuration while preserving
  detection of a changed checked-out Gitlink.
- Restored trusted runtime-benchmark repository initialization after Git
  worktree hardening and made process attribution skip the fixed worktree
  options, so the release performance gate once again measures actual Git
  subcommands and enforces its committed process ceilings.
- Removed case-variant duplicate proxy keys from release-workflow environments,
  which GitHub Actions rejects even though ordinary YAML parsers accept them.
  Workflow maintenance tests now enforce GitHub's case-insensitive `env` key
  uniqueness before a workflow reaches hosted validation.
- Made safe release-output preparation canonicalize only the existing working-
  directory or Python temporary-directory prefix. This permits macOS's stable
  `/var` to `/private/var` system alias while continuing to reject every
  symlink, junction, or reparse point in the output-specific suffix.

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

[Unreleased]: https://github.com/yzm1/boundver/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/yzm1/boundver/compare/v0.14.1...v0.15.0
[0.14.1]: https://github.com/yzm1/boundver/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/yzm1/boundver/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/yzm1/boundver/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/yzm1/boundver/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/yzm1/boundver/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/yzm1/boundver/releases/tag/v0.10.0
[0.9.1]: https://github.com/yzm1/boundver/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/yzm1/boundver/releases/tag/v0.9.0
