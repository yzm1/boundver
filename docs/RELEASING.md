# Releasing boundver

This is the maintainer runbook for promoting one reviewed commit through every
public boundver surface. A release is complete only when the same version and
commit are visible on the documentation site, GitHub, GitHub Marketplace,
PyPI, TestPyPI, GHCR, the Homebrew tap, the GitLab CI/CD Catalog, and every
Action alias that was explicitly approved for that compatibility line.

Do not repair a partial release by rebuilding from a different checkout,
moving the immutable version tag, replacing release assets, or reusing a PyPI
version. Prefer **Re-run failed jobs** on the original workflow run so its
successful build job and artifact IDs are retained; do not use **Re-run all
jobs**. If the original run is completed and failed, the `resume` command
described below is the only supported new dispatch: it proves and reuses that
run's exact retained artifacts and its logged compatibility-alias policy
instead of creating another candidate. Never dispatch `publish.yml` directly.
Release artifacts are retained for 90 days, but GitHub permits workflow reruns
only during the first 30 days after the initial run. The build also has to
prove that the exact tagged source is byte-reproducible before its first
upload.

## One-time service setup

The repository owner must configure these controls before starting a release:

- Enable immutable GitHub Releases.
- Add an active repository tag ruleset that blocks updates and deletion for
  exact SemVer tags. In GitHub's **Include by pattern** field, enter
  `v*.*.*`; GitHub represents that pattern in ruleset API responses as
  `refs/tags/v*.*.*`, which is the form verified by the publishing gate. It
  must not target mutable compatibility aliases such as `vX.Y`, and it must
  not restrict initial tag creation. Immutable-release protection takes over
  once the Release is published.
- Keep the checked-in `.github/rulesets/protect-main.json` contract active for
  `refs/heads/main`. It requires pull requests, resolved review conversations,
  and the strict `required-pr-gate` status from the GitHub Actions App; it also
  blocks deletion and force pushes. The aggregate check fails unless the full
  supported-platform matrix plus build, public Action, and public installation
  jobs all succeed. The status is published by `required-pr-gate.yml`, which
  runs trusted code from the pull request's base commit after `CI` completes;
  it never executes pull-request code or consumes its artifacts. It also rejects
  any pull request that changes a workflow, the gate verifier, or the checked-in
  ruleset contract. Those path checks use GitHub's immutable base-SHA/head-SHA
  comparison rather than the mutable pull-request files endpoint. This keeps a
  force-push race or pull request from weakening the policy that judges it and
  still permits contributions from forks. Zero mandatory human approvals keeps
  ordinary maintenance workable for the solo-owned repository;
  exact Codex and independent release review evidence is enforced separately by
  the review auditors. The ruleset has no bypass actor. Release checks compare
  its complete effective API policy with the checked-in contract, including
  GitHub-added defaults, and reject any other active ruleset that also targets
  `main`. Classic branch protection must remain absent because GitHub would
  enforce it in addition to the ruleset; an inherited or stale rule cannot be
  treated as harmless.
- A necessary change to a protected gate control requires a short, auditable
  maintenance window. Freeze the exact reviewed pull-request head, require all
  ordinary CI and review gates, record the reason and exact commit in its issue,
  temporarily remove only this ruleset's `required_status_checks` rule while
  keeping enforcement active, squash-merge that frozen head, and immediately
  restore the canonical active ruleset from the merged JSON. Verify the live
  ruleset after restoration and keep the issue open until a subsequent ordinary
  pull request proves that the base-controlled status is emitted by the GitHub
  Actions App. Do not add a bypass actor or weaken the tracked contract.
- Enable private vulnerability reporting, Dependabot alerts and security
  updates, secret scanning, and secret-scanning push protection. Keep every
  alert queue at zero before release; the promotion gate checks dependency,
  secret, and code-scanning alerts directly.
- Require full-length commit SHA pins for third-party GitHub Actions. Set the
  default workflow token to read-only and do not allow it to approve pull
  requests. Keep `.github/workflows/codeql.yml` active so every exact `main`
  release commit receives a successful Python `security-extended` analysis.
- Set GitHub Pages to **GitHub Actions** and retain the protected
  `github-pages` deployment environment created by Pages.
- Create protected GitHub environments named `testpypi`, `pypi`,
  `marketplace`, `container`, `container-public`, and `action-alias`. Require at
  least one reviewer for every release environment; a wait timer alone is not
  approval. `action-alias` pauses the workflow until the owner has performed
  the separately confirmed local compatibility-tag handoff.
  `container` authorizes the registry write. `container-public` is approved
  only after the exact GHCR package is publicly readable, which matters on its
  first publication because a new GHCR package is private by default.
- Configure separate trusted publishers for the `boundver` project on PyPI and
  TestPyPI. Both publishers must identify repository `yzm1/boundver`, workflow
  `.github/workflows/publish.yml`, and their matching environment name. The two
  indexes have separate accounts and publisher configuration.
- Accept the GitHub Marketplace Developer Agreement and retain the Action's
  primary and secondary categories. Marketplace publication requires an owner
  using two-factor authentication; repository configuration cannot perform
  that consent step.
- Keep the repository homepage pointed at the public Marketplace listing and
  keep the repository description/topics current. These are discovery
  metadata, not versioned release content.
- In `boundver-project/boundver` on GitLab, protect `main` against force pushes
  and protect tags matching `*.*.*`, allowing tag creation only to Maintainers.
  Verify the new tag reports `protected: true` before accepting its Catalog
  release. The checked-in GitLab pipeline independently refuses to publish when
  `CI_COMMIT_REF_PROTECTED` is not `true`.
- Keep the GitHub social preview and GitLab Catalog project avatar synchronized
  with `docs/assets/social-preview.png` and `docs/assets/logo.png`, which mirror
  the production sources under `assets/brand/`. Marketplace
  Action badges support only GitHub's approved Feather icons and colors, so
  `action.yml` uses the closest native branding rather than a custom image.
- Maintain the public `yzm1/homebrew-boundver` tap. Formula changes must use
  the immutable `.pyz` release asset and its SHA-256, pass `brew audit` and
  `brew test`, and merge through normal review.
- Mirror the release source into the GitLab Catalog project with top-level
  `templates/`. GitHub tags retain the `vX.Y.Z` spelling; the GitLab component
  release uses the Catalog's exact `X.Y.Z` semantic-version tag and must point
  at the matching GHCR image. Never reuse a GitLab version tag.

Record configuration changes through normal repository/owner review. Never add
an API token or `.pypirc` to this repository; PyPI uploads use short-lived OIDC
credentials from the protected environments.

## Required-gate control maintenance

The base-controlled `required-pr-gate` deliberately rejects a pull request that
changes `.github/workflows/`, the tracked main-ruleset snapshot
`.github/rulesets/protect-main.json`, or the gate validator
`scripts/check_required_ci_results.py`. Those control paths can change what
counts as successful CI, so a candidate must not authorize its own merge.
Control-plane maintenance therefore uses a narrow ruleset transaction:

1. Freeze and record the pull request's full head SHA. Obtain a fresh review of
   that exact head, resolve every review thread, and require every ordinary CI
   job in the current base-controlled topology to succeed. Do not update the
   branch afterward.
2. Record the reason, frozen SHA, CI run, review evidence, and intended
   transaction on the pull request or its tracking issue before changing the
   live ruleset.
3. Read and retain the complete effective branch ruleset from GitHub. Confirm
   that it is active, targets only `refs/heads/main`, has no bypass actors, and
   contains deletion, non-fast-forward, squash-only pull-request/thread-
   resolution, and `required-pr-gate` status rules.
4. Temporarily remove only `required_status_checks`. Keep the ruleset active
   and preserve every condition, bypass-actor list, and other rule exactly.
   Do not add a bypass actor, allow a direct push, or disable the ruleset.
5. Squash-merge through the pull-request API only when its current head still
   equals the frozen SHA. Never push the candidate directly to `main`.
6. In a `finally` path, restore the exact retained ruleset whether the merge
   succeeds or fails. Re-read the effective server state and compare every
   condition, rule, parameter, enforcement value, and bypass actor before doing
   any follow-up work.
7. Record the merge commit and restored effective state. A failed merge is not
   retried until the required-status rule has already been restored and the new
   blocker has been resolved.

This transaction exists only because the gate cannot approve a modification to
itself. Ordinary pull requests must receive the base-controlled
`required-pr-gate` success and never use this procedure.

## Public surface matrix

| Surface | Source of truth | Release requirement |
|---|---|---|
| Git source and documentation | Exact commit on `main` | Version, schemas, examples, migration text, and links describe the release—not a future or already-published state. |
| Project branding | SVG sources and generated exports under `assets/brand/`, mirrored site assets under `docs/assets/`, and `social-preview.*` | README, hosted docs, favicons, repository social preview, and GitLab Catalog avatar agree; the release gate requires the complete responsive and monochrome asset family. Historical immutable releases are not rewritten. |
| Hosted documentation | `mkdocs.yml`, `docs/`, and the hash-locked docs profile | Strict build passes and GitHub Pages deploys the exact `main` documentation commit. |
| Version tag | `vX.Y.Z` | Immutable tag resolves to the tested `main` commit. |
| TestPyPI | Verified wheel and sdist artifact | File names and SHA-256 digests equal the candidate; the wheel installs directly from TestPyPI. |
| PyPI | The same verified wheel and sdist artifact | Trusted publication, metadata/README/links correct, direct wheel install succeeds. |
| GitHub Release | Draft prepared from the version tag | Exact changelog notes and verified assets are attached before publication and immutability. |
| GitHub Marketplace | The GitHub Release plus `action.yml` | Owner selects **Publish this Action to the GitHub Marketplace** while publishing the prepared draft; listing reports `vX.Y.Z` as Latest. |
| Stable Action aliases | Mutable compatibility tags such as `vX.Y` | Advanced only after the exact release and Marketplace listing verify; never create a GitHub Release for aliases and never move a broader alias across a breaking Action contract without explicit approval. |
| Pre-commit | Git tags | Exact `rev: vX.Y.Z` and compatible aliases resolve to the release commit. |
| Standalone archive | Versioned GitHub Release asset | Reports/imports the release version and contains the boundver license, bundled schema, and lock-pinned pure-Python PyYAML runtime plus license; a dependency-free environment passes YAML config, version-source, and OpenAPI generation. |
| GHCR container | Release commit and `Dockerfile` | `linux/amd64` and `linux/arm64` share one exact version tag, immutable manifest digest, release SHA label, public pull, and GitHub artifact attestation. Do not publish `latest`. |
| Homebrew | Immutable standalone archive | `yzm1/homebrew-boundver` installs the exact `.pyz` release asset with its reviewed SHA-256 and passes tap audit/test before merge. |
| GitLab CI/CD Catalog | `templates/boundver.yml` mirrored to GitLab | Exact Catalog version runs the matching GHCR image; GitLab's release tag is `X.Y.Z`, not GitHub's `vX.Y.Z`. |

The source distribution contains user-facing guides, specifications, examples,
and community files. It deliberately excludes tests, repository automation, and
this maintainer runbook, which remains available in the source repository but is
neither package runtime nor end-user reference material.

PyPI and Marketplace render the README embedded in the distribution or tag.
They do not update when a later documentation commit lands. All public-facing
release wording therefore belongs in the release commit.

Automation dependencies use pip's secure-install model. The reviewed version
policy is `scripts/release-tool-lock.toml`; its purpose-specific Action, CI,
docs, and release profiles generate `scripts/requirements/*.lock`. CI and release
each reuse the minimal Action base without sharing unrelated tools. Every direct and
transitive requirement is exact-pinned, and every permitted non-yanked wheel
has a checked-in SHA-256 digest. Installs force `--require-hashes` and
`--only-binary :all:` against the canonical PyPI index, while retaining pip's
dependency resolver so incompatible exact pins fail before automation runs.
Local boundver source is then installed offline with
`--no-deps --no-build-isolation`, so neither its runtime nor build backend can
open an unchecked resolver path.

The Dockerfile uses that same Action lock to download a hash-verified
wheelhouse, builds boundver with dependencies and build isolation disabled, and
performs the final install offline. The builder clamps archive timestamps with
`SOURCE_DATE_EPOCH`, so identical source inputs produce identical wheel bytes.
Its official Python base is pinned to a
multi-architecture manifest digest, and its required Git client resolves from
the immutable Debian snapshot recorded by that base image. Review base-digest
and snapshot updates together; the build verifies that they still match and
fails closed on an incomplete update. Digest pinning deliberately stops
automatic security updates, so weekly Docker Dependabot checks surface base
updates for review.

The runtime image is scanned at high and critical severity for both supported
architectures. `.trivyignore.yaml` may contain only temporary, package-scoped
Debian exceptions for findings with no vendor fix: give every entry an exact
CVE, one or more `pkg:deb/debian/...` PURLs, a reachability statement, and an
expiry no more than 14 days away. Before expiry, rebuild against the current
pinned base, verify the Debian security status, remove fixed or unreachable
packages where possible, and renew only independently justified residuals.
CI performs a second scan with `--ignore-unfixed` and no exception file, so a
newly fixable high/critical issue fails immediately. It also scans the source
tree for high/critical secret and configuration findings. Never use a wildcard,
unscoped CVE, non-expiring entry, or global `--ignore-unfixed` as the primary
publication gate.

`scripts/release-tool-artifacts.json` records the PyPI wheel filenames behind
the local hashes for review. The generator reads only the official versioned
PyPI JSON API over HTTPS and rejects any pin with an active advisory in that
version metadata. Copying the accepted digests into a reviewed commit makes
them local trust inputs rather than accepting an index-provided hash during an
install. The Linux/Python 3.12 CI job and both release-promotion workflows run
the networked `check` again so an advisory published after lock generation
fails closed. The Action and CI locks include wheels for Python 3.10+ on Linux,
macOS Intel and Apple silicon, and Windows; pip still selects only the
compatible wheel. The larger release profile is used by Linux promotion jobs.
Its advisory-free cryptography pin also has upstream wheels for Windows and
Apple silicon, but not macOS Intel, so do not use an Intel Mac as the release
build host. The release-profile installer rejects Python older than 3.12
before contacting the package index.

Use Python 3.12 or newer to maintain the locks:

```bash
# Networked, intentional dependency update; inspect every resulting diff.
python scripts/lock_release_tools.py generate

# Network-free manifest/artifact/lock consistency check.
python scripts/lock_release_tools.py verify

# Networked proof that checked-in bytes still match official PyPI metadata
# and that every pin remains free of active advisories reported there.
python scripts/lock_release_tools.py check
```

Never hand-edit a generated lock or artifact-evidence file. A changed version
belongs in the TOML policy first. A newly published wheel at an already pinned
version is also a trust-set change and must arrive through a reviewed generated
diff; it is never accepted implicitly by an install.

## Prepare the release pull request

1. Choose `X.Y.Z` according to SemVer and set the same value in
   `pyproject.toml`.
2. Move completed notes from `## [Unreleased]` into exactly one
   `## [X.Y.Z] - YYYY-MM-DD` section. Leave a new, empty Unreleased section and
   update comparison links. Releases from 0.14 onward must start that section
   with one machine-checked `### Upgrade contract` block:

   ```markdown
   ### Upgrade contract

   - Semantic config: `boundver-semantic-config/v2`
   - Lock schema: `boundary-lock/v3`
   - Fingerprint compatibility: `digest-neutral`
   - Lock regeneration: `not-required`
   ```

   Use `digest-changing` with `required` or `conditional` when appropriate.
   The readiness gate compares the declared semantic config and lock schema
   with the candidate specifications and rejects contradictory regeneration
   guidance.
3. Remove temporary “unreleased” or “use after publication” wording from
   README and guides. Check every install command, Action reference, lock
   schema, upgrade instruction, and version-specific link.
4. Pin configuration and CLI-output schema URLs and `$id` values to the exact
   release tag. Keep the persisted lock's `$schema` and the lock schema `$id`
   on the immutable canonical release for that structural schema; v3 is frozen
   at v0.13.0. Advance that canonical reference only together with a new lock
   schema identity. Living documentation may link to `main`; generated configs
   must not silently adopt a future branch schema.
   Between releases, a changed configuration or CLI-output schema uses a
   `main` `$id`, while generators continue to reference the last released
   schema until release preparation pins the changed document to its new exact
   tag. The canonical lock-schema publication is the intentional exception.
5. Regenerate the repository and all example locks from the final hash/provider
   contract. Validate every config, schema, lock, and machine-readable example.
6. Run `python3 scripts/check_repo_hygiene.py`; remove generated caches and
   build outputs, then confirm only intentional source and release files remain.
7. Review `scripts/release-tool-lock.toml`, regenerate the locks, run both
   `verify` and `check`, then inspect every version, filename, and SHA-256 diff.
   Build the wheel, sdist, and versioned standalone archive. Run Twine checks
   and install each Python distribution in a clean environment. In a separate
   environment with no installed PyYAML, exercise the archive with a YAML
   config, YAML version source, and YAML OpenAPI boundary.
8. Run both public demos (`demo_consumer_impact.py` and
   `demo_range_review.py`), build MkDocs with `--strict`, validate the GitLab
   component source, and render a Homebrew formula from a known release digest.
   Confirm `action.yml`, the Dockerfile, and all three pre-commit hooks execute
   the public installed form. Test the supported Python/OS and container
   architecture matrices.
9. Confirm the Homebrew tap and GitLab Catalog project are writable by their
   reviewed promotion paths, and that all six release environments still
   require a reviewer.
10. Resolve every relevant review thread and blocking review. Merge only after
   the exact release-preparation commit passes required CI.

The pre-tag review audit fails closed on API or pagination errors, unresolved
threads, changes-requested state, and pending human or team review requests.
Its range begins at the newest lower stable, published, immutable GitHub
Release whose tag is merged into the candidate and whose tag and commit match a
successful run of the repository's active `publish.yml`. That run must bind the
exact tag, commit, release-line alias or explicit `none`, optional recovery
source, repository, and a workflow control commit in candidate history; the
release publication time must not postdate that run's successful completion.
Standalone tags, drafts,
prereleases, mutable historical releases, unmerged releases, and releases
created without that publication provenance cannot narrow the range. The
read-only audit and the workflow-owned review-state snapshot derive this anchor
independently from the paginated Releases and Actions APIs plus the fetched Git
graph. Malformed,
unavailable, or calendar-invalid timestamps fail closed.
Each contributing PR also needs current, exact-commit evidence: an approval by
a non-author collaborator with push access, or—only for an owner-authored PR in
this personal repository—a review from the trusted Codex GitHub App account.
Codex evidence must be an authenticated record for the latest PR head or merge
commit. A standard Codex suggestions review counts only after every review
thread is resolved. A later exact-commit record supersedes earlier feedback;
equal timestamps are ambiguous and fail closed. A clean verdict line must be
exactly `Codex Review: Didn't find any major issues.`, optionally followed by
the positive allowlist `Another round soon, please!`, `Bravo.`, `Breezy!`,
`Can't wait for the next one!`, `Delightful!`, `Hooray!`, `Keep it up!`,
`Keep them coming!`, `Nice work!`, `Swish!`,
`Already looking forward to the next diff.`, `Chef's kiss.`,
`More of your lovely PRs please.`, `What shall we delve into next?`, or
`You're on a roll.`, plus the literal GitHub emoji codes `:rocket:` and
`:tada:`.
Arbitrary or adverse latest bodies fail closed. A `COMMENTED` review binds its
body to the review commit; an issue comment must also contain exactly one
`**Reviewed commit:**` marker. The audit permits only the bot's recognized
informational footer, pins the bot's numeric account ID, resolves abbreviated
commit IDs, and snapshots record IDs, timestamps, bodies, threads, and those
resolutions to detect collisions or drift before mutation. It rejects spoofed
identities, ambiguous commit IDs or timestamps, unresolved feedback, or stale
evidence. Every timestamp used for ordering is checked as a real UTC calendar
instant, including leap-year and fractional-second handling.
An authenticated, exact-commit Codex security-review no-findings comment is a
separate evidence channel: the audit recognizes its complete fixed shape and
treats it as neutral. It neither satisfies nor supersedes the required code
review. A malformed, marker-mismatched, or finding-bearing security comment
remains adverse and fails closed. Code-review evidence cannot clear adverse
security evidence; only a later valid security-review result for the same
current commit can do so.

The release PR subject should name the user-visible contract. Avoid generic
subjects such as “release changes.” For the v3 transition, an appropriate
subject is:

```text
feat!: introduce boundary-lock/v3 and close v0.10 integrity gaps
```

## Promote the exact commit

The publishing script is the only supported local release entry point. Its
`check` command is read-only: it rejects a dirty or non-`main` checkout, checks
the canonical origin and exact remote-main SHA, inspects GitHub controls and
CI, and runs readiness, review, test, reproducible-build, Twine, TestPyPI, and
PyPI preflights in a disposable checkout.

Candidate-owned commands receive a minimal allowlisted process environment.
The maintainer launchers additionally reject repository-local `git` or `gh`
executables, force authenticated API reads to `github.com`, disable Git hooks,
fsmonitor callbacks, replacement refs, lazy object fetching, and interactive
credential prompts, and bound subprocess output and wall time. Do not bypass
those failures by running equivalent ad hoc Git or GitHub CLI commands.
The credential-free repository-hygiene, candidate-verification, build-epoch,
and compatibility-alias helpers enforce the same external-Git and callback
controls; a local executable or replacement ref must never become release
evidence.
Their home, temporary, Git, GitHub CLI, pip, container, cloud, and XDG paths all
point into disposable directories, so maintainer home-directory credentials and
ambient CI/cloud variables are not inherited. Inline parsers, environment
creation, and pip/tool installation also start with Python isolated mode
(`-I`), with pip retaining its own `--isolated` configuration mode. This
isolates credentials; it does not provide a network sandbox. Candidate checks
intentionally contact GitHub and package indexes. Before `check` or `start`, set
`BOUNDVER_RELEASE_REVIEW_TOKEN` to a fine-grained token restricted to this
repository with read-only Contents, Issues, Metadata, and Pull requests access.
The gate does not fall back to the maintainer's ambient `gh` token, and only the
trusted review-audit subprocess receives this credential. That audit collects
and evaluates GitHub evidence first, then runs the exact reviewed semantic
proposal checker in a separate bounded Python process whose environment omits
the review token and all other ambient credentials. Run the local gate
only on a trusted, isolated maintainer host and review changes to release
scripts as security-sensitive code.

On Windows, use the checked-in PowerShell wrapper. It prompts through
`Read-Host -AsSecureString`, exposes the token only to the isolated Python
launcher for that invocation, restores the caller's process environment, and
zeroes the unmanaged plaintext buffer before returning. It also rejects a
repository-local Python executable. Do not pass the token as a command-line
argument or paste it into a terminal command.

```powershell
.\scripts\publish_release.ps1 check --tag vX.Y.Z
$sha = git rev-parse HEAD
.\scripts\publish_release.ps1 start --tag vX.Y.Z `
  --alias vX.Y --confirm "vX.Y.Z@$sha"
```

On macOS or Linux, prompt without echoing and invoke the same Python launcher:

```bash
read -rsp "Read-only release-review token: " BOUNDVER_RELEASE_REVIEW_TOKEN
export BOUNDVER_RELEASE_REVIEW_TOKEN
python3 scripts/publish_release.py check --tag vX.Y.Z
sha=$(git rev-parse HEAD)
python3 scripts/publish_release.py start --tag vX.Y.Z \
  --alias vX.Y --confirm "vX.Y.Z@$sha"
```

Use `--alias none` when no compatibility alias is approved. `start` repeats
the complete gate and makes exactly one local-side mutation: it dispatches the
tag-creation workflow with the explicitly confirmed tag, SHA, and alias policy.
It never creates a local tag, uploads a package, publishes a Release, or calls
the publication workflow directly. Both workflows encode the complete dispatch
identity in their run title. The local command checks for that exact run before
dispatch and polls for it after both successful and ambiguous API responses, so
an accepted request is reused rather than submitted twice. The protected
workflows then perform these gates in order:

1. Reconfirm that package version, current `main`, successful CI, changelog,
   reviews, and Action behavior all refer to the exact same commit.
2. Create the exact `vX.Y.Z` tag and dispatch the publication workflow from
   that tag—not from moving `main`. Immutable-release protection locks the tag
   only when the prepared GitHub Release is published later in this sequence.
3. Build twice from the tagged source with its commit timestamp as the
   reproducible-build epoch. Require identical wheel, sdist, and standalone
   bytes, then upload one candidate wheel/sdist as an identified GitHub Actions
   artifact retained for 90 days. Later index jobs download that exact artifact;
   they do not rebuild it.
4. Publish to TestPyPI with its trusted publisher. Wait for index propagation,
   compare every remote file name and SHA-256 digest with the candidate, then
   install the exact TestPyPI wheel URL with dependencies disabled. Do not use
   an extra index that could substitute the production package. Verify each
   TestPyPI file's trusted-publisher attestation against this repository. The
   OIDC job checks out no repository code: it only downloads the preflight's
   immutable artifact ID and invokes the commit-pinned publisher action. The
   action receives the pinned container's system CA file and directory
   explicitly. This preserves TLS verification after the workflow-wide
   environment neutralizes inherited CA overrides; do not replace those paths
   with empty values, because doing so disables OpenSSL's default trust store.
5. Prepare a draft GitHub Release with the exact changelog notes and release
   assets. A read-only job generates and retains the notes first; the mutation
   job checks out no candidate tree and treats the retained notes and assets as
   data. Draft first so immutable-release protection does not freeze an
   incomplete asset set. `gh release create` exposes a URL but no numeric ID,
   while GitHub's exact-tag endpoint excludes drafts, so the workflow performs
   a six-attempt, two-second authenticated-list visibility retry after creation
   and then binds every later operation to the unique numeric ID. A terminal
   visibility failure reports the canonical release URL for safe recovery.
6. Open the prepared GitHub draft. Select **Publish this Action to the GitHub
   Marketplace**, confirm its categories and version, then publish with 2FA.
   This owner-only step makes the release immutable, so verify the complete
   asset set before clicking publish.
7. Keep the original workflow run waiting at the protected `marketplace`
   environment while the owner publishes the draft. Once approved, that same
   run revalidates the immutable tag, release assets, Marketplace listing, and
   TestPyPI candidate. After approval in the `pypi` environment, publish the
   read-only preflight's immutable subset of the same identified wheel/sdist
   artifact to production PyPI; the OIDC job executes no repository code.
   Verify its remote hashes, metadata, clean installation, and trusted-publisher
   attestations. Do not rebuild in a second event-triggered run.
8. After production PyPI verification, build and publish the exact release
   container for `linux/amd64` and `linux/arm64` under
   `ghcr.io/yzm1/boundver:X.Y.Z`. Record the manifest digest and GitHub
   attestation. For a first package publication, make the package public in
   GitHub's package settings, then approve `container-public`; the verification
   job logs out before proving anonymous pull, labels, runtime version, and
   attestation by digest.
9. After PyPI and the public container verify, leave the publication waiting at
   the protected `action-alias` environment. GitHub's built-in `GITHUB_TOKEN`
   cannot create or update a ref that makes `.github/workflows/` changes
   reachable because it has no separate Workflows permission. Do not approve
   this environment yet. From a clean, current `main` checkout authenticated by
   `gh` as repository owner `yzm1`, copy the active `publish.yml` run ID from
   its Actions URL and perform the explicit handoff:

   ```bash
   release_tag=vX.Y.Z
   release_sha=$(git rev-list -n 1 "$release_tag")
   run_id=123456789
   python3 -I scripts/publish_release.py alias \
     --tag "$release_tag" --alias vX.Y --run-id "$run_id" \
     --confirm "$release_tag@$release_sha#$run_id"
   ```

   The local command requires current reviewed `main`, the immutable exact tag,
   the active publication attempt, successful release/PyPI/public-container
   gates, and the waiting alias-decision job. If `main` advanced after dispatch,
   all credentialed alias-control scripts and their imported helpers must
   remain byte-identical to the parent-reviewed versions. The command
   revalidates the logged release inputs
   at the mutation boundary, rejects non-ancestral or non-monotonic moves, and
   pushes the exact object ID with `--force-with-lease`; it does not leave a
   mutable local tag. The credential stays on the trusted maintainer host and
   is never exposed to candidate code or stored as a repository secret. A normal
   `gh auth login` for `yzm1` supplies the required `repo` and `workflow` OAuth
   scopes. The mutation always uses the canonical HTTPS push URL configured by
   `gh auth setup-git`, even when the checkout's `origin` uses SSH, so the push
   cannot bypass the owner identity verified through `gh`. A fine-grained token
   instead needs access only to `yzm1/boundver` with Actions read, Contents
   read/write, and Workflows read/write.

   After that command succeeds, approve `action-alias`. The parent dispatches
   the dedicated read-only workflow from immutable `vX.Y.Z`. Its child loads the
   reviewed publication controls, proves the parent is still active at the
   exact attempt, verifies every public surface before and after requiring the
   alias, and returns to the parent's final independent comparison. For
   `--alias none`, no local command is needed; approve the environment to record
   that explicit decision. A failed-jobs rerun may reuse exact successful gates
   from an earlier attempt of the same active run, never a different run or
   commit. Advance only aliases approved for the compatibility line; for
   breaking `0.12.0`, use `v0.12` and leave `v0.11` and `v0` on their prior
   lines unless an owner explicitly approves and announces those migrations.

The Homebrew tap and GitLab Catalog are downstream promotion surfaces because
they consume an immutable public Release or GHCR image. After the protected
publication workflow succeeds:

1. Render `Formula/boundver.rb` from `boundver-X.Y.Z.pyz` and the matching
   `SHA256SUMS` entry, run the tap's macOS audit/test workflow, review, and merge
   the formula update.
2. Mirror the exact released source into the GitLab component project, create
   the unprefixed `X.Y.Z` tag, and let `.gitlab-ci.yml` create the Catalog
   Release only after component validation passes. GitLab must have a protected
   tag rule matching `*.*.*` with creation restricted to Maintainers. The
   release job also requires `CI_COMMIT_REF_PROTECTED=true`, so an unprotected
   semver-looking tag cannot publish a Catalog release.
3. Treat the release as incomplete until both public install paths resolve the
   exact version. If either downstream service is unavailable, record the
   partial state instead of substituting mutable assets or credentials.

If any gate fails, stop. PyPI/TestPyPI versions and immutable GitHub releases
cannot be overwritten. A retry is valid only when every pre-existing remote
file has the expected digest and **Re-run failed jobs** reuses the original
successful build and artifact IDs.

If the original `publish.yml` run is completed with conclusion `failure` and
cannot continue through **Re-run failed jobs**, recover from a clean, current
`main` checkout with the numeric ID of that original run:

```bash
release_tag=vX.Y.Z
release_sha=$(git rev-list -n 1 "$release_tag")
run_id=123456789
python3 scripts/publish_release.py resume \
  --tag "$release_tag" --alias vX.Y --run-id "$run_id" \
  --confirm "$release_tag@$release_sha#$run_id"
```

Use `--alias none` only if that was the approved policy for the original
release. Recovery cannot change that choice in either direction. The
confirmation binds three independent facts: immutable tag, full lowercase
release commit, and positive-decimal source run ID. `resume` remains read-only
until its final workflow dispatch. It requires the checkout to be clean and at
current remote `main`; confirms that the tagged release commit is on that main
history; repeats the hygiene, version, exact-main CI, environment, ruleset,
immutability, and serialization checks; rejects a legacy release branch or an
unsafe public GitHub Release; and permits either an existing draft or an
already-public stable, immutable Release for the workflow to reconcile. Release
discovery uses the authenticated paginated release list and binds subsequent
reads to the unique numeric release ID because GitHub's release-by-tag endpoint
does not expose drafts. Initial draft creation applies the same bounded
visibility retry described above; a failed-job rerun first searches for the
existing draft and therefore does not create a second one. A public Release
must already be complete: recovery
will not add missing assets to it or edit it. The workflow compares its title,
tag, notes, exact asset set, and every downloaded asset byte with the retained
candidate before continuing. LF, CRLF, and CR are treated only as equivalent
transport spellings in release notes; all other text remains exact. This public
Release reconciliation runs before any TestPyPI or PyPI upload, so an immutable
conflict cannot strand registry files. It then proves through the GitHub API
that the source was the completed failed `publish.yml`
`workflow_dispatch` for the exact tag and SHA,
and that it contains one uniquely identified successful `verify-release` job.
The command fetches that job's retained log and requires every GitHub-emitted
`RELEASE_TAG`, `RELEASE_SHA`, and `COMPATIBILITY_ALIAS` environment triple to
be complete and to match the requested recovery exactly. Missing, malformed,
or alternate values fail closed. Finally, it requires exactly the two expected,
unexpired, SHA-256-identified release artifacts whose names bind the source run
and successful verification attempt. If **Re-run failed jobs** advanced the run
attempt, the earlier successful verification job, its logged input policy, and
its original two release artifacts remain the accepted source. Strictly named
downstream release-note, container, and disabled Docker diagnostic records are
validated separately; unknown records remain fatal, and more than one retained
container is ambiguous and rejected.

Immediately before its only mutation, `resume` re-reads current remote `main`.
It then dispatches `publish.yml` on `main` with the exact release tag, tagged
SHA, alias policy, and source run ID. The recovery workflow downloads those
identified source artifacts. When the source run retained a container, the
unprivileged verifier independently checks its GitHub artifact ZIP digest and
the extracted OCI bytes, then re-retains that exact file under the current run
for the protected publisher; it does not rebuild or overwrite the immutable
public image. The workflow does not create or move the version tag and must not
rebuild or replace release bytes. Any malformed, stale, ambiguous, or unreadable
GitHub state fails closed. Public-surface checks use the reviewed
recovery-control implementation from current `main`, while release notes and
artifact identity remain bound to the immutable tagged source. Once PyPI and
the public container verify, recovery reaches the same protected
`action-alias` handoff. Run the `alias` command above with the recovery
publication's active run ID, then approve the environment. Current `main`
dispatches `advance-release-alias.yml` from the immutable release tag and waits;
the child checks its publication-control scripts out from the separately bound
current-main commit and only verifies the already-advanced ref. This recovery
path requires v0.13.0 or later immutable tags to contain the exact-tag child
workflow. For an older tag, recovery fails before dispatch unless the approved
alias already resolves to the release SHA. An original `--alias none` policy
needs no local tag handoff, but it cannot be substituted for a release that
approved an alias. An ad hoc fresh publication dispatch is not a recovery path.

## Post-release checks

Use a clean environment and inspect the public services, not local build
outputs:

```bash
python -m pip install --no-cache-dir "boundver[schema,yaml]==X.Y.Z"
boundver --version
boundver --help
```

Then verify:

- `https://pypi.org/project/boundver/X.Y.Z/` has the expected wheel, sdist,
  Python requirement, README, project links, and trusted-publisher attestations.
- `https://test.pypi.org/project/boundver/X.Y.Z/` contains byte-identical test
  uploads. TestPyPI is a rehearsal service and may later prune old data.
- `https://github.com/yzm1/boundver/releases/tag/vX.Y.Z` is public, immutable,
  attached to the expected SHA, and contains the expected assets and notes.
- `https://github.com/marketplace/actions/boundver` marks `vX.Y.Z` as Latest
  and displays the release's current inputs and documentation.
- `https://yzm1.github.io/boundver/` serves the current strict documentation
  build, including the runnable demo, comparison, and distribution guide.
- `docker pull ghcr.io/yzm1/boundver:X.Y.Z` works without authentication;
  both platform manifests resolve, labels bind the release SHA, and
  `gh attestation verify` succeeds for the manifest digest.
- `brew install yzm1/boundver/boundver` installs `X.Y.Z` from the immutable
  `.pyz` asset and the tap formula's checksum matches `SHA256SUMS`.
- The GitLab Catalog lists exact component version `X.Y.Z`, and a minimal
  consumer pipeline runs successfully with `ghcr.io/yzm1/boundver:X.Y.Z`.
- `git ls-remote origin refs/tags/vX.Y.Z refs/tags/vX.Y` resolves the exact tag
  and intended compatibility-line alias to the release commit; broader aliases
  remain on their deliberately supported line.
- A minimal external workflow using `yzm1/boundver@vX.Y.Z` and each alias
  intentionally advanced for the release succeeds.
- The repository README, docs, changelog links, support/security links, and
  badges resolve publicly.

Keep the completed checklist and workflow URLs in the release PR or release
discussion. Do not describe a release as complete while any public surface is
stale.
