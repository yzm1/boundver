# Project review: v0.10 retrospective and v0.11 design record

- Audit date: 2026-08-12
- Immutable release under review: `v0.10.0` at `5664437`
- Main baseline at re-audit start: `6e337b10086961a027e1398e44da4df870066e21`
- Corrective contract: `0.11.0` using `boundary-lock/v3`

This document supersedes the earlier blanket statement that every finding was
resolved. It records the published v0.10 behavior, reproduced failures, v0.11
design responses, and durable product limits. Current release status belongs in
the automated release gate and its workflow run, not in this retrospective.
It is a repository-maintainer record and is deliberately excluded from source
distributions.

## Executive conclusion

The v0.10 review was not reliable enough to support its “all findings resolved”
claim. Several tests asserted the implementation's behavior instead of an
independently defined contract. The clearest example was glob matching: the
README described `*` as non-recursive and `**` as recursive, while v0.10 tests
blessed whole-string `fnmatch` behavior in which `*` crossed `/` and `**` missed
the zero-directory case.

The re-audit found additional violations in file identity, semantic-config
identity, behavior/boundary containment, vendored-copy generation, component-
scoped updates, the consumer model, partial-lock symmetry, heterogeneous facet
policy, derived-artifact freshness, canonical parsing, root-manifest discovery,
and release review enforcement. These are correctness issues, not documentation
polish.

The corrective contract changes file hashing, glob selection, provider versions,
and the lock schema. It therefore belongs in `0.11.0`, not `0.10.1`. The
published v0.10 tag, release assets, and PyPI artifacts remain immutable.

## Repository and history reconciliation

The re-audit began by separating real content from worktree noise:

- Twenty-eight tracked paths appeared modified. Two matched `origin/main`
  exactly, twenty-five differed only by CRLF/LF checkout representation, and
  one was stat-only. No unique user content was discarded.
- The audited paths were restored explicitly, the index was refreshed, and a
  `.gitattributes` rule was added in the corrective tree to make LF checkout
  normalization explicit.
- Commit `a337d05496a07883bb960023ee3f86e8e64c3eb5` is not reachable from `main`
  because it belongs to pre-squash history. Its tree is represented by the
  squash result at `5664437`; cherry-picking it would duplicate changes rather
  than recover missing work.
- Local `v0` had been stale. Only the local tag was removed and fetched again;
  no remote tag or release was rewritten.

## Audit method

The second pass used contracts derived before looking at existing tests:

1. State the externally observable invariant in plain language.
2. Construct the smallest counterexamples, including zero-depth recursion,
   symlink/mode transitions, missing inputs, equal-current-output config changes,
   unselected stale components, and malformed canonical documents.
3. Exercise the invariant across `head`, `index`, and `working-tree` where it
   applies.
4. Test the base installation with optional validators unavailable.
5. Inspect built artifacts and public entry points, not only source imports.
6. Keep publication facts separate from local or CI intentions.

Existing tests were treated as evidence only after their expected behavior was
checked against the independent invariant.

## First-principles invariant matrix

| Area | Required invariant | v0.10 result | v0.11 design response |
|---|---|---|---|
| Glob selection | `*`, `?`, and classes stay within one segment; a whole `**` segment matches zero or more directories; every surface uses one matcher | Violated | Shared matcher implemented for config, raw/canonical providers, behavior, and explain; focused regressions added |
| File identity | A digest changes when path, Git mode/type, or content changes | Violated: v2 omitted mode/type | v3 frame binds label, mode, type, and content |
| Snapshot identity | One operation uses one captured source identity; unreachable tags do not influence it | Partially met | HEAD commit and index tree capture implemented; tag lookup bound to captured reachable history |
| Control-file identity | `head`/`index` config and verification lock come from the same captured source as artifacts | Violated: v0.10 could combine staged artifacts with unstaged control files | Config and lock are source-bound; index refreshes must stage source/output/config, generate, then stage the lock |
| Configuration identity | Every semantic policy choice is lock-bound even when selected bytes currently agree | Violated | v3 `config_contract` and `config_digest` cover effective semantic configuration |
| Facet containment | A configured boundary change must change behavior | Violated: path coverage was warning-only | Behavior envelope includes the boundary digest |
| Strict generation | A generated strict lock must immediately be verifiable from the same snapshot | Violated by missing/divergent vendored copies | Vendored absence/divergence is a generation error |
| Focused update | Updating A must not bless B; selected entries must remain internally coherent | Violated | Full candidate recomputation rejects stale unselected entries; selected entries and slices are rebuilt coherently |
| Consumer identity | Internal graph edges are validated; external terminal labels remain explicit and opaque | v0.10 validated all `consumers` as configured components while examples mixed internal and external names | `consumers` is a configured-component edge; `external_consumers` is a separate terminal label; unknown internal names fail |
| Consumer impact | Direct reporting remains stable; CI can request a deterministic downstream closure | Immediate names only | `verify --transitive` and `why --transitive` follow internal edges, include reached external terminals, and handle cycles |
| Slice membership | Explicit sets and declared graph closures are reviewable and reproducible | Explicit component arrays only | `closure_of` resolves seed plus downstream internal closure and persists the sorted membership |
| Facet policy | Heterogeneous components can select meaningful gates; unavailable selected facets cannot pass as null equality | Global default/CLI policy only; `compat` could be vacuous without a version source | Precedence is CLI, component, defaults; configured/CLI unavailable facets fail with usage exit 2; an implicit default means all available facets |
| Partial generation | An escape hatch may relax intentional absence, never bless computation failure | `--allow-partial` could write a lock that normal verification rejected | Only intentional null slice inputs are relaxed; declared/provider/version/vendored errors remain fatal |
| Derived artifacts | A generated boundary must be checked for freshness before its digest is trusted | No derivation model | Limitation is explicit; generator `--check` is required before verify; executable config commands are deferred pending a safe contract |
| Configuration validation | Base install rejects unknown fields and unsafe values; checkout files cannot shadow installed rules | Violated in several schema-optional cases | Hand validation and packaged-schema-first loading implemented |
| Canonical providers | Malformed or unresolved input fails closed; ignored data is narrowly documented | Violated in JSON/OpenAPI edge cases | Duplicate/non-finite JSON, invalid OpenAPI roots/maps/refs/YAML, and extension handling hardened; provider versions advanced |
| Path safety | Empty, absolute, traversing, backslash, and ambiguous declared paths fail consistently | Inconsistent | Shared normalization is used by config, providers, and explain |
| Discovery | A generated config has a safe non-root component or no file is written | Violated for root-only/empty discovery cases | Conservative root-manifest mapping and fail-without-write behavior implemented |
| Severity | Exit classification depends on structured issue type, not component-controlled text | Vulnerable to substring classification | Structured facet extraction implemented, including global highest-severity fail-fast reporting |
| Distribution | Installed Action, wheel, sdist, `.pyz`, Docker, and hooks contain their required runtime behavior | Partially tested | Distribution contracts and release gates exercise each supported installation surface |
| Package promotion | Production PyPI receives the byte-identical reviewed distributions only after a real index rehearsal | No TestPyPI gate; downstream jobs selected workflow artifacts by name | One immutable artifact ID carries only the wheel and sdist through TestPyPI, a hash/size/download/install gate, and PyPI; conflicting pre-existing TestPyPI state fails closed |
| Release review | A release cannot be tagged with blocking state, unresolved threads, pending review requests, or stale/missing exact-commit review evidence | Violated | The pre-tag workflow paginates every review surface; it normally requires non-author human approval with push access and permits numeric-ID-pinned Codex evidence only for an owner-authored PR in this personal repository |

The last column defines the corrective contract. Exact-commit test, review, tag,
and publication evidence is produced by the release workflows rather than kept
as mutable prose in this document.

## Reproduced counterexamples

### Glob grammar

Under v0.10's whole-string matcher:

- `*.yaml` selected root and nested YAML files.
- `api/*.yaml` selected direct and deeper descendants.
- `**/*.yaml` omitted root YAML files.
- `api/**/*.yaml` omitted direct children of `api`.

This contradicted the README and conventional path-glob expectations. The v3
contract now defines, implements, and tests segment-aware matching explicitly.

### File identity

A regular file whose content was `target.txt` could be replaced with a symlink
whose target text was also `target.txt` without changing a v2 raw digest. A
`100644` to `100755` executable-bit transition could also remain clean. Content
alone is therefore not a sufficient file identity; v3 includes Git mode/type.

### Semantic configuration

Policy changes could preserve current v2 fingerprints when they happened to
select the same bytes—for example, changing a literal to an equivalent glob,
changing a version source that currently yielded the same value, adding
provider options, or weakening a default gate. v3 binds a normalized semantic
configuration independently of current component output.

### Behavior containment

v0.10 only warned when `behavior.paths` did not cover boundary files. A boundary
could change while a configured behavior digest stayed equal, allowing a
behavior-only gate to pass. The corrective behavior envelope incorporates the
boundary digest, making containment a cryptographic property rather than a
documentation convention.

### Vendored strictness

Strict generation in v0.10 could write a lock when a configured vendored copy
was missing or divergent. The same lock could immediately fail verification.
That violates the minimum generation invariant. The corrective implementation
treats missing, empty, unreadable, or unequal vendored inputs as unblessable.

### Component-scoped update

`verify --components a --update` could verify `a` and then regenerate broader
state, silently accepting drift in unselected `b`. The corrected workflow
computes a complete candidate first, refuses a partial update if any unselected
entry is stale, writes the complete selected component entries, and recomputes
all slices.

### Field feedback after the first corrective pass

Testing 0.10 against a production-style service graph exposed several
assumptions that the earlier re-audit still had not challenged:

- `--allow-partial` could generate null boundary/compat state that the default
  verifier rejected at preflight. The corrective contract now relaxes only
  intentional null slice inputs and keeps all extraction failures fatal.
- Immediate consumer output was insufficient for shared layers. The v3 model
  now distinguishes validated internal edges from opaque external terminals
  and offers opt-in transitive impact without changing direct output by
  default.
- One global facet policy forced heterogeneous repositories toward the loosest
  gate. Component policy now overrides defaults, while an explicit CLI policy
  intentionally overrides all components.
- A compatibility gate with no version source compared null with null and could
  appear clean. Selecting an unavailable facet now exits `2`; the implicit
  fallback means all facets actually available for the component.
- Explicit slice membership drifted away from the declared graph. `closure_of`
  now stores the seed and its deterministic downstream component closure.
- Machine-readable `status` output already existed in 0.10. The new output
  surfaces are `why --format json` and `slice --format json`.

The largest remaining product gap is derived-artifact freshness. A generated
OpenAPI document can be internally consistent yet stale relative to its SAM or
source template. v3 does not execute repository-configured commands or claim to
prove that relationship. Documentation requires a deterministic generator
`--check` before boundver and requires index workflows to stage derivation
source, output, config, and lock coherently. A first-class declarative derivation
contract remains roadmap work because trust, tool identity, and snapshot
materialization need a design rather than a shell-string shortcut.

## The 0.10 release record

The former changelog mixed features that already existed in v0.9.1 with actual
v0.10 additions. The corrected changelog now treats these as the principal
v0.10 changes:

- facet gates and non-gating observations;
- severity-specific exit codes;
- `verify --update`;
- consumer metadata and reporting;
- Git-aware hardening of existing discovery;
- provider metadata, isolated registries, custom-provider opt-in, and additional
  validation/hardening;
- the structured verify-only Action interface, Python 3.9 floor, packaging,
  community, and release-automation work.

Behavior fingerprints, slices, general discovery commands, canonical providers,
JSON/YAML/TOML support, several CLI commands, Docker, standalone archives,
pre-commit support, and a Marketplace Action existed before v0.10 and are no
longer presented as new in that release.

The original squash subject, `Release boundver 0.10.0 (#10)`, was too generic
for a change of roughly ninety files and obscured the breaking Action, exit-code,
Python-floor, trust-model, and lock changes. Published commit history must not be
rewritten. A corrective commit and PR should instead name the contract change,
for example: `feat!: define v3 path contracts and close 0.10.0 release gaps`.

## Release-process failure

PR #10 was merged and an unresolved high-priority review identified the
vendored-copy strict-generation failure before the release tag was created. The
tag workflow checked that the candidate matched current `main` and the package
version, but it did not inspect unresolved review threads or a changes-requested
state for pull requests represented by commits since the previous release.
Consequently the release process allowed a known P1 correctness defect to ship
while the project review claimed complete resolution.

The corrective pre-tag workflow determines the prior SemVer release, finds
pull requests associated with commits in the release range, and paginates
their reviews, comments, and review threads. It rejects changes-requested
state, unresolved threads, pending review requests, API failures, and stale or
missing exact-commit evidence. Normal evidence is a non-author human approval
from a collaborator with push access. Because the repository is personal and
its owner cannot approve the owner's own PR, an owner-authored PR may instead
use a trusted Codex `COMMENTED` review, once any findings are resolved, or a
clean Codex issue-comment marker anchored to the latest PR head or merge
commit. That exception pins the GitHub App bot's numeric account ID and
resolves abbreviated commit markers through GitHub, so a matching display name
or stale SHA is not sufficient. Manual memory is no longer the designed
release invariant.

Publication now separates the lifecycle invariants correctly: before tagging,
the fully tested candidate must equal current `main`; publication validates that
the release SHA remains on `main` and the version tag resolves to that SHA; after
environment approval and again before the GitHub Release, it revalidates the
immutable tag/SHA rather than requiring the candidate to remain the branch tip.
This permits normal forward progress on `main` without stranding a tested tag.
The wheel, sdist, and standalone archive are built twice from a fixed epoch and
must be byte-identical before one candidate is retained. The wheel and sdist are
then uploaded once as one immutable GitHub artifact and every later job selects
that numeric artifact ID, never its reusable name. A protected
`testpypi` Trusted Publishing job runs before production: the release must be
absent, an exact partial upload, or byte-identical on an idempotent rerun. The
workflow then compares filenames, sizes, API SHA-256 values, and downloaded
bytes; installs the hash-pinned TestPyPI wheel with `--no-index --no-deps`; and
prepares a complete checksummed GitHub Release draft. The owner publishes that
draft with Marketplace consent before production approval; the same Python
artifact ID then reaches PyPI, where public bytes, clean installation, and
trusted-publisher provenance are verified. The breaking 0.11 release advances
only `v0.11`; it deliberately leaves the public `v0` alias on 0.10.

## Usability and visibility findings

Visibility improvements are only credible when the first example is executable
and limitations are prominent. The maintained docs now:

- lead with an outcome and a short install/init/validate/generate/verify path;
- identify the v3 contract and its v2 migration boundary without implying that
  published v0.10 already has v3 behavior;
- include a four-example glob table that resolves the original contradiction;
- distinguish validated internal consumer edges from opaque external terminal
  labels and show direct versus transitive impact;
- explain per-component facet precedence and fail unavailable selected facets;
- make generated-artifact freshness checks and the complete index staging
  workflow explicit;
- explain source tracking and source consistency before users commit a lock;
- explain that facets select reporting/gating while updates write coherent
  entries;
- provide a v2-to-v3 regeneration path instead of suggesting relabelling; and
- avoid claiming that canonicalization or fingerprints prove compatibility.

The previous public-star/Marketplace/PyPI visibility snapshot is not reused as
current evidence. Public counts, listings, and latest-version claims are
time-sensitive and must be rechecked at the time they are reported.

## Known product limits

- Detection is limited to declared artifacts and tracked source. Untracked files
  do not enter established-repository fingerprints.
- `working-tree` cannot be an immutable snapshot; it uses a captured tracked
  path set and fails when reads become invalid, but concurrent disk edits remain
  an environmental race. Use `head` or `index` for reproducible gates.
- Canonical JSON/OpenAPI output is boundver-specific normalization, not general
  semantic-diff or compatibility analysis.
- Trusted custom-provider Python is an installed execution-environment input,
  not part of the selected Git snapshot; pin its distribution and version.
- Consumer impact follows only explicitly declared internal edges. Transitive
  closure is available, but dependency discovery is not.
- Derived artifacts have no first-class freshness relationship. Run a
  deterministic generator check before verification.
- Discovery is intentionally conservative. It cannot infer the correct public
  boundary or root-package version mapping for every repository layout.
- A compatibility fingerprint records the configured version family; it does
  not prove that a version bump is sufficient or that an unchanged version is
  honest. File and reachable `git_tag_prefix` sources exist; sibling-derived or
  constant identities remain roadmap possibilities rather than inferred data.

## Release-validation boundary

This retrospective intentionally does not track test totals, working-tree
state, checklist completion, or service configuration. Those facts change as a
candidate moves through review and would make a historical design record stale.
Maintainers use `docs/RELEASING.md` and
`python3 scripts/publish_release.py check --tag vX.Y.Z` for the current,
fail-closed gate. The matching workflow run is the evidence for exact-commit CI,
review state, artifacts, registry rehearsal, publication, and Action aliases.
