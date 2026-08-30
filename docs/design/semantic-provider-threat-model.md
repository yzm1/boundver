# Semantic provider threat model and red-team record

<!-- semantic-provider-proposal-status: review-ready -->
<!-- semantic-provider-implementation-allowed: false -->
<!-- semantic-provider-v0.15-work-allowed: false -->

| Field | Value |
| --- | --- |
| Proposal | `boundver-semantic-provider-system/v1` |
| Status | Review Ready; independent review pending |
| Parent RFC | [Semantic provider extension system](semantic-provider-rfc.md) |
| Machine traceability | [`spec/semantic-provider-proposal.json`](https://github.com/yzm1/boundver/blob/main/spec/semantic-provider-proposal.json) |
| Last full internal red-team pass | 2026-08-30; not independently accepted |

This document assumes that repository contents, provider inputs, provider
artifacts, package metadata, protocol messages, caches, and diagnostics can be
hostile. A provider's publisher may be honest, compromised, or malicious. A
valid signature proves identity, not benign behavior.

## Assets

The system must protect:

- CI and developer credentials, tokens, environment, home directory, SSH/GPG
  agents, cloud metadata, and unrelated repositories;
- the analyzed checkout, Git object database, index, lock, config, and outputs;
- Boundver coordinator control flow, imported modules, process state, and
  source-snapshot invariants;
- the correctness, completeness, determinism, and provenance of fingerprints,
  review plans, caches, and diagnostics;
- availability of local and hosted verification within declared budgets;
- provider/publisher namespace integrity and the meaning of trust labels; and
- rollback, revocation, incident response, and historical replay evidence.

## Trust boundaries

1. **Git source boundary:** a pull request can change every tracked byte,
   including provider requests, options, paths, docs, workflows, and locks.
2. **Operator policy boundary:** policy is outside the analyzed source and is
   the only authority to execute an extension artifact.
3. **Artifact boundary:** a stored provider and its metadata remain untrusted
   until exact digest, provenance identity, component shape, and policy are
   verified.
4. **Coordinator/broker boundary:** BPP frames and all provider output are
   untrusted, length-delimited data.
5. **Broker/guest boundary:** the WebAssembly runtime enforces the guest's
   capability and memory model; the broker process limits blast radius if that
   boundary fails.
6. **Host/OS boundary:** an OS sandbox is defense in depth. Its availability and
   actual strength differ by platform and must be reported rather than assumed.
7. **Catalog boundary:** catalog metadata is discovery information, never
   execution authority.

## Adversaries

- A pull-request author who controls repository data but not trusted workflow
  policy.
- A malicious repository owner convincing a developer to run Boundver.
- A typosquatter, dependency-confusion attacker, or malicious provider author.
- A compromised first-party/community maintainer, CI workflow, package index,
  artifact mirror, transparency service, or signing identity.
- A provider that begins benign and is later replaced, revoked, or compromised.
- Hostile source files designed to exploit parsers or cause algorithmic denial
  of service.
- Another concurrent local/CI job attempting store, cache, pipe, or result
  confusion.
- An attacker who can modify user-writable package/import paths or provider
  storage between checks.

## Assumptions and exclusions

The design does not claim to withstand:

- a compromised OS kernel, hypervisor, CPU, Boundver coordinator, or sandbox
  runtime already executing arbitrary host code;
- an operator who deliberately authorizes and runs a legacy trusted-native
  provider under the same account as valuable secrets;
- compromise of every independent review, policy, release, provenance, and
  runtime trust root at once; or
- all microarchitectural side channels. The guest receives no secrets and
  deterministic interfaces, which reduces useful side-channel material.

These exclusions cannot be used to waive ordinary parser, protocol, runtime,
or supply-chain hardening.

## Red-team methods

The review is repeated through independent lenses so one taxonomy does not
define the blind spots:

- **STRIDE:** spoofing, tampering, repudiation, information disclosure, denial
  of service, and elevation of privilege at every trust boundary.
- **Confused-deputy analysis:** every repository-controlled value is followed
  to filesystem, network, process, policy, source, and output authority.
- **Supply-chain lifecycle:** authoring, review, build, signing, publication,
  cataloging, acquisition, installation, execution, caching, update, rollback,
  revocation, incident response, and historical replay.
- **Parser/protocol adversarial analysis:** complexity attacks, malformed state,
  partial transport, allocation-before-validation, differential semantics, and
  untrusted diagnostics.
- **Composition analysis:** Git source modes, locks, semantic config, Action and
  GitLab workflows, artifact stores, caches, concurrent repositories, and
  release/recovery paths are reviewed together rather than as isolated tools.
- **Cross-platform analysis:** Windows, Linux, macOS, architectures, filesystem
  rules, process isolation, and runtime identity are compared explicitly.
- **Human-factors analysis:** trust labels, confusable names, approval fatigue,
  warnings, defaults, recovery pressure, and compatibility wording are treated
  as security surfaces.
- **Fault and temporal analysis:** crashes, cancellation, races, replacement,
  stale evidence, expiry, compromise after approval, and unavailable historical
  artifacts are exercised as state transitions.

Each material finding must change the proposal, add a traced control/test, or
remain an explicit blocking finding. “Documented” without prevention,
detection/recovery, and verification is not a disposition.

## Red-team findings and dispositions

These are design findings, not claims that the current documented trusted-code
interface is vulnerable under a different promise. They explain why it cannot
be promoted into the semantic-provider SDK.

### RTF-001: import occurs before provider identity is trusted - Critical

The repository declares module/class strings. Once global opt-in is present,
module top-level code and constructors run before runtime name/version checks.
An identity mismatch therefore detects the wrong provider only after code has
already executed.

**Disposition:** closed by SPC-001 through SPC-006. New extensions are
content-addressed components, not imported Python. Host policy authorizes the
artifact before broker creation.

### RTF-002: authorization can be inherited accidentally - Critical

`BOUNDVER_ALLOW_CUSTOM_PROVIDERS` enables the legacy loader for every relevant
CLI invocation in the environment. A broad CI/job-level variable can make a
later untrusted checkout executable.

**Disposition:** closed for the new system by SPC-002 and SPC-004. The RFC also
requires legacy mode to remain visibly separate; removal of the environment
fallback is an implementation/migration decision that must be handled without
breaking a published contract silently.

### RTF-003: subprocess isolation is not a sandbox - Critical

A native helper normally inherits filesystem, network, user credentials,
handles, and kernel APIs. Sanitizing environment variables alone does not stop
credential or file access.

**Disposition:** closed by SPC-007 through SPC-010. Native helpers are not
eligible for curated/community execution. The WASM guest receives no ambient
WASI capabilities, and the runtime is isolated in a one-shot broker.

### RTF-004: WebAssembly runtime escape remains possible - Critical

A malicious parser/component can target the runtime or broker. Capability-safe
guest semantics do not prove a native runtime free of memory-safety defects.

**Disposition:** mitigated, not erased, by SPC-008, SPC-009, SPC-021, SPC-023,
SPC-027, fuzzing, runtime pinning, vulnerability response, and no secrets in the
broker. This is a residual risk requiring repeated runtime security review.

### RTF-005: repository pins can still request attacker bytes - Critical

A pull request can update a provider artifact digest and lock together. A
content digest proves consistency, not authorization.

**Disposition:** closed by SPC-001, SPC-002, and the four-record architecture.
The host policy must independently authorize the same exact digest.

### RTF-006: attestations can be mistaken for approval - High

An artifact can be correctly signed and built by a malicious or compromised
publisher. SLSA provenance and PyPI attestations bind identity and build facts;
they do not prove semantics or safety.

**Disposition:** closed by SPC-002, SPC-023, and SPC-024. Provenance is required
evidence subordinate to policy, sandboxing, review, and tests.

### RTF-007: check/use races can replace stored artifacts - Critical

Rehashing a path and then launching by path permits symlink, rename, hardlink,
or concurrent-writer substitution if file identity is not held through use.

**Disposition:** closed by SPC-021, SPC-026, and an implementation gate that
requires descriptor/handle-based identity or an equivalent no-follow,
same-object launch mechanism on every platform.

### RTF-008: static semantic extraction can silently omit APIs - High

Python and TypeScript have dynamic, conditional, generated, and
configuration-dependent public surfaces. A parser that ignores unresolved
constructs can produce a reassuring but incomplete stable digest.

**Disposition:** closed by SPC-014, SPC-018, SPC-019, the language-specific
contracts, and `unsupported` as the safe result. Compatibility is never claimed.

### RTF-009: framed protocols can be memory bombs - High

An attacker can send huge lengths, integer overflows, nested values, duplicate
fields, compressed bombs, endless diagnostics, partial frames, or valid frames
in an invalid state order.

**Disposition:** closed by SPC-027, SPC-028, no compression/pickle, streaming
parsers, parent timeouts, and protocol fuzz/property tests.

### RTF-010: partial output can be mistaken for success - High

A provider can emit valid entries and then crash, time out, or omit a terminal
frame. Hashing entries as they arrive would bless a prefix.

**Disposition:** closed by SPC-013, SPC-014, and the terminal-complete protocol
state. The host hashes/publishes only after complete validation.

### RTF-011: curation labels create misplaced trust - High

Users may read “listed” or “conformant” as “safe,” while a provider later
changes ownership or releases malicious bytes.

**Disposition:** closed by SPC-017, SPC-024, explicit label definitions, exact
artifact evidence, expiry, and revocation. UI must never collapse the labels.

### RTF-012: a fork can change its workflow and provider request - Critical

Public CI may evaluate attacker-controlled workflow/config changes. Secrets can
be exposed if a plugin opt-in or policy is taken from the pull request.

**Disposition:** closed by SPC-001 through SPC-005 and CI requirements. The
maintained Action does not expose native execution; sandbox policy must come
from immutable trusted integration/workflow state. Fake-secret fork tests are
mandatory.

### RTF-013: cache keys can omit trust or parser state - High

Reusing output by provider name/version and source digest alone can survive an
artifact, parser, policy, runtime, option, revocation, or repository change.

**Disposition:** closed by SPC-016, SPC-025, SPC-029, and deferring cache support
until the complete identity is implemented.

### RTF-014: offline verification can miss revocation - High

Network-free analysis may retain a once-trusted artifact after compromise.
Always-online checks create availability and privacy failures.

**Disposition:** mitigated by signed, expiring local policy/revocation data,
release updates, explicit expiry, and fail-closed stale policy. Analysis remains
network-free. Emergency response must publish updated deny evidence through
trusted channels.

### RTF-015: sandbox strength differs by platform - High

Linux namespaces/seccomp, Windows AppContainer/job controls, and macOS
facilities differ. Claiming a generic “sandbox” can hide missing controls.

**Disposition:** closed by SPC-009, SPC-010, exact backend evidence, capability
canaries per platform, and refusing execution where minimum v1 controls cannot
be demonstrated.

### RTF-016: parser complexity bypasses host byte budgets - High

Small input can trigger exponential parsing, pathological type expansion,
recursion, or huge internal allocations. Input limits alone are insufficient.

**Disposition:** closed by SPC-027, fuel/CPU/memory/wall ceilings, adversarial
complexity corpora, and parser fuzzing. Budget exhaustion is an error.

### RTF-017: diagnostics can leak contract contents or inject CI syntax - High

Parser errors often echo source lines. Provider-controlled text can contain
terminal escapes, GitHub workflow commands, newlines, or misleading paths.

**Disposition:** closed by SPC-015. Protocol diagnostics carry codes and bounded
safe parameters; source excerpts require a separate explicit local-only mode
and never enter Action command channels unescaped.

### RTF-018: rollback may become impossible after yank/revocation - Medium

Deleting old artifacts breaks historical lock replay; running a revoked
artifact may be unsafe.

**Disposition:** closed by SPC-020 and SPC-025. Historical identity remains
visible; revoked execution is denied by default; a controlled emergency
override is distinct from successful verification.

### RTF-019: provider-controlled file requests create a confused deputy - High

Even without direct filesystem access, a callback accepting arbitrary paths
could make Boundver read secrets on the provider's behalf.

**Disposition:** closed by SPC-011 and SPC-012. The complete virtual input set is
host-resolved before start; BPP/1 has no arbitrary read callback.

### RTF-020: legacy and sandboxed providers can be confused - Critical

If names, flags, docs, outputs, or CI inputs make legacy native code appear
equivalent to sandboxed extensions, users can unknowingly choose the unsafe
tier.

**Disposition:** closed by distinct namespaces, policy kinds, output trust tier,
no native fallback, no maintained-Action native input, migration warnings, and
SPC-032's quarantine requirements.

### RTF-021: runtime identity can destroy portable semantics - High

The first draft called for one complete identity tuple without specifying where
each axis belongs. If OS sandbox or runtime version enters the portable lock or
boundary hash, identical source changes identity across hosts and every
security-only runtime patch creates false API drift. If omitted everywhere, a
runtime downgrade becomes invisible.

**Disposition:** closed by SPC-036. Semantic/artifact identities are portable;
authorization and execution-environment identities are explicit operation and
cache evidence. Runtime changes do not directly hash into the boundary.

### RTF-022: provider-defined option schemas become a host parser - High

A plugin-supplied general JSON Schema, regex, remote reference, custom format,
or default can consume resources, fetch data, run host hooks, or make host and
guest interpret the same repository options differently.

**Disposition:** closed by SPC-038. BPP defines a bounded offline schema subset
without regex, recursion, remote refs, coercion, executable hooks, or implicit
semantic defaults. Host and guest disagreement fails.

### RTF-023: policy identity can be spoofed or changed during use - Critical

Unicode confusables and case aliases can make an operator approve one displayed
provider while authorizing another. A writable/symlinked policy can also change
between parse and broker launch.

**Disposition:** closed by SPC-037 and SPC-040: strict ASCII identities, stable
no-follow reads, ownership/permission checks, policy digest binding, and exact
approval evidence.

### RTF-024: metadata budgets do not prevent overcollection - High

A provider can copy contract source, internal comments, paths, or derived
details into a bounded lock metadata object. Private-repository data can then
reach logs or public artifacts even though byte limits pass.

**Disposition:** closed by SPC-015 and SPC-039. Metadata is allowlisted by the
provider contract, purpose-limited, and tested for non-disclosure; arbitrary
opaque metadata is invalid.

### RTF-025: mutable review evidence can satisfy a gate falsely - High

An approval, scan result, or red-team note can refer to another commit/artifact,
expire, be edited, omit its tool contract, or be supplied by the change author.

**Disposition:** closed by SPC-033, SPC-034, and SPC-040. Gate evidence binds
exact identities, role, status, expiry, and immutable evidence coordinates.

### RTF-026: a “data-only” Python package can still alter execution - Critical

Calling a provider a separate Python package invites ordinary installation into
the Boundver environment. Wheels can affect import paths and startup behavior;
module names, `.pth` handling, dependencies, and installer state recreate the
ambient-code problem even if the intended payload is data.

**Disposition:** closed by SPC-003, SPC-005, SPC-006, SPC-021, and SPC-022.
First-party “packages” are provider bundles consisting of separately ingested
regular manifest/component/evidence files. They are not installed into Python,
not discovered through distribution metadata, and not executed by a package
manager.

### RTF-027: embedded review evidence creates an approval self-reference - High

A commit cannot contain authoritative reviews that are only created after the
commit exists. Storing reviewer names, verdicts, or a mutable API response in
the proposal would either be a forgeable self-assertion or require changing the
reviewed commit and invalidating the evidence.

**Disposition:** closed by SPC-033, SPC-034, SPC-040, and SPC-043. The manifest
contains only immutable proposal and release-review requirements. A bounded
read-only auditor obtains current GitHub state after merge, binds it to the
reviewed head and identical merge tree, checks current reviewer roles and latest
decisive states, fetches two identical snapshots, and passes authority to the
proposal checker only in memory. No embedded proposal or release-review claim
is trusted.

### RTF-028: candidate-controlled gate code can approve itself - Critical

Running an auditor or checker newly supplied by the same acceptance commit
would let malicious candidate code fabricate a clean result before external
evidence is evaluated. A signed output does not help if the signer executed the
attacker's decision procedure.

**Disposition:** closed for proposal acceptance by SPC-033, SPC-034, and
SPC-040 plus a two-phase bootstrap. Gate code and its CI hook land while work is
still blocked. The later acceptance commit must preserve their exact blobs from
its first parent, and the authoritative invocation runs from a trusted clean
control environment. A gate-code update restarts that sequence; it cannot
authorize itself in the commit that introduces it.

### RTF-029: a sandboxed provider can return a deterministic lie - High

Capability isolation, signatures, provenance, and reproducibility can all pass
while an authorized component intentionally omits or fabricates a public
symbol. A targeted logic bomb can behave correctly on the public conformance
corpus and lie only for selected repository bytes. The host cannot derive
semantic truth merely by validating output shape.

**Disposition:** mitigated, not erased, by SPC-002, SPC-019, SPC-031, and
SPC-041. Exact provider identity remains visible; first-party artifacts require
source review, reproducible builds, differential/metamorphic/adversarial tests,
and narrow claims. High-assurance policy may require agreement from independent
implementations. Community labels never claim correctness. Deliberate semantic
deception remains an explicit residual risk of authorizing third-party code.

### RTF-030: digest-neutral artifact migration can hide future semantic drift - High

Two artifact versions can return equal output for today's repository while
differing for another valid input tomorrow. Treating one output comparison as
proof of implementation equivalence would preserve a semantic contract ID that
no longer describes one stable function.

**Disposition:** mitigated by SPC-016, SPC-019, SPC-036, and SPC-041. Artifact
identity always changes visibly and is never folded into semantic identity.
Retaining the digest-contract ID additionally requires source review and full
differential/conformance evidence; equality for one repository is explicitly
insufficient. A later output change still rotates the boundary and exposes the
exact artifact that produced it.

### RTF-031: an indefinitely valid approval becomes stale authority - High

GitHub can continue to report an old exact-head approval after reviewers leave,
assumptions change, or the platform/runtime threat landscape moves. Exact SHA
binding alone does not make a years-old security judgment current.

**Disposition:** closed for the proposal gate by SPC-033, SPC-040, and SPC-044.
Counted reviewers must still be the two distinct external humans configured in
the pinned account-owned public gist and must retain exactly the public read-only
repository permission. Their latest decisive review
must postdate the unchanged roster, precede the merge, and not be future-dated;
approvals expire after 90 days. Expiry requires a new acceptance PR and fresh
review; old evidence cannot be refreshed by editing the immutable commit.

### RTF-032: merged-PR metadata survives canonical history rewrite - High

GitHub can retain a PR's merged state and review records after an administrator
force-moves `main` so the reviewed merge result is no longer in canonical
history. Checking only `merged_at` and `merge_commit_sha` would accept an
orphaned proposal.

**Disposition:** closed by SPC-034 and SPC-040. Each authoritative audit reads
the current canonical `main` ref and GitHub comparison, requires the proposal
record to be its merge base/ancestor, and includes both in two stable snapshots.
A concurrent move or rewritten-away record fails closed.

### RTF-033: per-response limits do not bound a paginated audit - Medium

An API page can fit its byte cap while many pages, reviewers, permission
lookups, or slow responses cumulatively exhaust memory, rate limits, and wall
time. Per-command timeout and per-page validation alone leave this gap.

**Disposition:** closed by SPC-034 and SPC-040. The authoritative auditor has
independent per-response and aggregate response-byte limits, plus ceilings for
pages, records, reviewer identities, API requests, each command, and a
90-second monotonic total. Exhausting any budget fails the gate without using
partial evidence.

### RTF-034: host-side artifact inspection runs the attack before the sandbox - Critical

The initial installation sequence said to inspect WebAssembly imports and
features without naming the process that parses the binary. If Boundver loads a
runtime/parser in-process, a malformed component can exploit that native parser
before the broker or OS sandbox exists. Manifest, certificate, attestation,
provenance, transparency-proof, and precompiled/AOT parsers have the same
problem.

**Disposition:** closed by SPC-008, SPC-009, SPC-021, and SPC-042. The
coordinator only bounds and hashes opaque provider files. An
already-contained, limited one-shot verifier performs all structured evidence
and component decoding, cryptographic validation, compilation, AOT handling,
and import/export inspection and returns a complete nonce/digest-bound result.
Execution follows the same ordering; unsupported platforms fail before parsing
provider-controlled structured data.

### RTF-035: an alternate manifest can escape the reviewed tree - Critical

The first authoritative-auditor CLI accepted a caller-supplied `--manifest`
path. A locally fabricated accepted record outside the Git tree could therefore
be evaluated after the auditor proved an unrelated reviewed repository tree,
breaking the exact-content gate even though the default command was safe.

**Disposition:** closed by SPC-034 and SPC-040. The authoritative auditor has
one compiled canonical manifest path under the selected checkout and exposes no
manifest override. That file is in the governed path set, must be clean, and is
bound through the local, merge-result, reviewed-head, and GitHub record trees.
The general-purpose structural checker may still inspect test fixtures, but it
cannot confer authoritative review evidence.

### RTF-036: a review body can be edited after merge - High

GitHub approval state and `submitted_at` do not prove that a security marker in
the current review body existed when the PR merged. A reviewer could add the
marker to an old approval after merge; REST review data alone does not expose
that distinction.

**Disposition:** closed by SPC-033 and SPC-040. The auditor cross-binds every
REST review database ID to GraphQL `fullDatabaseId` and `lastEditedAt`, requires
the complete ID sets to agree, and excludes any counted review edited after the
merge or at the same timestamp, where ordering is ambiguous. Review submission
must likewise strictly precede merge, and future GitHub timestamps are not
tolerated. Edit metadata is included in both bounded normalized snapshots.
Missing, duplicated, malformed, paginated-inconsistently, or racing metadata
fails closed.

### RTF-037: underspecified WebAssembly features break determinism - High

An import allowlist does not constrain core/proposal features. Threads, shared
memory, relaxed SIMD, NaN payload choices, experimental component adapters, or
runtime-specific feature defaults can make the same component race, expose
implementation-defined behavior, or produce different canonical output across
engines and architectures.

**Disposition:** closed at design level by SPC-010, SPC-018, SPC-019, and
SPC-042. The contained verifier enforces an explicit versioned feature and
instruction allowlist before execution. The initial profile rejects threads,
atomics, shared memory, relaxed SIMD, memory64, experimental features,
implementation-dependent floating-point behavior, and all unreviewed imports;
permitted NaN behavior must be canonicalized, or floating point is forbidden.
Cross-engine/OS/architecture golden replay and feature-rejection fixtures are
mandatory under SPV-006, SPV-009, SPV-010, and SPV-016.

### RTF-038: a transport nonce becomes guest randomness - High

BPP needs a fresh nonce to bind broker messages, but including it in the
component request gives the guest a random per-run input. A malicious or merely
incorrect provider can then vary valid canonical output while every source byte
and semantic option remains unchanged.

**Disposition:** closed by SPC-018, SPC-028, and SPC-030. The nonce belongs only
to the coordinator/broker envelope and is never part of the component world,
virtual source, guest metadata, output, or digest. Component-interface tests
prove that nonce, source commit, clock, and other per-run values are
unobservable; deterministic replay and fault injection cover substitution and
replay under SPV-009 and SPV-030.

### RTF-039: valid provider output is a bounded exfiltration channel - High

A provider must read selected contract files and emit digest-relevant data.
Even without network, filesystem, or diagnostics, a malicious authorized
provider can encode some of those bytes into canonical entries or the resulting
digest. Capability isolation cannot distinguish that from incorrect semantic
extraction.

**Disposition:** mitigated, not erased, by SPC-012, SPC-015, SPC-039, and
SPC-041. Users explicitly grant access only to a pre-resolved input closure;
raw canonical entries are validated and hashed in the coordinator and are not
persisted or emitted by default; diagnostics and metadata are narrow
allowlists; first-party providers require source review and adversarial
semantic testing; high-assurance policy may use independent-provider agreement.
Selected-source confidentiality from the authorized provider, and the finite
information capacity of a digest, remain explicit residual risks.

### RTF-040: repository-local tools or an alternate API host forge review evidence - Critical

Launching bare `git` or `gh` names with the checkout as current directory can
select a tracked/untracked repository-local executable on Windows or a hostile
PATH entry. An ambient `GH_HOST` can also redirect otherwise fixed API paths to
a server that returns fabricated repository and review JSON.

**Disposition:** closed by SPC-034 and SPC-040. The auditor resolves each tool
to a regular absolute host path, rejects any resolution inside the checkout,
and invokes the GitHub API with explicit `--hostname github.com`. The trusted
control environment, installed tool binaries, authentication store, DNS/TLS,
GitHub service, and Python runtime remain declared roots of trust; the gate does
not pretend to attest its own host.

### RTF-041: bounded JSON bytes still permit pathological number parsing - Medium

On supported older Python versions, converting an attacker-controlled JSON
integer with hundreds of thousands of digits can consume disproportionate CPU
and memory while the enclosing response remains below its byte cap. Floats also
introduce an unnecessary second numeric domain into gate evidence.

**Disposition:** closed by SPC-034 and SPC-040. Both proposal and authoritative
evidence decoders use duplicate-safe JSON with an explicit signed-64-bit integer
hook and reject floats, non-finite constants, overlong integers, and recursive
or malformed input. Hostile numeric fixtures are part of SPV-035.

### RTF-042: reviewed-tree proof races mutable checker inputs - Critical

The first auditor captured a clean record commit and matching Git tree, queried
GitHub, and then loaded checker code and proposal files from the working tree.
A concurrent writer could replace those bytes during the network window and
make unreviewed gate logic or an external acceptance declaration run with
`authoritative_review_passed=True`.

**Disposition:** closed by SPC-034 and SPC-040. The auditor first identifies the
record commit, then reads every checker input as a bounded exact Git blob and
materializes a private validation tree. Requirements, checker code, manifest,
schema, CI contract, RFC, and threat model are all consumed from that snapshot;
no post-proof worktree content participates. A regression fixture changes the
working manifest after commit and proves the committed bytes are used.

### RTF-043: a multi-commit rebase hides gate changes before the record parent - Critical

The bootstrap check originally compared the acceptance record's gate blobs only
with `record^1`. In a multi-commit rebase merge, an earlier rebased commit could
replace the auditor/checker and a later declaration commit could leave those
malicious blobs unchanged, making `record^1` the wrong pre-change baseline.

**Disposition:** closed by SPC-034 and SPC-040. The auditor records the local
record from canonical first-parent history, records its first parent, and
requires that parent to equal GitHub's exact base SHA for the reviewed PR, in
both normalized snapshots. Squash and merge commits compare against the pre-PR
base; a one-commit rebase has the same property; an ambiguous multi-commit
rebase fails closed. The bootstrap blob comparison then proves no gate change
entered the acceptance PR.

### RTF-044: Git symlink blobs become trusted regular checker inputs - High

Reading `commit:path` proves blob identity but not the tree entry's mode. A Git
symlink is also a blob; materializing its target text as an ordinary file would
silently change the reviewed object semantics and could make path validation
reason about a different object class than reviewers saw.

**Disposition:** closed by SPC-034 and SPC-040. Exact validation-tree loading
parses the NUL-delimited Git tree entry and accepts only one matching `blob`
with mode `100644` or `100755`. Symlink, submodule, tree, missing, duplicate, or
malformed entries fail before content is materialized. A synthetic mode-120000
Git fixture verifies rejection under SPV-035.

### RTF-045: the native broker becomes an ambient plugin backdoor - Critical

A WebAssembly guest is not the only executable. Discovering a native broker or
runtime from PATH, a Python package, the checkout, a mutable version, or an
uncontrolled dynamic-library search path recreates arbitrary native execution
before guest capabilities matter. Automatic runtime download during analysis
adds the same supply-chain and fork risks.

**Disposition:** closed at design level by SPC-006, SPC-010, SPC-021 through
SPC-023, SPC-026, and SPC-036. The optional broker/runtime is a separately built
first-party companion provisioned before analysis, selected by
operator-controlled absolute path and exact digest, provenance/revocation
checked, same-object launched, and recorded as runtime evidence/cache identity.
No checkout/PATH/package discovery, analysis-time install, provider-controlled
loader state, or native fallback is permitted. A packaging ADR and
cross-platform loader/path negative matrix block implementation.

### RTF-046: machine acceptance contradicts the human proposal - High

The checker initially required control IDs and the proposal identifier in both
documents but did not bind their status text. A manifest could therefore claim
`accepted` and enable implementation while the RFC and threat model still said
Review Ready or described work as blocked.

**Disposition:** closed by SPC-033 and SPC-040. Both documents contain exactly
one machine-readable marker for proposal status, implementation authority, and
v0.15 work authority. The structural checker derives expected values from the
manifest and rejects a missing, duplicate, malformed, stale, or contradictory
marker. Acceptance tests mutate all three surfaces together and prove a
manifest-only transition fails.

### RTF-047: v0.15 release evidence is self-referential or never enforced - Critical

The initial release record embedded scan booleans, reviewer evidence, and an
`exact_candidate_commit` string in the proposal manifest. A release commit
cannot contain its own SHA or reviews and scans created only after it exists;
the checker also accepted any 40-hex candidate value without comparing it to a
release input. More fundamentally, neither the local release launcher nor the
tag and publication workflows consumed the semantic-provider release gate, so
the documented prohibition could be bypassed by the ordinary release path.

**Disposition:** closed by SPC-034, SPC-040, and SPC-043. The manifest now
declares only immutable release-review requirements. The authoritative auditor
accepts the v0.15.0 tag and release SHA externally, requires local `HEAD` and
the GitHub release record to match, and proves a separate merged release PR's
reviewed head tree equals the release commit tree. Two fresh, role-marked
approvals from the distinct external humans in the pinned gist roster,
resolved threads, no pending requests, and one exact six-assertion security
attestation are required from two identical bounded GitHub snapshots. The
local launcher, both read-only tag checks, and fresh or resumed publication
invoke the audit fail closed. Those enforcement files are bootstrap-protected,
and SPV-038 mutates every identity, review, roster, attestation, ordering,
and workflow-enforcement edge.

### RTF-048: release authority expires or is edited during the tag handoff - Critical

The read-only semantic audit initially handed only the existing general review
digest to the write-token tag job. That digest included current review bodies
but not GraphQL `lastEditedAt`, so a reviewer could edit an approval after the
audit and restore byte-identical text without changing the handoff. Review
freshness was evaluated only at audit time, allowing a proposal or release
approval to cross its 90-day or 14-day limit while the tag job waited.

**Disposition:** closed by SPC-034, SPC-040, and SPC-043. The workflow-owned
review snapshot now cross-binds every REST review ID to GraphQL edit metadata
and includes `lastEditedAt` in the digest compared before and throughout tag
mutation. The authoritative auditor computes the latest instant at which a
qualifying reviewer set and security marker remain valid; for v0.15 it hands
off the earlier of proposal and release authority expiry. The tag job strictly
validates that value and rechecks it alongside the mutable-state digest before
tag creation, authentication, and push. Missing IDs, same-text edits, changed
roles/states/threads/requests, malformed timestamps, expiry, or a racing
snapshot fails closed under SPV-038. The workflow snapshot also binds the
complete gist file, owner-authored revision, and both effective permissions, so
a roster or permission edit aborts the handoff. A five-minute minimum remaining-validity margin plus a 60-second
tag-push timeout bounds the final check/use interval.

### RTF-049: repository namespace reuse inherits old approval authority - High

The first auditor compared only the textual `yzm1/boundver` full name and owner
login. A repository transfer changes the controlling account without
necessarily changing its public path, while deletion and recreation can assign
the old path to a wholly different repository. Text equality alone could make
old governance requirements appear to authorize a new GitHub object.

**Disposition:** closed by SPC-034 and SPC-040. Proposal and v0.15 release
requirements now pin GitHub repository ID `1226008327` and owner account ID
`22440724` in addition to the case-normalized full name. Every normalized
snapshot obtains and validates those numeric identities before review evidence
is evaluated. A legitimate ownership transfer or repository replacement fails
closed and requires a blocked governance update followed by fresh independent
acceptance. Namespace/ID mutation fixtures are included in SPV-035 and
SPV-038.

### RTF-050: write-level reviewer authority expands the attack surface - Critical

The first independent-review rule accepted only humans with current `write`,
`maintain`, or `admin` permission. This repository belongs to a personal
account, where adding a collaborator grants write access rather than an
organization's narrower triage role. At discovery time `main` had no branch
protection and the SemVer tag ruleset intentionally did not restrict creation.
Recruiting reviewers under that rule would grant repository and release-state
mutation authority merely to obtain an opinion, turning the assurance gate
itself into a supply-chain exposure.

**Disposition:** closed by SPC-033, SPC-040, and SPC-044. Reviewer authority now
comes from a public gist owned by account `22440724`, whose gist ID, node ID,
owner, description, and sole filename are pinned. Its strict content names one
security and one product reviewer by numeric account ID and login and records
the owner's beneficial-owner-independence attestation. The designated
identities must be external to the repository owner and PR author, and the gist
update must strictly predate both counted exact-head approvals. Public-repository read access is sufficient,
so reviewers receive no push, tag, release, secret, or settings authority. The
auditor reads the gist, its immutable latest revision, and both effective permissions twice, and the release
workflow includes their complete normalized state in every mutation-handoff
digest. Gist replacement, owner or revision substitution, truncation, file or
reviewer substitution, duplicate identities, bots, permission above read, post-approval edits, or
malformed metadata fail closed under SPV-035 and SPV-038. Beneficial-owner
independence remains a human governance assertion, not something GitHub account
IDs can prove.

### RTF-051: public read permission is not Environment reviewer eligibility - Critical

The RTF-050 closure assumed a public user whose effective permission endpoint
reported exact `read` could be selected as a GitHub Environment required
reviewer. A live disposable-environment probe disproved that assumption:
GitHub rejected an ordinary public reader with HTTP 422 while accepting the
repository owner through the same request. GitHub documents that Environment
reviewers need repository read access, but a personal-account repository has
only owner and collaborator authority, and collaborators can push. The REST
collaborator `permission` selector is explicitly limited to organization-owned
repositories. The proposed pair of read-only Environment reviewers was
therefore impossible here: the gate could never pass without granting the very
write authority RTF-050 prohibited.

**Disposition:** closed by SPC-033, SPC-040, and SPC-044. Environments are no
longer reviewer authority. The account-owned public gist adds no repository
permission to either reviewer, while its owner, numeric/node identities,
canonical file, immutable revision, timestamps, and exact public-read
permission records are independently observable and included in every bounded snapshot.
Both designated humans must submit role-specific exact-head markers containing
their own independence confirmation after the latest roster update. Any roster
or permission mutation invalidates prior approvals. The disposable probe
environment was deleted, no semantic-provider review environment exists, and
the unconfigured roster fails closed until two external humans are named.

### RTF-052: a locked issue does not provide owner-only attestation - Critical

The first RTF-051 correction moved reviewer names into a locked issue authored
by the repository owner. Locking controls conversation, not issue edits.
GitHub permits collaborators on a personal-account repository to edit issue
titles and descriptions, and this repository currently has a non-owner write
collaborator. That collaborator could replace the roster while retaining a
literal `Attested-by: 22440724:yzm1` line; issue author identity proves who
created the issue, not who last edited its body. Timestamps would invalidate
older reviews but could not prove the new attestation came from the owner.

**Disposition:** closed by SPC-033, SPC-040, and SPC-044. Reviewer authority is
held in public gist `0caedb798d168b974f9d9fb63c377f73`, owned by account
`22440724`. Updating a gist requires separate user-level Gists write authority;
repository collaboration does not confer it. The gate pins gist and owner
identities, description and sole filename; requires a complete public UTF-8
file; validates the latest history entry's owner, commit ID, timestamp and
change counts; and fetches that immutable revision to prove it reproduces the
current roster. Mutable timestamps and revision identity are bound into every
double snapshot, so deletion, replacement, rollback, concurrent edit, or
post-approval change fails closed. Compromise of the owner's GitHub account or
Gists-authorized token remains an explicit root-of-trust risk.

### RTF-053: repository-held user credentials collapse gist separation - Critical

An account-owned gist is a separate authority only while its mutation
credential remains outside repository-controlled execution. If an owner stores
a Gists-write PAT, GitHub App user token, OAuth token, or equivalent credential
in an Actions or Environment secret, a write collaborator who can influence a
workflow may be able to exfiltrate or exercise it and forge the owner's roster
attestation. Merely using a different GitHub object does not help if both trust
roots share a credential channel.

**Disposition:** closed by SPC-033, SPC-040, and SPC-044. GitHub permits
anonymous reads of public gists, but requires separate user-level Gists write
permission to mutate one. The tag workflow reads only the fixed current and
immutable-revision gist paths anonymously over verified HTTPS with no redirect
or authorization header. Its repository calls and all publication calls use
only the repository-scoped `github.token`, whose workflow permission vocabulary
contains no Gists authority; the proposal checker rejects secret expressions,
alternate `GH_TOKEN` sources, or authenticated gist reads. At this review, every
repository and Environment secret and variable inventory is empty. Operational
policy forbids placing any Gists-write credential in repository workflows,
secrets, variables, artifacts, caches, or runners. A future owner who violates
that policy, or compromise of the owner's account or separately held token,
remains a root-of-trust failure rather than a property the repository can prove
away.

### RTF-054: a write collaborator can bypass the audited release path - Critical

The semantic audit can prove what the release workflows observed, but it cannot
make those workflows exclusive mutation authority by itself. At discovery this
personal repository had no `main` branch protection; its active SemVer tag
ruleset blocked updates, non-fast-forwards, and deletion but deliberately
permitted tag creation; and direct collaborator `horizonscanning` (account
`259076988`) had `write`/`push` permission. That principal could have pushed
unreviewed source, occupied `v0.15.0` with an immutable tag, or created a
misleading GitHub Release without traversing the exact-tree audit. Publication
registries might still have rejected the attempt, but release identity,
availability, and user trust would already have been damaged.

The collaborator was removed on 2026-08-30. A subsequent live census returned
exactly repository owner `22440724:yzm1` with `admin` authority and no pending
invitations, deploy keys, or repository webhooks. SPC-045 now repeats that exact
collaborator census in both authoritative snapshots and the release workflow.
GitHub does not expose every owner-delegated App, OAuth grant, or separately
held credential to the repository workflow token, so roster format v2 also
binds the owner's explicit
`Owner-exclusive-mutation-authority-attested: true` declaration. A false
declaration or owner-account compromise remains a root-of-trust failure.

**Disposition:** open and release-blocking under
[#85](https://github.com/yzm1/boundver/issues/85) until SPC-045 is merged, the
v2 owner attestation is published in the immutable public-gist history, and
both independent exact-tree reviewers approve that closure. External product
and security reviewers need only public read access and must not be made
collaborators.

## Threat catalog and traceability

The JSON assurance record is authoritative for automated cross-reference. This
table is the human review surface.

| Threat | Adversarial scenario | Impact | Principal controls | Required verification |
| --- | --- | --- | --- | --- |
| SPT-001 | Repository data activates code without operator policy | Host compromise | SPC-001, SPC-002, SPC-004 | SPV-004, SPV-017 |
| SPT-002 | Entry points, `PYTHONPATH`, PATH, cwd, or import hooks shadow identity | Wrong/malicious code execution | SPC-003, SPC-006, SPC-021 | SPV-005, SPV-011 |
| SPT-003 | Malicious or compromised artifact has a valid name/version | Host compromise, false digest | SPC-002, SPC-007, SPC-016, SPC-021 | SPV-010, SPV-012, SPV-027 |
| SPT-004 | Provider reads environment, credentials, home, agents, or cloud metadata | Secret disclosure | SPC-007, SPC-008, SPC-009 | SPV-006, SPV-017 |
| SPT-005 | Provider writes checkout, Git state, output, or unrelated files | Integrity loss | SPC-007 through SPC-012 | SPV-006, SPV-030 |
| SPT-006 | Provider monkey-patches or terminates Boundver | Gate bypass/availability | SPC-006, SPC-008, SPC-010 | SPV-006, SPV-030 |
| SPT-007 | Small/large input consumes unbounded CPU, memory, stack, or output | Denial of service | SPC-014, SPC-027, SPC-028 | SPV-007, SPV-008, SPV-016 |
| SPT-008 | Malformed protocol length/state corrupts parser or allocates first | Broker/coordinator compromise | SPC-027, SPC-028 | SPV-007, SPV-008 |
| SPT-009 | Provider coerces host into reading undeclared paths | Secret disclosure, digest confusion | SPC-011, SPC-012 | SPV-004, SPV-006 |
| SPT-010 | Locale, OS, parser, clock, random, or ordering changes output | Non-reproducible locks | SPC-016, SPC-018, SPC-019 | SPV-009, SPV-016 |
| SPT-011 | Older artifact/contract is replayed or version is downgraded | Stale/false verification | SPC-016, SPC-019, SPC-020 | SPV-010, SPV-019 |
| SPT-012 | Artifact changes between verification and execution | Arbitrary code execution | SPC-021, SPC-026 | SPV-011, SPV-013 |
| SPT-013 | Namespace/catalog label spoofs first-party trust | Social/supply-chain compromise | SPC-017, SPC-024 | SPV-012, SPV-021 |
| SPT-014 | Error/metadata leaks source or injects terminal/CI controls | Disclosure, log manipulation | SPC-015 | SPV-015, SPV-017 |
| SPT-015 | Cache omits identity, policy, revocation, or completeness | Stale/poisoned result | SPC-016, SPC-025, SPC-029 | SPV-014, SPV-019 |
| SPT-016 | PR changes both provider request and lock/policy-like repository data | Self-authorization | SPC-001, SPC-002 | SPV-004, SPV-017 |
| SPT-017 | Build/dependency/index/publisher compromise inserts code | Supply-chain compromise | SPC-021 through SPC-025 | SPV-012, SPV-020, SPV-022 |
| SPT-018 | Guest escapes WebAssembly runtime/broker | Host compromise | SPC-007 through SPC-010, SPC-023 | SPV-006, SPV-022, SPV-027 |
| SPT-019 | Revoked artifact still runs offline | Known-compromised execution | SPC-020, SPC-025 | SPV-019, SPV-028 |
| SPT-020 | Prefix/truncated/partial output is accepted | Incomplete boundary blessed | SPC-013, SPC-014, SPC-028 | SPV-007, SPV-030 |
| SPT-021 | Dynamic language construct is silently omitted | False stable digest | SPC-014, SPC-018, SPC-019 | SPV-016, SPV-026 |
| SPT-022 | Semantic digest is presented as compatibility proof | Unsafe product decision | Compatibility non-claim, governance | SPV-003, SPV-021 |
| SPT-023 | Bundle/store path traversal, link, case, or ADS attack | Host file overwrite/substitution | SPC-022, SPC-026 | SPV-013 |
| SPT-024 | Concurrent jobs cross-wire state, cache, store, or response | Cross-repository result leak | SPC-008, SPC-026, SPC-030 | SPV-014, SPV-030 |
| SPT-025 | Platform lacks equivalent sandbox/resource control | Overstated isolation | SPC-009, SPC-010 | SPV-006, SPV-009 |
| SPT-026 | Missing sandbox silently invokes native plugin | Host compromise | SPC-006, SPC-010 | SPV-005, SPV-018 |
| SPT-027 | Analysis downloads/builds code or follows mutable URL | Supply-chain/RCE, nondeterminism | SPC-003, SPC-005, SPC-021, SPC-022 | SPV-005, SPV-025 |
| SPT-028 | Maintainer/release workflow compromise publishes “official” malware | Supply-chain compromise | SPC-002, SPC-023, SPC-025 | SPV-012, SPV-020, SPV-022 |
| SPT-029 | Parser differential or unsupported grammar changes meaning | False/moving digest | SPC-018, SPC-019 | SPV-009, SPV-016, SPV-026 |
| SPT-030 | Provenance, signature, or conformance is treated as authorization | Malicious signed code runs | SPC-002, SPC-023, SPC-024 | SPV-012, SPV-021 |
| SPT-031 | Repeated prompts train users to approve opaque upgrades | Approval fatigue | SPC-002, SPC-021, SPC-022 | SPV-021, SPV-028 |
| SPT-032 | Huge provider declarations/errors exhaust validation before execution | Denial of service | SPC-027, existing config limits | SPV-008, SPV-023 |
| SPT-033 | Clock/random/nonce/timing interfaces create nondeterminism or side channels | Digest drift/information leak | SPC-007, SPC-018, SPC-028, SPC-030 | SPV-006, SPV-009, SPV-030 |
| SPT-034 | Historical exact artifact disappears | Replay/rollback unavailable | SPC-020, SPC-021 | SPV-028 |
| SPT-035 | Provider ships incompatible or prohibited licenses | Legal/distribution incident | SPC-023, curation governance | SPV-020, SPV-029 |
| SPT-036 | Runtime/backend identity is omitted or incorrectly made semantic | Hidden downgrade or non-portable lock drift | SPC-016, SPC-036 | SPV-009, SPV-010, SPV-034 |
| SPT-037 | Host policy is spoofed, raced, or locally replaced | Unauthorized execution | SPC-002, SPC-037, SPC-040 | SPV-004, SPV-011, SPV-031 |
| SPT-038 | Provider option schema triggers host work or divergent defaults | DoS, network access, digest ambiguity | SPC-027, SPC-028, SPC-038 | SPV-007, SPV-008, SPV-032 |
| SPT-039 | Provider output or metadata discloses selected source beyond the narrow contract | Information disclosure | SPC-012, SPC-015, SPC-039, SPC-041 | SPV-015, SPV-033, SPV-036 |
| SPT-040 | Stale, mutable, misattributed, or over-privileged evidence satisfies approval | Governance, supply-chain, and release bypass | SPC-033, SPC-034, SPC-040, SPC-044, SPC-045 | SPV-021, SPV-024, SPV-035, SPV-038 |
| SPT-041 | Authorized sandboxed provider returns deterministic but false output | False stable or changed boundary | SPC-002, SPC-019, SPC-031, SPC-041 | SPV-016, SPV-026, SPV-036 |
| SPT-042 | Host parses, compiles, or deserializes provider artifact/evidence before containment | Main-process compromise before sandbox | SPC-008, SPC-009, SPC-021, SPC-042 | SPV-006, SPV-011, SPV-037 |
| SPT-043 | Self-attested, stale, or unenforced v0.15 release evidence satisfies promotion | Release-governance bypass | SPC-034, SPC-040, SPC-043, SPC-045 | SPV-024, SPV-035, SPV-038 |

## Abuse-case test corpus

The implementation gate requires hostile artifacts/components that attempt:

- importing every forbidden WASI interface;
- reading environment, common credential paths, parent handles, and cloud
  metadata addresses;
- writing cwd, repository-like paths, store paths, stdout protocol, and stderr
  beyond limits;
- opening TCP/UDP/DNS, spawning processes/threads, loading libraries, and
  acquiring clock/random values;
- infinite loops, deep recursion, memory/table growth, huge allocations, trap,
  abort, deadlock, pipe fill, and slow-drip output;
- malformed and conflicting identities, nonce reuse, duplicate fields, unknown
  critical fields, out-of-order states, huge/overflowing lengths, premature
  EOF, trailing frames, invalid UTF-8, non-canonical numbers, and compression
  bombs;
- returning valid entries then crashing or omitting the terminal result;
- emitting duplicate/unsorted labels, invalid modes/types, source excerpts,
  terminal escapes, workflow commands, path spoofing, or misleading trust text;
- requesting undeclared paths, symlink targets, host imports, package-manager
  data, or dependency resolution;
- replacing its artifact/cache/store entry concurrently and racing revocation;
  and
- producing different output by OS, architecture, process order, locale,
  timezone, parser version, hash iteration, or repeated execution; and
- behaving honestly on public fixtures but omitting/fabricating entries for a
  target repository, file digest, provider option, or symbol name; and
- malformed modules, component adapters, custom sections, import graphs, and
  compiled/AOT cache records targeting decoder/runtime bugs before guest entry.

Tests must plant fake high-entropy secrets in environment/files and assert that
they never appear in protocol, diagnostics, result files, cache, temp files, or
network observations.

## Semantic adversarial corpus

### Python

- Encodings, BOMs, mixed newlines, invalid Unicode, enormous tokens, nesting,
  and parser-complexity cases.
- Explicit/implicit exports, aliases, relative/star re-exports, cycles, missing
  modules, namespace packages, and duplicate module identities.
- `TYPE_CHECKING`, platform/version branches, `try` imports, overloads,
  protocols, dataclasses, descriptors, decorators, metaclasses, module
  `__getattr__`, dynamic `__all__`, and C-extension placeholders.
- Default expressions with side effects (which must never execute), forward
  references, postponed annotations, and grammar transitions.

### TypeScript

- Value/type/default/wildcard exports, cycles, aliases, declaration merging,
  ambient declarations, overloads, generics, conditional/mapped types, and
  enormous type expansion.
- `package.json` export maps, `typesVersions`, path mappings, project
  references, declaration files, JS interop, module-mode differences, missing
  modules, and case collisions.
- Repository compiler plugins, package scripts, generated declaration output,
  and ambient `node_modules` (which must never execute or become visible).
- Grammar/compiler-version transitions and differential parser fixtures.

Every unresolved construct must produce a deterministic unsupported/error
classification. The corpus must contain paired metamorphic mutations for every
documented ignored and retained construct.

## Verification plan

| Verification | Evidence required before implementation/publication |
| --- | --- |
| SPV-001 | Machine schema and cross-reference checker passes |
| SPV-002 | Strict MkDocs build and link validation passes |
| SPV-003 | RFC/threat/manifest status and product/non-claim review has no contradiction |
| SPV-004 | Hostile repository cannot authorize or widen policy |
| SPV-005 | No ambient entry-point/import/PATH/env discovery; no analysis-time install/network |
| SPV-006 | Capability canaries fail safely on every supported platform |
| SPV-007 | Protocol state/property fuzzing, truncation, overflow, and malformed-frame corpus passes |
| SPV-008 | Every declared resource ceiling is exercised at limit, limit+1, aggregate, and concurrency levels |
| SPV-009 | Byte-identical golden replay across supported OS/arch/Python/repeated runs |
| SPV-010 | Artifact/provider/parser/protocol/policy downgrade and replay matrix fails closed |
| SPV-011 | Store and launch TOCTOU/substitution tests pass |
| SPV-012 | Attestation subject/digest/issuer/workflow/provenance negative matrix passes |
| SPV-013 | Path traversal, link, case collision, ADS, permissions, interrupted-install, and archive rejection passes |
| SPV-014 | Cache poisoning, corruption, identity omission, revocation, and cross-repo tests pass |
| SPV-015 | Diagnostics/output escaping and source-excerpt non-leak tests pass |
| SPV-016 | Semantic differential, metamorphic, unsupported, complexity, and parser fuzz corpus passes |
| SPV-017 | GitHub Action/GitLab fork, hostile workflow data, and fake-secret tests pass |
| SPV-018 | Legacy native mode cannot use sandbox namespace, policy, Action input, fallback, or curation labels |
| SPV-019 | Offline revocation, stale policy, expiry, rollback, and emergency-override tests pass |
| SPV-020 | Reproducible artifact, SBOM, locked dependencies, licenses, and vulnerability evidence passes |
| SPV-021 | Exact-commit non-author security/product reviews approve all claims and UX |
| SPV-022 | Full code/dependency/runtime security scans have no unresolved Critical/High/Medium finding |
| SPV-023 | Full repository test, schema, lint, hygiene, packaging, and supported-platform gates pass |
| SPV-024 | Release tooling enforces exact-commit proposal and full-source audit evidence |
| SPV-025 | Network monitor confirms analysis is offline, including failure paths |
| SPV-026 | Reference and language providers pass the versioned conformance kit |
| SPV-027 | Independent hostile-runtime/broker red-team and fuzz campaign completes |
| SPV-028 | Install, update, yank, revocation, rollback, disappearance, and disaster recovery drill passes |
| SPV-029 | License/policy scan and notices are complete for every artifact and dependency |
| SPV-030 | Crash, cancellation, deadlock, concurrency, cleanup, nonce, and result-binding fault injection passes |
| SPV-031 | Policy confusable, duplicate, permission, symlink, race, digest, and approval-binding tests pass |
| SPV-032 | Option-schema recursion, regex, reference, default, complexity, and host/guest differential tests pass |
| SPV-033 | Output/metadata allowlist, minimization, retention, and private-source non-disclosure tests pass |
| SPV-034 | Semantic/artifact/authorization/runtime identity-placement and cross-platform migration tests pass |
| SPV-035 | Review self-reference, owner-exclusive repository collaborators and v2 owner attestation, account-owned public-gist roster, immutable-revision binding, exact public-read authority, role markers, canonical-manifest/tool/API-host/blob/mode binding, numeric-parser bounds, merge-mode-safe gate bootstrap, post-merge review edits, scan, expiry, role, canonical ancestry, exact-commit/tree/artifact, API-race/budget, TOCTOU, and evidence-tampering negative matrix passes |
| SPV-036 | Malicious-valid output, targeted logic-bomb, independent-oracle, differential, and provider-quorum tests pass |
| SPV-037 | Pre-sandbox manifest/evidence/crypto/decoder/runtime/AOT corpus proves no provider-controlled structure is parsed outside containment |
| SPV-038 | External exact-tree v0.15 attestation roster/identity/permission/repository-collaborator/owner-delegation/review/age/role-marker/edit/expiry/race matrix and local/tag/publish enforcement mutations pass |

## Residual risks

Even after every gate passes:

- a sandbox runtime, OS, broker, parser, compiler, or first-party build system
  can contain an unknown vulnerability;
- static extraction can support only its documented subset of dynamic-language
  behavior;
- an authorized malicious publisher can cause denial of service up to enforced
  ceilings and can intentionally return semantically wrong data unless tests or
  review catch it;
- an authorized provider can derive bounded output from every selected virtual
  input, so isolation cannot guarantee those selected bytes remain secret from
  the digest/output observer;
- revocation evidence can be stale while analysis is offline;
- deterministic structural identity is not compatibility proof; and
- a compromised reviewer account, GitHub service/API, local Git/`gh`/Python
  control environment, or coordinated reviewer failure can defeat governance;
  double snapshots and exact identities narrow but do not erase that root of
  trust; and
- a user can explicitly run legacy trusted-native code and thereby grant it the
  user's authority.

These risks must remain visible in user-facing documentation and result
provenance. A future implementation may reduce them but must not delete or
soften this section without a new threat-model review.

## Red-team acceptance record

The proposal is not accepted while the machine record is not `accepted`.
Embedded review records are not authoritative; acceptance is the conjunction
of that static record and a successful current run of
`scripts/audit_semantic_provider_proposal.py`. Acceptance evidence must record:

- exact commit SHA;
- reviewers and review roles;
- commands and CI run URLs;
- fuzz/corpus versions where applicable;
- every finding and disposition;
- residual risks explicitly accepted; and
- the date and next mandatory review trigger.

An acceptance commit cannot modify the auditor, proposal checker, CI hook,
local release launcher, release-tag workflow, or publication workflow. Those
bootstrap changes require an earlier blocked PR and a separate acceptance PR,
preventing candidate-supplied gate code from approving itself.

Triggers include protocol/runtime changes, new capabilities, new artifact or
catalog infrastructure, a new platform, provider digest-contract changes,
security incidents, the first community-provider listing, and migration or
retirement of the GitHub API version used by the authoritative review auditor.
