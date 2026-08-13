# Releasing boundver

This is the maintainer runbook for promoting one reviewed commit through every
public boundver surface. A release is complete only when the same version and
commit are visible on GitHub, GitHub Marketplace, PyPI, TestPyPI, and every
Action alias that was explicitly approved for that compatibility line.

Do not repair a partial release by rebuilding from a different checkout,
moving the immutable version tag, replacing release assets, or reusing a PyPI
version. Use **Re-run failed jobs** on the original workflow run so its
successful build job and artifact IDs are retained; do not use **Re-run all
jobs** or start a fresh dispatch after an index upload. Release artifacts are
retained for 90 days, but GitHub permits workflow reruns only during the first
30 days after the initial run. The build also has to prove that the exact
tagged source is byte-reproducible before its first upload.

## One-time service setup

The repository owner must configure these controls before starting a release:

- Enable immutable GitHub Releases.
- Add an active repository tag ruleset that blocks updates and deletion for
  exact SemVer tags with `refs/tags/v*.*.*`. It must not target mutable
  compatibility aliases such as `v0.11`, and it must not restrict initial tag
  creation. The publishing gate verifies this before creating a version tag;
  immutable-release protection takes over once the Release is published.
- Create protected GitHub environments named `testpypi`, `pypi`, and
  `marketplace`. Require at least one reviewer for every environment; a wait
  timer alone is not approval.
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

Record configuration changes through normal repository/owner review. Never add
an API token or `.pypirc` to this repository; PyPI uploads use short-lived OIDC
credentials from the protected environments.

## Public surface matrix

| Surface | Source of truth | Release requirement |
|---|---|---|
| Git source and documentation | Exact commit on `main` | Version, schemas, examples, migration text, and links describe the release—not a future or already-published state. |
| Version tag | `vX.Y.Z` | Immutable tag resolves to the tested `main` commit. |
| TestPyPI | Verified wheel and sdist artifact | File names and SHA-256 digests equal the candidate; the wheel installs directly from TestPyPI. |
| PyPI | The same verified wheel and sdist artifact | Trusted publication, metadata/README/links correct, direct wheel install succeeds. |
| GitHub Release | Draft prepared from the version tag | Exact changelog notes and verified assets are attached before publication and immutability. |
| GitHub Marketplace | The GitHub Release plus `action.yml` | Owner selects **Publish this Action to the GitHub Marketplace** while publishing the prepared draft; listing reports `vX.Y.Z` as Latest. |
| Stable Action aliases | Mutable compatibility tags such as `vX.Y` | Advanced only after the exact release and Marketplace listing verify; never create a GitHub Release for aliases and never move a broader alias across a breaking Action contract without explicit approval. |
| Pre-commit | Git tags | Exact `rev: vX.Y.Z` and compatible aliases resolve to the release commit. |
| Standalone archive | Versioned GitHub Release asset | Reports/imports the release version and contains the license and bundled schema. |
| Dockerfile | Release commit | Built and exercised by exact-commit CI. There is currently no promised public container registry; add one only with an explicit supported-image policy. |

The source distribution contains user-facing guides, specifications, examples,
and community files. It deliberately excludes tests, repository automation,
`docs/PROJECT_REVIEW.md` and this maintainer runbook: both remain available in
the source repository, but neither is package runtime or end-user reference
material.

PyPI and Marketplace render the README embedded in the distribution or tag.
They do not update when a later documentation commit lands. All public-facing
release wording therefore belongs in the release commit.

## Prepare the release pull request

1. Choose `X.Y.Z` according to SemVer and set the same value in
   `pyproject.toml`.
2. Move completed notes from `## [Unreleased]` into exactly one
   `## [X.Y.Z] - YYYY-MM-DD` section. Leave a new, empty Unreleased section and
   update comparison links.
3. Remove temporary “unreleased” or “use after publication” wording from
   README and guides. Check every install command, Action reference, lock
   schema, upgrade instruction, and version-specific link.
4. Pin machine-readable schema URLs and `$id` values to the exact release tag.
   Living documentation may link to `main`; generated configs and locks must
   not silently adopt a future branch schema.
5. Regenerate the repository and all example locks from the final hash/provider
   contract. Validate every config, schema, lock, and machine-readable example.
6. Run `python3 scripts/check_repo_hygiene.py`; remove generated caches and
   build outputs, then confirm only intentional source and release files remain.
7. Build the wheel, sdist, and versioned standalone archive. Run Twine checks
   and install each Python distribution in a clean environment.
8. Confirm `action.yml`, the Dockerfile, and both pre-commit hooks execute the
   public installed form. Test the supported Python/OS matrix.
9. Resolve every relevant review thread and blocking review. Merge only after
   the exact release-preparation commit passes required CI.

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

```bash
python3 scripts/publish_release.py check --tag vX.Y.Z
sha=$(git rev-parse HEAD)
python3 scripts/publish_release.py start --tag vX.Y.Z \
  --alias vX.Y --confirm "vX.Y.Z@$sha"
```

Use `--alias none` when no compatibility alias is approved. `start` repeats
the complete gate and makes exactly one local-side mutation: it dispatches the
tag-creation workflow with the explicitly confirmed tag, SHA, and alias policy.
It never creates a local tag, uploads a package, publishes a Release, or calls
the publication workflow directly. The protected workflows then perform these
gates in order:

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
   TestPyPI file's trusted-publisher attestation against this repository.
5. Prepare a draft GitHub Release with the exact changelog notes and release
   assets. Draft first so immutable-release protection does not freeze an
   incomplete asset set.
6. Open the prepared GitHub draft. Select **Publish this Action to the GitHub
   Marketplace**, confirm its categories and version, then publish with 2FA.
   This owner-only step makes the release immutable, so verify the complete
   asset set before clicking publish.
7. Keep the original workflow run waiting at the protected `marketplace`
   environment while the owner publishes the draft. Once approved, that same
   run revalidates the immutable tag, release assets, Marketplace listing, and
   TestPyPI candidate. After approval in the `pypi` environment, publish the
   same identified wheel/sdist artifact to production PyPI and verify its
   remote hashes, metadata, clean installation, and trusted-publisher
   attestations. Do not rebuild in a second event-triggered run.
8. Verify every public surface, then advance only the Action aliases approved
   for this compatibility line. For breaking `0.11.0`, create or advance
   `v0.11`; leave the existing `v0` alias on the compatible `0.10.x` line unless
   an owner explicitly approves and announces that migration.

If any gate fails, stop. PyPI/TestPyPI versions and immutable GitHub releases
cannot be overwritten. A retry is valid only when every pre-existing remote
file has the expected digest and **Re-run failed jobs** reuses the original
successful build and artifact IDs. A fresh dispatch is a new candidate and is
not the recovery path for an interrupted promotion.

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
