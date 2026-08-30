# Semantic provider extension system

<!-- semantic-provider-proposal-status: review-ready -->
<!-- semantic-provider-implementation-allowed: false -->
<!-- semantic-provider-v0.15-work-allowed: false -->

| Field | Value |
| --- | --- |
| Proposal | `boundver-semantic-provider-system/v1` |
| Status | Review Ready; implementation is blocked |
| Owners | boundver maintainers |
| Tracking | [#73](https://github.com/yzm1/boundver/issues/73), [#31](https://github.com/yzm1/boundver/issues/31), [#86](https://github.com/yzm1/boundver/issues/86) |
| Threat model | [Semantic provider threat model](semantic-provider-threat-model.md) |
| Machine-readable assurance record | [`spec/semantic-provider-proposal.json`](https://github.com/yzm1/boundver/blob/main/spec/semantic-provider-proposal.json) |

!!! danger "This document does not authorize implementation"

    New semantic-provider code must not be implemented or enabled while the
    machine-readable proposal record says `implementation_allowed: false`.
    Acceptance requires the review and evidence gates in this proposal. The
    existing `custom.*` interface remains explicitly trusted, in-process
    Python; it is not the security model proposed here.

## Decision

Boundver will use three provider classes with deliberately different trust
claims:

1. **Built-ins** remain a small, dependency-light set compiled into Boundver.
2. **Sandboxed extensions** are separately versioned WebAssembly components.
   They run in a one-shot broker process with no ambient filesystem, network,
   environment, clock, random, subprocess, or repository capability. This is
   the only extension class eligible to be first-party, curated, or used by a
   maintained public CI integration.
3. **Legacy trusted-native extensions** are the existing `custom.*` Python
   modules. They execute arbitrary code with the user's authority. They remain
   available for compatibility behind explicit authorization, but cannot be
   described as sandboxed, first-party, curated, or conformant under this RFC.

Python and TypeScript semantic providers will be separately released,
first-party sandboxed extension bundles. “Bundle” does not mean an importable
Python plugin: the executable is a WebAssembly component and is never installed
into Boundver's Python environment. Their parser stacks will not become
Boundver core dependencies. If the sandbox and broker requirements cannot be
met on a supported platform, the semantic provider will not run there;
Boundver must not silently fall back to native or in-process execution.

This is a security boundary, not merely a packaging choice.

## Why a normal Python plugin system is rejected

The current custom-provider loader imports a module selected by repository
configuration after a caller opts in. Import hooks, module top-level code,
constructors, descriptors, `validate_environment`, `validate_config`,
`resolve`, and `explain_diff` can all execute in the Boundver process. Returned
data is bounded, but arbitrary code can still:

- read CI credentials and unrelated files;
- modify the checkout, index, process state, or imported Boundver modules;
- open network connections or spawn children;
- consume unbounded CPU or memory before returning;
- terminate the host with signals, `SystemExit`, or `os._exit`;
- exploit module shadowing, import hooks, or ambient `PYTHONPATH` state; and
- report the same name/version from different code or dependency graphs.

An extra process reduces crash contamination but does not make arbitrary native
code untrusted: it normally inherits the same account's files, network, and
secrets. Entry-point discovery also turns every installed distribution into an
attack surface before an operator has selected it. Therefore this proposal
uses capability-confined artifacts, an external authorization policy, and an
explicit content-addressed store. It does not use automatic Python entry-point
loading.

## Goals

- Extract stable structural API surfaces without executing repository source.
- Preserve Boundver's immutable source-snapshot and fail-closed guarantees.
- Make provider code, parser semantics, artifacts, policy, and output identity
  independently reviewable and replayable.
- Prevent a pull request or checkout from granting execution authority.
- Prevent provider compromise from exposing the normal host process or ambient
  host resources, within the stated sandbox assumptions.
- Bound input, output, CPU, memory, wall time, protocol traffic, and diagnostics.
- Support separately released first-party providers and carefully described
  community providers without conflating conformance with trust.
- Keep every existing lock and provider digest stable unless a documented
  migration deliberately changes the relevant contract axis.

## Non-goals

- Proving source, binary, or behavioral backward compatibility.
- Executing a compiler, build script, package manager, import, or generator from
  the analyzed repository.
- Automatically discovering, downloading, installing, updating, or trusting
  provider code during `generate`, `verify`, `review`, or another analysis.
- Sandboxing arbitrary native Python or executables portably.
- Treating package-index presence, a signature, provenance, a catalog listing,
  popularity, or conformance as sufficient authorization.
- Inferring undeclared files or dependencies from ambient filesystem state.
- Preserving the current in-process Python API as the future SDK.

## Security invariants

The control identifiers below are traced to threats and verification evidence
in `spec/semantic-provider-proposal.json`.

### Authority and activation

- **SPC-001 - Configuration is never authority.** A selected Git source may
  request a logical provider and options. It cannot grant trust, choose an
  executable path, module, URL, registry, publisher, sandbox backend, or
  capability.
- **SPC-002 - Two independent keys are required.** Execution requires both a
  repository request and operator-controlled host policy that pins the exact
  provider artifact digest. Neither is sufficient alone.
- **SPC-003 - No ambient discovery.** Analysis never enumerates or imports
  Python entry points, `sys.path`, `PYTHONPATH`, current-directory modules, PATH
  executables, package-manager state, or a network catalog.
- **SPC-004 - No implicit authorization channel.** Environment variables do
  not enable sandboxed or trusted-native providers. Authorization is an
  explicit command argument or maintained CI input bound to policy evidence.
- **SPC-005 - No install during analysis.** Analysis is network-free and never
  invokes `pip`, `npm`, a shell, a package manager, or an updater.

### Isolation and capabilities

- **SPC-006 - No new in-process extensions.** New extension code never imports
  into the Boundver interpreter. The current Python API is legacy trusted code.
- **SPC-007 - Capability-only guest.** A sandboxed provider is a WebAssembly
  component that imports only the versioned Boundver provider world. It gets no
  WASI filesystem, socket, environment, clock, random, process, terminal, or
  device interfaces.
- **SPC-008 - Separate one-shot broker.** The WebAssembly runtime and component
  execute in a fresh helper process for one request. No component instance,
  memory, import table, or mutable registry is reused across repositories or
  operations.
- **SPC-009 - Defense-in-depth host sandbox.** Where a supported OS offers a
  maintained sandbox backend, the broker additionally runs with a private
  temporary directory, no inherited handles, no credentials, no network, no
  child-process permission, and least privilege. Platform capability is
  reported, never overstated.
- **SPC-010 - No unsafe fallback.** A missing runtime, unavailable hard limit,
  unsupported architecture, or sandbox setup failure is a usage error. It does
  not select a native provider, weaken a limit, or continue partially.

### Source and result integrity

- **SPC-011 - Core owns source selection.** Boundver captures the source
  snapshot and resolves every configured input. A provider receives a bounded
  virtual tree of logical labels, Git mode/type, and bytes; it receives no
  repository root or host path.
- **SPC-012 - Explicit input closure.** Every file available to the provider is
  selected by validated provider configuration and included in semantic config
  identity. A provider cannot request arbitrary additional paths at runtime.
- **SPC-013 - Core owns hashing.** Providers return canonical entries and
  metadata. Boundver validates types, ordering, uniqueness, limits, status, and
  completeness, then performs domain-separated hashing itself.
- **SPC-014 - Partial means failure.** Crash, timeout, cancellation, unsupported
  syntax, missing input, truncated stream, unknown critical field, denied
  capability, protocol error, or budget exhaustion cannot yield a usable
  boundary digest.
- **SPC-015 - Untrusted diagnostics remain data.** Provider errors and metadata
  are bounded, Unicode-validated, terminal/annotation escaped, and cannot
  include source excerpts by default.

### Identity and reproducibility

- **SPC-016 - Complete identity tuple.** Provenance records bind protocol
  version, logical provider ID, digest-contract ID, implementation version,
  parser/grammar identity, artifact SHA-256, publisher policy identity, sandbox
  runtime/backend identity, canonical options, and source snapshot identity.
  SPC-036 defines which axes are semantic, executable, authorization, or
  per-run evidence so portable fingerprints do not absorb host details.
- **SPC-017 - Names do not imply trust.** Namespace and catalog labels are
  identifiers only. Actual trust comes from host policy and verified artifact
  identity.
- **SPC-018 - Deterministic environment.** The provider cannot observe locale,
  timezone, host path, process ID, user, clock, random, network, or unspecified
  environment. Text encoding, path ordering, Unicode treatment, numeric rules,
  and parser configuration are part of the digest contract.
- **SPC-019 - Semantic changes advance an identity.** Any extraction,
  canonicalization, parser, grammar, default-option, protocol interpretation,
  or output-label change that can affect output advances the digest-contract
  ID. An identifier is never reused with new meaning.
- **SPC-020 - Replay is explicit.** Old artifacts remain addressable by digest
  for lock replay unless revoked. Upgrade, rollback, unavailability, and
  revocation are distinguishable states with actionable diagnostics.

### Supply chain and lifecycle

- **SPC-021 - Immutable artifacts only.** Runtime artifacts are prebuilt,
  content-addressed, and verified before every execution. This includes the
  native broker/runtime companion and provider components. Mutable tags,
  package names, versions, URLs, and PATH resolution are never execution
  identity.
- **SPC-022 - Installation is a separate trusted ceremony.** A dedicated
  command may import a locally supplied artifact into an owner-controlled
  content store after verification and confirmation. Verification never
  downloads it and never builds an sdist.
- **SPC-023 - First-party provenance.** First-party artifacts require a
  protected, reviewed, immutable source tag; reproducible build evidence;
  locked dependencies; SBOM and license inventory; vulnerability scan; and
  identity-bound SLSA/PyPI/Sigstore-compatible attestations. The policy pins
  expected issuer and subject, not merely signature validity.
- **SPC-024 - Curation is not authorization.** A signed catalog can aid
  discovery. `listed`, `conformant`, and `first-party` are distinct. Catalog
  data cannot install, authorize, or override a local deny/revocation decision.
- **SPC-025 - Revocation fails closed.** Host policy can deny an artifact,
  publisher, digest contract, or runtime. Cached results from revoked identities
  are unusable. Emergency override is explicit, local, noisy, and auditable.
- **SPC-026 - Secure local store.** The artifact store rejects symlinks,
  hardlink substitution, path traversal, case collisions, alternate data
  streams, non-regular files, ownership/permission failures, concurrent partial
  writes, and digest changes between validation and execution.

### Availability and composition

- **SPC-027 - Every resource is bounded.** Host policy intersects with lower
  global ceilings for input count/bytes, output count/bytes, metadata,
  diagnostics, protocol frames, nesting, guest linear memory, tables, stack,
  fuel/instructions, wall time, CPU, and concurrent workers. A provider cannot
  raise a ceiling.
- **SPC-028 - Protocol parsing is streaming and allocation-safe.** Lengths are
  bounded and overflow-checked before allocation. Unknown critical messages,
  duplicate fields, invalid encodings, trailing data, premature EOF, and
  out-of-order state transitions fail closed.
- **SPC-029 - Cache identity is complete.** Only complete successful results may
  be cached. Keys include the full identity tuple and input contents. Cache
  corruption or ambiguity causes recomputation, never acceptance.
- **SPC-030 - Concurrency does not share trust.** Broker limits are global and
  per-provider; output is associated with an unguessable request identifier;
  concurrent processes cannot write another request's result or artifact.

### Semantic honesty, legacy separation, and governance

- **SPC-031 - Semantic claims are narrow.** Unsupported or ambiguous constructs
  fail closed; provider output is structural evidence, never an automatic
  compatibility verdict. Every ignored and retained construct is normative and
  tested.
- **SPC-032 - Legacy native mode is quarantined.** Legacy `custom.*` execution
  has distinct names, flags, warnings, policy kind, output provenance, and docs.
  It cannot use sandbox trust labels, maintained CI inputs, automatic fallback,
  conformance badges, or first-party/community curation.
- **SPC-033 - Changes require independent review.** Protocol, capability,
  runtime, policy, artifact, trust-label, and digest-semantic changes require
  exact-commit non-author review, a security reviewer, resolved threads, and
  updated threat/evidence records.
- **SPC-034 - Release gates consume exact evidence.** Proposal acceptance and
  full-source bug/issue/security audits are machine-checked against the exact
  reviewed candidate tree before v0.15.0 can be promoted. The external release
  SHA and its GitHub commit tree must match that reviewed tree. An issue,
  paragraph, stale report, self-attestation, or green run for another tree is
  insufficient.
- **SPC-035 - Distribution is legally reviewable.** Every first-party/curated
  artifact and transitive dependency has machine-readable license inventory,
  required notices, source availability where required, and an explicit policy
  disposition before publication.
- **SPC-036 - Identity axes remain separate.** Provider semantic identity,
  executable artifact identity, operator authorization, and runtime/sandbox
  evidence are all bound and compared in their proper records. Runtime/backend
  changes cannot silently redefine semantics and do not rotate portable
  fingerprints merely because hosts differ.
- **SPC-037 - Policy is stable, unambiguous operator state.** Provider IDs and
  policy keys use strict ASCII grammars; policy reads are bounded, duplicate-
  rejecting, no-follow, owner/permission checked, and stable through broker
  launch. Every authorization change is content-addressed and auditable.
- **SPC-038 - Options use a bounded host schema.** Provider options are checked
  by a versioned, offline, non-recursive schema subset with no remote references,
  code hooks, coercion, or data-mutating defaults. Schema complexity and
  diagnostics are bounded; the guest revalidates semantic constraints.
- **SPC-039 - Outputs are data-minimized.** Canonical entries contain only data
  required for the digest contract. Persisted metadata and diagnostics exclude
  source text, secrets, host paths, and unnecessary identifiers; every field
  has a documented retention and disclosure purpose. Raw canonical entries are
  hashed in the coordinator and are not persisted or emitted by default.
- **SPC-040 - Approval evidence is tamper-evident.** Reviews, policy approvals,
  red-team dispositions, conformance results, and release scans bind exact
  commits/artifact digests, reviewer identity and role, timestamp/expiry, tool
  contract, and immutable evidence URL or digest. Stale or contradictory
  evidence cannot satisfy a gate. Proposal review evidence is read from GitHub
  by the bounded authoritative auditor; the manifest path is fixed inside the
  reviewed tree, and a claim embedded in proposal JSON is never evidence of
  its own approval. Review content used as security evidence must not have been
  edited after the reviewed PR was merged. Git and GitHub CLI executables are
  resolved outside the checkout, and the evidence host is fixed to
  `github.com`. Checker code, manifest, schema, CI contract, RFC, and threat
  model are loaded from exact reviewed Git blobs, never from mutable worktree
  bytes after the network audit.
- **SPC-041 - Isolation is not semantic trust.** Provider identity and source
  provenance remain visible with every result. First-party digest contracts
  require source review, reproducible builds, differential and metamorphic
  tests, adversarial logic-bomb fixtures, and conformance evidence. A
  high-assurance mode may require matching canonical output from independently
  implemented providers. Sandboxed, signed, listed, or conformant never means
  semantically correct.
- **SPC-042 - Isolation precedes untrusted decoding.** The coordinator only
  performs bounded byte transport and cryptographic hashing of provider files.
  Manifest, attestation, certificate, provenance, component, and AOT decoding;
  import inspection; validation; compilation; and instantiation occur in a
  verifier/broker process after OS containment and hard limits are active.
  Runtime initialization may occur first only when it consumes no
  provider-controlled bytes or metadata.
- **SPC-043 - v0.15 release authority is external and exact-tree bound.** The
  proposal declares only immutable requirements. A separate release-candidate
  PR supplies two fresh approvals from the distinct external humans named by
  the pinned public reviewer roster; both approvals contain role-specific
  exact markers and independence attestations, and the security approval also
  contains the required scan/platform/publication attestation.
  The bounded auditor obtains that mutable state directly from GitHub twice,
  proves the reviewed PR-head tree equals the externally supplied release
  commit tree, and grants release authority only in memory. Local launch, tag
  creation, pre-tag revalidation, fresh publication, and publication recovery
  all fail closed if this evidence is absent or changes. Review-edit timestamps
  are part of the workflow-owned mutation handoff, and the earliest proposal or
  release-review expiry is rechecked at every tag mutation boundary.
- **SPC-044 - Reviewer authority uses least privilege.** The two reviewer
  identities come from an account-owned public gist, pinned by gist ID, node
  ID, owner account ID, description, and sole filename. Its content is a strict
  versioned record naming one security reviewer and one product reviewer by
  numeric account ID and login, plus the owner's attestation that their
  beneficial ownership is independent. Repository collaborators cannot update
  the owner's gist with repository authority; mutation requires the owner's
  separate user-level Gists authority. The designated humans must have exactly
  GitHub's public read-only repository permission; broader access fails the
  gate. Proposal review never requires repository or tag mutation authority.
  Every snapshot binds the complete file metadata and content, owner-authored
  latest revision, mutable gist timestamps, and the same content fetched by
  immutable revision ID. Both users must be distinct and external to the
  repository owner and PR author, and the roster must predate both
  role-specific exact-head approvals. A roster edit invalidates all older
  approvals.
- **SPC-045 - Repository mutation authority is owner-exclusive.** The
  authoritative proposal and v0.15 release snapshots enumerate every
  repository collaborator through GitHub's [collaborator-list
  endpoint](https://docs.github.com/en/rest/collaborators/collaborators#list-repository-collaborators)
  and accept exactly one principal: repository owner account `22440724` with
  GitHub's canonical `admin` permission record. Any additional collaborator,
  duplicate record, malformed/numeric permission flag, role downgrade, or
  owner identity change fails closed. Because repository workflow credentials
  cannot enumerate every owner-delegated App, OAuth grant, deploy key, or
  separately held credential, reviewer-roster format v2 additionally requires
  the owner-signed literal
  `Owner-exclusive-mutation-authority-attested: true`. It attests that no such
  principal can mutate `main`, version tags, or Releases outside the audited
  workflows. The complete normalized authority and immutable gist revision are
  captured twice and included in every release mutation-handoff digest.
  External reviewers remain ordinary public readers, never collaborators.

## Architecture

```text
untrusted Git source                    operator-controlled host
--------------------                    ------------------------
boundary config request ----+       +-- provider policy (exact digest,
provider request lock -------|-------|   publisher, limits, capabilities)
                             v       v
                       Boundver coordinator
                     (captures one source view,
                    selects and bounds virtual files)
                              |
                       framed BPP/1 request
                              |
                     one-shot broker process
                 (sanitized handles/environment,
                   hard limits, no network)
                              |
              capability-free WebAssembly component
                              |
                    canonical result entries
                              |
                 Boundver validates, hashes, records
```

There are four separate records. Combining them would recreate a confused
deputy:

1. **Repository declaration** says which logical provider contract a component
   needs and which tracked inputs/options are contract-relevant.
2. **Provider request lock** pins exact artifact and semantic identities for
   reproducibility. It is repository data and can be changed by a pull request;
   therefore it requests but does not grant authority.
3. **Host policy** is supplied outside the analyzed source and maps allowed
   identities to exact artifact digests, publisher identities, capabilities,
   ceilings, and revocation state.
4. **Content store** contains immutable verified artifacts addressed by digest.
   It is never searched by name and is not writable by analyzed code.

The effective decision is the strict intersection of all four plus Boundver's
compiled ceilings. Missing or conflicting evidence is an error.

## Repository declaration

The final schema will be versioned; this example is illustrative:

```yaml
providers:
  org.boundver.python.public-api:
    contract: org.boundver.python.public-api/v1
    artifact: sha256:0123456789abcdef...
    inputs:
      - "src/**/*.py"

components:
  sdk:
    path: packages/sdk
    boundary:
      provider: org.boundver.python.public-api
      options:
        export_policy: explicit-all
```

The declaration cannot contain `module`, `class`, `command`, executable path,
package URL, index URL, registry credential, environment, network capability,
or sandbox override. Provider inputs use Boundver's existing path grammar and
source snapshot. Every option is strict-schema validated before execution and
participates in semantic configuration identity.

The exact artifact digest is intentionally reviewable repository data, but it
still requires a matching host-policy authorization. A pull request that swaps
the digest cannot make the new artifact executable merely by updating its own
lock and config.

## Host policy

Host policy must be selected by an explicit CLI/SDK argument or a maintained CI
integration input. It must not be discovered under the repository root or
enabled by an environment variable. The loader must verify that the policy is
a stable regular file from an operator-controlled location and report when its
ownership/permission assurance cannot be established.

Logical IDs, contract IDs, policy keys, and namespace segments use a strict
bounded lowercase ASCII grammar. Unicode confusables, control characters,
case-fold aliases, leading/trailing punctuation, and visually ambiguous trust
prefixes are rejected. Display text is separate from identity.

One policy entry binds:

- logical provider ID and digest-contract ID;
- exact artifact SHA-256;
- accepted publisher issuer and subject/workflow identity;
- accepted build provenance predicate and source repository/ref policy;
- protocol and sandbox runtime range;
- maximum input/output/resource ceilings;
- allowed capabilities, which are empty beyond the provider protocol for v1;
- expiration/review date and revocation state; and
- a human rationale or approval reference.

Policy files are deny-by-default. Wildcards, unbounded version ranges, trust on
first use, publisher-only authorization, and “latest” are invalid in CI mode.
A local interactive policy editor may help create an exact entry, but analysis
never prompts to expand trust.

Policy is parsed with duplicate-key rejection and bounded values, then held as
one captured value for the operation. Its file identity and digest are checked
before and after reading and again before broker launch, or the implementation
must hold an equivalent stable descriptor/handle. Authorization evidence
records that policy digest; a changed policy requires a new decision.

## Broker and runtime distribution

Sandboxed extensions are optional. Boundver core remains dependency-light and
does not import a native WebAssembly runtime into its Python process. The
verifier/broker/runtime is a separately built first-party companion executable
for each supported platform and architecture. Its distribution vehicle is an
implementation ADR, but every vehicle must preserve the same rules:

- it is provisioned during a trusted installation or CI-image build step,
  never downloaded, installed, or updated during repository analysis;
- Boundver receives an operator-controlled absolute path and exact executable
  digest; it never searches the checkout, current directory, PATH, Python
  packages, registry entries, or a network catalog;
- ownership, permissions, regular-file type, publisher/provenance identity,
  SBOM, license, vulnerability, and revocation evidence are checked before use;
- the executable is opened/rehashed with no-follow same-object guarantees and
  launched without a provider-controlled path, argument, loader path, working
  directory, environment, or dynamic-library search location;
- its exact broker, runtime engine, adapter, feature-profile, and sandbox
  backend identities are operation evidence and cache inputs; and
- security-only broker/runtime updates do not rotate portable semantic digests
  when deterministic replay passes, but they do require reviewed runtime
  evidence and cache invalidation.

The maintained Action, container, and other first-party integrations may bundle
that exact companion through their own pinned release process. A plain core
installation without it continues to support built-ins and reports sandboxed
extensions as unavailable. It never installs a fallback or executes a native
provider selected by repository data.

## Identity axes and placement

The complete tuple is not one giant fingerprint. Mixing host/runtime details
into semantic identity would make equal source produce different locks on
Windows, Linux, and macOS and would turn a security-only runtime patch into API
drift. The records are separated as follows:

| Axis | Examples | Persisted in | Changes boundary digest? |
| --- | --- | --- | --- |
| Semantic contract | logical provider ID, digest-contract ID, parser/grammar semantics, canonical options | repository declaration, provider request lock, component lock metadata | Only when canonical result entries change |
| Executable artifact | exact WebAssembly SHA-256, implementation version, manifest/SBOM/provenance digests | provider request lock, host policy, operation provenance | No direct hash input; identity change requires reviewed regeneration even if output is neutral |
| Authorization | policy digest, publisher issuer/subject, approval and revocation state | operator policy and operation provenance | No |
| Execution environment | broker executable digest, WebAssembly runtime, adapter/feature profile, OS sandbox backend/version and effective limits | operator policy, operation/review evidence, cache key | No |
| Source/result | source snapshot, input entry identities, canonical output entries, completeness | operation result and lock | Canonical entries are the boundary hash input |

The provider request lock is repository data and records both semantic
configuration identity and the requested artifact identity so neither kind of
transition can be hidden. They remain separate fields: only the logical digest
contract and canonical options are semantic configuration, while the exact
artifact digest is provenance and validation state. The artifact digest is not
fed into the boundary hash. The portable component lock records both axes;
per-host runtime/backend evidence belongs in a review or operation result, not
the portable lock contract. Cache keys conservatively include every axis.

An implementation must define digest-neutral artifact migration explicitly.
The new artifact is authorized and rerun from source, output equality is shown,
full differential/conformance evidence supports retaining the semantic contract
ID, and lock/config provenance is regenerated without claiming the old
executable produced the new evidence. Equality for one repository is not proof
that two implementations are semantically equivalent for all future inputs.

## Provider option schemas

Every artifact manifest must identify a separately hashed option schema written
in a small BPP-defined data vocabulary; a provider with no options uses the
canonical empty schema. It cannot supply Python validation code, regular
expressions with unbounded engines, recursive references, remote or filesystem
references, custom formats, coercion, or executable defaults.

The host validates shape, count, depth, scalar size, enumeration size, and
unknown fields before broker launch. Defaults that affect semantics are
materialized by the repository declaration contract, not injected differently
by host/provider versions. The guest checks semantic relationships again and
returns bounded error codes. Host and guest disagreement is a provider error,
not a reason to accept one interpretation.

## Artifact format and store

The executable payload is one WebAssembly component. A canonical manifest and
offline verification bundle accompany it but are not executable. These are
ingested as bounded individual regular files, not installed with `pip`, `npm`,
or another package manager and not extracted from a provider-controlled
archive. A Python wheel's `.pth`/import path and installer behavior are outside
the provider trust model. The manifest contains the complete identity tuple,
protocol imports/exports, declared limits, source provenance coordinates,
license/SBOM digests, and payload hash.

Installation accepts only regular files and performs this order:

1. Read under per-file and aggregate bounds without archive extraction.
2. Hash each opaque file and compare it with the exact operator-approved
   digests; no provider-controlled field selects another path or parser.
3. Start a one-shot verifier under the same or stricter containment and limits
   as execution. Only there parse the canonical manifest, attestation,
   certificate chain, provenance, and transparency bundle; verify signature,
   issuer, subject, artifact digest, and predicate; decode the component;
   inspect imports/exports; and enforce a versioned allowlist of WebAssembly
   proposals and instructions. The initial profile rejects threads, atomics,
   shared memory, relaxed SIMD, implementation-dependent floating-point/NaN
   behavior, memory64, experimental component features, and every unreviewed
   host import. The main Boundver process does not load those parsers or
   deserialize provider-supplied AOT state.
4. Supply trust roots and policy as captured host inputs; the verifier has no
   network or ambient trust-store discovery.
5. Accept only the verifier's complete nonce- and digest-bound result over a
   separate bounded protocol; a crash, timeout, or partial result rejects the
   artifact.
6. Write to a private temporary file, flush it, set restrictive permissions,
   and atomically install it under its digest.
7. Reopen without following links and reverify before recording success.

Analysis reopens the stored artifact safely and rehashes it before execution.
Artifact replacement, disappearance, or ambiguous filesystem identity is an
error. The store does not execute post-install hooks and does not contain
source distributions.

## Boundver Provider Protocol v1

`BPP/1` is a binary, length-framed state machine between the coordinator and
broker. It is not shell, JSON lines, pickle, Python marshal, or an inherited
stdio convention. The exact wire schema must be separately specified and
fuzzed before implementation.

Required properties:

- fixed magic and protocol version;
- a random request nonce bound into every broker-envelope message but never
  exposed through the component world or any guest input;
- unsigned fixed-width lengths checked against remaining budgets before reads
  or allocation;
- one canonical metadata encoding with duplicate keys, non-finite numbers,
  oversized integers, invalid Unicode, unknown critical fields, and trailing
  data rejected;
- a manifest-first sequence followed by bounded streamed input entries;
- exactly one terminal complete or error result;
- output entries streamed under host accounting, then sorted/validated by the
  host before hashing;
- bounded concurrently drained diagnostic transport separate from protocol
  output; and
- explicit cancellation, timeout, crash, and unsupported-contract states.

No repository value becomes an argument to a shell or executable command.
The broker executable and component path come from trusted host state; all
repository strings remain framed data.

Canonical result entries and metadata follow data-minimization rules. A
provider cannot persist its virtual input, source excerpts, full AST, host
diagnostics, or arbitrary opaque blobs merely because they fit the byte cap.
The contract must name the purpose and disclosure behavior of every metadata
field; undeclared fields are rejected. The coordinator hashes validated raw
entries and does not persist or print them by default. This limits accidental
disclosure but cannot make an authorized provider's digest a zero-capacity
channel: authorization necessarily grants the provider read access to the
selected virtual files and permits bounded derived output.

## Virtual source interface

The coordinator resolves configured inputs before starting the provider. Each
virtual entry has:

- a component-relative logical label;
- canonical Git mode and object type;
- exact source bytes from the captured `head`, `index`, or `working-tree`
  snapshot; and
- a host-computed content digest for transport verification.

The guest cannot enumerate the host filesystem or request an undeclared path.
For languages that need import/re-export closure, users must declare a broad
enough bounded input set. Missing required modules produce an unsupported or
error result; the provider must not consult installed packages, the network,
or host import resolution.

The transport request nonce, wall-time observations, host source commit ID,
policy metadata unrelated to semantics, and other per-run values are broker
state, not virtual source and not component inputs.

Symlink entries remain symlink target data and are never dereferenced. Working
tree instability is detected by the existing source accessor before bytes are
sent. Provider options cannot alter source mode or escape the component.

## Sandbox contract

The guest imports only the BPP component-model world. No general WASI world is
linked. In particular, the guest has no ambient:

- filesystem or preopened directory;
- socket, DNS, HTTP, or other network interface;
- environment or command arguments beyond the canonical request;
- wall/monotonic clock, timezone, locale, random source, or terminal;
- subprocess, thread, shared memory, device, or dynamic library loader; or
- host callback that accepts arbitrary paths or URLs.

The broker enforces guest memory, table, stack, and fuel limits and has an
independent parent-enforced wall timeout. The parent drains protocol and
diagnostics concurrently under byte caps to avoid pipe deadlock. A killed or
wedged broker cannot leave a valid result.

WebAssembly reduces guest authority; it does not make the runtime infallible.
The broker is therefore a separate process, receives no normal host secrets,
and uses OS containment where available. Sandbox strength is reported as
evidence with an exact backend/runtime identity. A platform that cannot meet
the minimum v1 isolation and resource contract rejects extension execution.
Containment and parent-enforced limits must be active before any
provider-controlled component byte, manifest field, cached compiled module, or
adapter is decoded by the runtime. Pre-initializing trusted runtime machinery
is allowed only when it cannot consume provider input.

## Determinism contract

A provider digest contract must specify all of the following:

- accepted language/specification and parser grammar versions;
- decoding, BOM, newline, Unicode, identifier, and path rules;
- canonical ordering and duplicate handling;
- numeric and string normalization;
- conditional construct treatment;
- unsupported syntax and ambiguity behavior;
- selection, import/re-export, and declaration-file rules;
- which documentation/formatting changes are ignored;
- which semantic structures are retained;
- default options and every option that changes extraction; and
- canonical output labels and bytes.

The provider must fail closed on syntax or semantics outside that declared
subset. It cannot evaluate host-specific conditions or execute source to guess
an answer. The runtime enables only the reviewed deterministic WebAssembly
feature profile and canonicalizes any permitted floating-point edge case; the
initial profile may forbid floating-point instructions entirely. The same
artifact and inputs must return byte-identical canonical entries on every
supported OS, architecture, Python host version, and repeated run. A
nondeterministic result is a provider defect and invalidates its conformance
status.

## Python semantic provider

“Python” names the language being analyzed, not the implementation language of
the component. The bundle does not contain or install a Python package or start
CPython. The same rule applies to the TypeScript provider and Node.js.

The Python provider must parse only supplied source bytes; it must never import
the package. Its v1 contract must explicitly decide and test:

- `__all__`, implicit public names, aliases, imports, and star re-exports;
- annotations, defaults, positional/keyword-only parameters, overloads,
  protocols, dataclasses, properties, and type aliases;
- relative imports and package/module identity in the virtual tree;
- `if TYPE_CHECKING`, version/platform conditions, `try` imports, and optional
  dependencies;
- dynamic `__getattr__`, metaclasses, decorators, generated members, extension
  modules, and other runtime-defined surfaces; and
- grammar differences across supported Python versions.

The safe default for constructs that can change the public surface but cannot
be resolved statically is `unsupported`, not omission. A provider may offer a
narrower explicitly named contract with clear exclusions, but its name and
documentation must not imply a complete runtime API.

## TypeScript semantic provider

The TypeScript provider must parse only supplied virtual files and strict
options; it must not run Node, package scripts, declaration emit, or repository
plugins. Its v1 contract must explicitly decide and test:

- value and type exports, aliases, default exports, namespaces, ambient
  declarations, and wildcard re-exports;
- overloads, generics, conditional/mapped types, declaration merging, and
  JSDoc-derived types;
- module resolution, package `exports`, `types`, `typesVersions`, path aliases,
  project references, and declaration files;
- compiler-option defaults and TypeScript grammar/version changes; and
- JavaScript interop or unsupported source kinds.

If faithful extraction requires compiler behavior that cannot run within the
virtual capability model, the provider must narrow its declared contract or
remain unshipped. It must not silently access the checkout or installed npm
tree.

## Compatibility claims

Semantic providers canonicalize a declared structural surface. A stable digest
means that representation did not change; a changed digest means it did. It
does not prove that unchanged behavior is compatible, nor that every changed
surface is breaking.

Documentation and output must use “structural surface” or “semantic boundary,”
not “compatible,” “safe,” or “breaking,” unless a separate ecosystem checker
provides that evidence. Python and TypeScript language/compiler tools remain
authoritative for their own compatibility rules.

## Provider identity and lock migration

The current `boundary_provider` and `boundary_provider_version` fields are not
enough for extensions. A future lock contract must bind the portable semantic
and requested-artifact axes without changing existing v3 digest meanings by
accident. Host policy and runtime/sandbox evidence remain in the operation
result rather than the portable lock. The design must specify exact fields and
migration before code lands.

Migration rules:

- existing built-in and raw-provider locks remain valid under their current
  contracts;
- `custom.*` locks remain legacy trusted-native and are never silently relabelled
  as sandboxed;
- changing provider class, artifact digest, parser identity, digest contract,
  or semantic options is visible in lock/config metadata;
- if canonical output remains equivalent, migration tooling may report digest
  neutrality, but must still require reviewed identity regeneration; and
- unknown or unavailable historical artifacts cannot be “verified” by a newer
  implementation claiming the same version.

## Caching

Provider caching is deferred until the identity and execution model is proven.
When added, a key must include the full identity tuple, canonical options,
every virtual input label/mode/type/content digest, source identity, policy
identity, runtime/backend identity where relevant, and Boundver cache-contract
version. Only complete successful output can be cached.

Cache entries are untrusted data: validate them exactly like broker output,
bind them to the originating repository trust domain, store atomically, and
discard on corruption, revocation, policy change, or version ambiguity.

## CI integrations

Maintained GitHub Action and GitLab Catalog integrations keep extension
execution off by default. They must not expose the legacy native-provider opt-in.

Sandboxed execution requires all of:

- an explicit workflow input controlled by trusted workflow code;
- a provider request lock in the analyzed source;
- an operator/maintainer policy supplied outside that source or embedded in the
  immutable integration release;
- preinstalled exact artifacts or a separate, trusted setup step; and
- result output that identifies provider, policy, artifact, sandbox, source,
  and completeness.

Fork/PR tests must inject fake secrets and hostile provider/config changes and
prove no secret, network, filesystem write, process spawn, policy expansion,
or native fallback occurs. Workflow changes are code changes and remain under
normal repository review controls; Boundver cannot make a workflow that runs
arbitrary commands safe.

## Curation and governance

The public catalog, if created, uses these non-overlapping labels:

- **First-party:** released and incident-managed by Boundver under the complete
  first-party supply-chain policy.
- **Conformant:** passed a named conformance-suite version for an exact artifact
  digest. This says nothing about publisher trust, absence of malicious code,
  or semantic correctness outside the tested corpus.
- **Listed:** metadata was accepted for discovery. No security or quality claim.
- **Revoked:** must not execute under normal policy, even if previously listed
  or conformant.

Catalog submissions require immutable source and artifact coordinates, license,
maintainer contacts, security policy, supported contract versions, and exact
conformance evidence. Boundver must not mirror arbitrary executable artifacts,
accept paid trust labels, auto-promote popular packages, or let a catalog entry
grant local authority.

First-party provider changes that can affect canonical output require:

- a digest-contract design review;
- threat-model update;
- two non-author reviews, including one security-focused reviewer;
- green conformance, fuzz, determinism, sandbox-canary, supply-chain, and full
  Boundver integration tests on the exact commit/artifact;
- explicit lock and migration notes; and
- coordinated docs, changelog, revocation, rollback, and release evidence.

Because a deterministic component can deliberately emit a plausible but false
surface, the result always identifies the exact provider and is evidence from
that provider, not a Boundver correctness attestation. High-assurance users may
configure an independently implemented provider quorum; disagreement is an
error, never a majority vote or silent fallback.

## Assurance and test gates

No finite test suite proves arbitrary code or a runtime free of vulnerabilities.
“Ironclad” therefore means explicit authority, minimized capabilities, complete
identity, defense in depth, fail-closed behavior, adversarial evidence, and an
honest residual-risk statement—not a claim of perfect security.

### Proposal acceptance gate

The proposal can move from Draft to Accepted only when:

1. The RFC, threat model, and machine-readable traceability record agree and
   pass `python -I scripts/check_semantic_provider_proposal.py`. Exact status,
   implementation-authority, and v0.15-authority markers in both human
   documents must match the manifest.
2. Every in-scope threat has preventive and detective/recovery controls plus a
   named verification plan; critical/high threats have defense in depth.
3. Red-team findings are closed or explicitly accepted with owner, rationale,
   expiry, and compensating controls. No Critical, High, or Medium finding may
   remain accepted for the initial design.
4. The account-owned public gist at
   `https://gist.github.com/yzm1/0caedb798d168b974f9d9fb63c377f73`
   names exactly one security and one product reviewer by numeric account ID
   and login. The gist is pinned by ID, node ID, owner, description, and sole
   filename. It must remain public, untruncated, UTF-8 text and contain exactly
   one canonical roster file. The latest revision must identify owner account
   `22440724`, and fetching that immutable revision must reproduce the current
   file and metadata exactly. The owner attests that the two reviewer accounts
   have independent beneficial owners and includes
   `Owner-exclusive-mutation-authority-attested: true`. The second attestation
   covers non-enumerable owner delegations; a false attestation is an explicit
   owner root-of-trust failure. Each reviewer must have the exact public
   read-only repository permission; `triage`, `write`, `maintain`, or `admin`
   access fails the gate. Neither reviewer receives repository or tag mutation
   authority. The repository owner, PR author, bots, and duplicate identities
   cannot count. Each designated human approves the exact reviewed head after
   the roster's latest update. The canonical roster body is:

   ```text
   semantic-provider-review-roster/v2
   Repository-id: 1226008327
   Repository-owner-id: 22440724
   Security-reviewer: <numeric-account-id>:<login>
   Product-reviewer: <numeric-account-id>:<login>
   Independent-beneficial-owners-attested: true
   Owner-exclusive-mutation-authority-attested: true
   Attested-by: 22440724:yzm1
   ```

   The security review body contains exactly these meaningful lines:

   ```text
   semantic-provider-security-review/v1
   Reviewed-commit: <full 40-character reviewed-head SHA>
   Independent-reviewer: confirmed
   Verdict: approved
   ```

   The product review body contains exactly:

   ```text
   semantic-provider-product-review/v1
   Reviewed-commit: <full 40-character reviewed-head SHA>
   Independent-reviewer: confirmed
   Verdict: approved
   ```

   Every review thread is resolved and no user or team review request remains.
   An aggregate `CHANGES_REQUESTED` or `REVIEW_REQUIRED` state blocks the gate;
   `null` is accepted because read-only external approvals do not necessarily
   participate in repository branch-protection counts. Counted approvals are no
   more than 90 days old, are not future-dated, postdate their reviewer-roster
   configuration, and precede the merge.
   A counted review edited after the merge does not qualify, including a
   security marker added to an older approval.
5. Live repository controls leave no non-owner user, App, deploy key, or other
   principal able to push `main`, create a protected release tag, or create a
   GitHub Release outside the audited path. The machine gate proves enumerable
   collaborator state and binds the owner-signed v2 attestation for
   non-enumerable delegations. On this personal repository, the minimum live
   closure is removal of all non-owner write collaborators and revocation of
   every other non-owner mutation grant. A more elaborate ruleset/App design
   must prove equivalent exclusivity before it can replace that closure.
   Owner-account compromise or a knowingly false owner attestation remains an
   explicit trust root; non-owner mutation authority does not.
6. Documentation builds strictly and repository hygiene, lint, tests, schemas,
   and existing release-contract checks remain green. The adversarial gate
   suite retains at least 75% combined branch coverage of the authoritative
   auditor and structural checker; coverage is a regression floor, not proof
   of correctness.
7. The tracking issues link the accepted exact commit and retain the rollout
   dependencies.

The manifest declares these requirements but cannot attest to reviews that are
created after the commit. After the acceptance PR is squash-merged to `main`,
the authoritative command is:

```console
python -I scripts/audit_semantic_provider_proposal.py --gate accepted
```

The auditor reads only the canonical
`spec/semantic-provider-proposal.json` inside the checkout and finds the latest
commit touching the governed proposal surface,
requires exactly one associated merged PR, and compares the complete reviewed
head tree with the merge-result tree and the local tree. The record commit must
also remain an ancestor of GitHub's current canonical `main`; a stale “merged”
PR after history rewrite is insufficient. The textual repository owner/name,
immutable GitHub repository ID, and immutable owner account ID must all match
the accepted record; transfer or namespace recreation requires re-review. It queries the
version-pinned GitHub APIs (REST `2022-11-28`, whose documented support ends
2028-03-10) under hard per-response, aggregate-byte, request, record,
pagination, reviewer, integer, command, and 90-second total limits; rejects
floating-point and non-finite evidence numbers; checks the pinned public gist,
its owner-authored immutable latest revision, complete normalized file, both
exact read-only permissions, and its update time; evaluates latest review
states; and requires two identical normalized snapshots to narrow API race
windows. REST review identities are cross-bound to GraphQL edit timestamps so
post-merge review-body edits cannot create approval evidence. It emits reviewer
names and a snapshot digest, never review bodies.

Bare executable lookup and ambient GitHub-host selection are not trusted. The
auditor resolves regular `git` and `gh` tool files outside the repository before
use and supplies `--hostname github.com`; a repository-local executable or
redirected enterprise/API host cannot provide evidence.

Before contacting GitHub, the auditor identifies the reviewed record commit and
materializes every structural-checker input from that commit's exact Git blobs
into a private temporary tree. Only regular Git file modes are accepted;
symlinks, submodules, missing paths, and ambiguous entries fail. The later
validation executes and reads only that snapshot. Concurrent worktree edits
therefore cannot replace the checker, manifest, schema, CI hook, RFC, or threat
model between identity proof and use.

Gate code has a two-phase bootstrap rule. A PR may introduce or revise the
auditor, checker, CI hook, local release launcher, release-tag workflow, or
publication workflow only while implementation remains blocked. A later
acceptance commit must preserve those six bootstrap blobs exactly from its
first parent, and that parent must equal GitHub's recorded reviewed-PR base
commit; otherwise the authoritative audit fails. This proves the comparison is
against pre-PR gate code rather than an earlier commit hidden in a rebase. Any
gate-code revision therefore requires a blocked gate-change PR followed by a
separate reviewed acceptance PR. The acceptance review binds the resulting
complete tree, not just the small declaration diff.

The GitHub credential needs only repository Metadata, Contents, Issues, and
Pull requests read access. GitHub documents that public gists are anonymously
readable while mutation requires separate user-level Gists write permission.
The workflow reads the two fixed `api.github.com` gist paths anonymously over
verified HTTPS, without redirects or an authorization header; repository API
calls use only `github.token`. The allowed
[`GITHUB_TOKEN` permission set](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idpermissions)
has no Gists permission, and the governed tag and publication workflows may not
reference repository or Environment secrets. A Gists-write PAT, GitHub App user
token, OAuth token, or equivalent credential must never be stored in this
repository's Actions secrets, environments, variables, artifacts, caches, or
runners. The audit is a read-only observation and must run from a trusted, clean
control environment; it does not make a compromised local Git, `gh`, Python
runtime, GitHub account, user token, or GitHub service trustworthy.

### Implementation gate

Before merging an executable SDK/broker:

- publish the exact BPP wire, component-world, artifact-manifest,
  operator-policy, content-store, and deterministic WebAssembly feature-profile
  specifications;
- select and review the companion-broker packaging ADR; prove absolute-path,
  digest, ownership/permission, dynamic-loader, update, rollback, revocation,
  and no-analysis-install behavior on every supported platform;
- implement a non-semantic reference component and hostile component corpus;
- pass protocol parser/serializer fuzzing and property tests;
- pass capability canaries for filesystem, environment, network, clock,
  random, process, thread, and host mutation;
- pass wall/CPU/fuel/memory/stack/table/input/output/diagnostic limit tests;
- pass crash, cancellation, truncation, deadlock, malformed frame, integer
  overflow, replay, downgrade, TOCTOU, concurrency, and cleanup tests;
- pass deterministic golden-corpus replay on every supported OS, architecture,
  and Python host version;
- pass artifact tampering, attestation identity, revocation, rollback, offline,
  install-store, and dependency-compromise tests;
- pass public Action/GitLab fork and fake-secret tests; and
- receive another exact-commit security review with no unresolved Critical,
  High, or Medium findings.

### Provider publication gate

Each Python/TypeScript provider additionally requires:

- a normative extraction contract and unsupported-case table;
- differential testing against the pinned authoritative parser/compiler where
  applicable;
- metamorphic tests proving ignored edits stay stable and retained semantic
  edits rotate output;
- parser fuzzing and adversarial complexity corpora;
- cross-version/cross-platform golden fixtures;
- reproducible artifacts, SBOM, licenses, locked dependencies, vulnerability
  scans, and verified provenance; and
- upgrade, rollback, revocation, and lock-migration demonstrations.

### v0.15 release gate

This proposal does not authorize a v0.15 release. Before v0.15.0 promotion, the
entire then-current source, tests, scripts, workflows, Action, container,
schemas, docs, packaging, and release automation must receive fresh bug, issue,
and security scans. Every finding must be triaged, release blockers closed, and
the exact candidate rerun through full supported-platform and publication
gates.

The release candidate must be the merge result of a separate PR into `main`.
Its reviewed head tree must be byte-identical to the release commit tree, and
the release commit's first parent must be the PR's recorded base commit. Within
14 days before promotion, the same two distinct external humans designated by
the pinned account-owned gist roster must approve that exact PR head.
Their repository permission must still be exactly read-only and must not grant
repository or tag mutation authority. All threads must be resolved, no review requests may remain, and the
latest decisive review from each designated reviewer must still be an approval.
The security review body must contain exactly these non-empty lines, in order:

```text
semantic-provider-v0.15-release-review/v1
Reviewed-commit: <release PR head SHA>
Independent-reviewer: confirmed
Full-source-bug-scan: passed
Full-issue-audit: passed
Full-security-scan: passed
All-blockers: closed
Supported-platforms: passed
Publication-gates: passed
Verdict: approved
```

The product review body must contain exactly:

```text
semantic-provider-v0.15-product-review/v1
Reviewed-commit: <release PR head SHA>
Independent-reviewer: confirmed
Verdict: approved
```

`scripts/audit_semantic_provider_proposal.py --gate v0.15-release` accepts the
release tag and SHA as external inputs, captures two identical bounded GitHub
snapshots, and proves the tree/ancestry/review bindings. It never accepts scan
booleans, reviewer names, or a candidate SHA embedded in the proposal manifest.
The local release launcher enforces this gate, the tag workflow enforces it
before candidate verification and again immediately before mutation authority,
and the publish workflow enforces it for both fresh and resumed publication.
Removing or weakening those calls is itself a governed bootstrap change that
invalidates proposal acceptance. The read-only tag job hands the minimum of the
proposal-review and release-review validity windows to the write-token job;
that job also recomputes a workflow-owned digest containing review bodies,
latest states, edit timestamps, the gist roster and reviewer permissions,
roles, requests, and threads before each tag mutation boundary. A roster change,
identical-text post-audit edit, or review crossing its freshness deadline
therefore aborts promotion. The final check reserves a five-minute safety
margin and the tag push has a 60-second hard timeout, so the approval cannot
cross its deadline during an unbounded network operation.

## Rollout

1. **RFC only:** accept this design and threat model. No extension execution.
2. **Broker laboratory:** ship no user-facing feature; build BPP, a no-op
   reference component, hostile corpus, and sandbox evidence.
3. **Experimental first-party Python provider:** explicit opt-in, no public CI
   default, one digest contract, rollback available.
4. **Experimental TypeScript provider:** separate artifact and release cadence;
   no coupling to Python provider availability.
5. **Stable first-party providers:** only after at least one release cycle of
   field evidence and a repeated security review.
6. **Community catalog:** only after the SDK, revocation, conformance, and
   incident processes have operated successfully for first-party providers.

No phase starts automatically. Failure to meet a gate keeps the previous phase
supported and the next phase blocked.

## Rejected alternatives

### Put Python and TypeScript parsers in core

This simplifies authority but couples Boundver's small core to large,
fast-moving parser stacks and their release/security cadence. It also broadens
the core attack surface for users who do not select those providers.

### Use Python entry points

Entry points are ambient metadata discovered through Python's import machinery.
Loading one imports arbitrary in-process code. Distribution metadata and module
resolution can also vary with `sys.path`, import hooks, and environment. This
violates explicit authority, isolation, and complete identity.

### Run an arbitrary executable in a subprocess

A subprocess normally shares the user's filesystem, network, credentials, and
kernel authority. It limits accidental interpreter corruption but is not a
sandbox and cannot be the community-provider security boundary.

### Trust signed or attested packages automatically

A signature proves a key or workload identity signed bytes; provenance records
claims about a build. Neither proves the code is benign, correct, deterministic,
or appropriate for this repository. Identity evidence is required but remains
subordinate to explicit host policy and sandboxing.

### Let the repository declare capabilities

The analyzed source is controlled by the party whose changes are being checked.
Allowing it to request network, host paths, environment, or native execution
would let a pull request expand its own authority. BPP/1 therefore has no such
capabilities.

### Treat unsupported constructs as absent

Silent omission creates a stable digest for an incomplete API and is more
dangerous than a failed check. Unsupported or ambiguous semantic constructs
must fail closed or use a narrower, honestly named provider contract.

## Standards and design references

- [Python entry-point and distribution discovery](https://docs.python.org/3/library/importlib.metadata.html)
- [Python index-hosted attestations](https://packaging.python.org/en/latest/specifications/index-hosted-attestations/)
- [PyPI attestation security model](https://docs.pypi.org/attestations/security-model/)
- [SLSA build track](https://slsa.dev/spec/v1.2/build-track-basics)
- [Sigstore identity-bound verification](https://docs.sigstore.dev/cosign/verifying/verify/)
- [Wheel artifact hash records](https://peps.python.org/pep-0427/)
- [WASI capability-based design principles](https://github.com/WebAssembly/WASI/blob/main/docs/DesignPrinciples.md)
- [WebAssembly security model](https://github.com/WebAssembly/design/blob/main/Security.md)
- [Windows AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)
- [GitHub REST API versioning](https://docs.github.com/en/rest/about-the-rest-api/api-versions)
- [GitHub pull-request review records](https://docs.github.com/en/rest/pulls/reviews)
- [GitHub commit-to-pull-request association](https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit)
- [GitHub gist metadata](https://docs.github.com/en/rest/gists/gists#get-a-gist)
- [GitHub immutable gist revisions](https://docs.github.com/en/rest/gists/gists#get-a-gist-revision)
- [GitHub gist update authority](https://docs.github.com/en/rest/gists/gists#update-a-gist)
- [GitHub issue editing authority](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/editing-an-issue)
- [Personal-repository permission levels](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/repository-access-and-collaboration/permission-levels-for-a-personal-account-repository)
- [Repository collaborator listing](https://docs.github.com/en/rest/collaborators/collaborators#list-repository-collaborators)
- [Repository permission lookup](https://docs.github.com/en/rest/collaborators/collaborators#get-repository-permissions-for-a-user)
- [GitHub GraphQL pull-request review state](https://docs.github.com/en/graphql/reference/objects#pullrequest)

These references inform the design; none is imported wholesale as a security
claim. Boundver's exact protocol, policy, sandbox, and verification contracts
must remain versioned and testable in this repository.
