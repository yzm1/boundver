# Installation and distribution

Every channel originates from the same reviewed release commit. Prefer an exact
version or immutable digest in CI.

The CLI requires Python 3.10 or newer and Git 2.32 or newer. It verifies both
the selected Git version and the process-local configuration mechanism used by
its security controls before repository inspection. Partial clones require Git
2.45 or newer because earlier versions cannot disable lazy object fetching.

## PyPI and uv

```bash
python -m pip install "boundver[schema,yaml]"
uvx --from "boundver[schema,yaml]" boundver verify --source head
```

PyPI is the canonical Python distribution. TestPyPI is release staging and is
not intended for production installation.

## GitHub Action

```yaml
- uses: yzm1/boundver@v0.17.0
```

Pin the exact patch release used to write the lockfile. The compatibility alias
such as `v0.17` advances to the newest patch in that line and is intentionally
mutable. An exact tag such as `v0.17.0` never moves.

From v0.15, the same Action can emit a source-bound historical test plan. Use
the exact immutable patch tag, check out full history, and request artifact
upload when another job or a reviewer needs the complete result:

This requires reconciled locks in both the base commit and target commit. A
periodic-lock workflow cannot route an unreconciled pull-request tip with
`review`; reconcile and commit the target lock first, or retain conservative
test routing.

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    fetch-depth: 0
    persist-credentials: false
- id: review
  uses: yzm1/boundver@v0.17.0
  with:
    operation: review
    base: ${{ github.event.pull_request.base.sha }}
    target: HEAD
    merge-base: true
    transitive: true
    upload-artifact: true
    artifact-name: boundver-review-${{ github.run_id }}
- if: steps.review.outputs.selection-complete != 'true'
  run: |
    echo "Boundver routing outputs are incomplete; use the uploaded plan artifact." >&2
    exit 1
```

The Action publishes the bounded Markdown to the GitHub Step Summary. Its
`result-file` is the complete `boundver-plan/v1` JSON; the optional artifact
contains that file and the summary. Bounded name-array outputs are convenient
for same-job routing, but consumers must require `selection-complete: true`.
`transport-complete` describes the result file itself. File annotations are
emitted only for provider documents with an exact target path and only when
the reviewed target commit is the checked-out `HEAD`.

## Container

```bash
docker run --rm \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --volume "$PWD:/repo:ro" \
  --workdir /repo \
  ghcr.io/yzm1/boundver:<version> \
  verify --source head
```

This least-privilege form is exercised in CI. Verification needs no writable
filesystem, network access, or Linux capabilities. Add those privileges only
for a workflow that deliberately needs them.

The container's Git is a reduced-feature build of Debian source for local
Boundver inspection, not a general-purpose Git installation. HTTP(S) clients
and optional Perl/Tcl/Python helpers are disabled, avoiding Git's curl/Kerberos
dependency chain. This does **not** disable every Git transport or make the
image a sandbox; keep `--network none` and the other restrictions above.

Git's full licence, original upstream source archive and signature, public key, build
recipes and integrity/provenance records accompany each image under
`/usr/share/licenses/git`. To inspect them without running the image:

```bash
docker create --name boundver-source ghcr.io/yzm1/boundver:<version>
docker cp boundver-source:/usr/share/licenses/git ./boundver-git-source
docker rm boundver-source
```

The bundle README describes reconstruction. The custom package records the
actual upstream Git 2.50.2 source version, not Debian's old source version or
a renamed package that hides Git from inventory. Its local suffix is not a
patch claim. The upstream tar signature, pinned sizes/hashes and primary/subkey
fingerprints are checked before extraction. Builds also check executable
hardening, SHA1 collision detection and byte equality of two independently
extracted Git-package builds; that check is not whole-image reproducibility.
Three stale Debian Git records have an upstream-fixed assessment tied to each
verified image. The release gate keeps raw and independent fixable-only scans;
see [Security](https://github.com/yzm1/boundver/blob/main/SECURITY.md) for the
binding checks and the separate, expiring base-library exceptions.

Release images target `linux/amd64` and `linux/arm64`, carry OCI source,
version, and revision labels, and receive GitHub artifact attestations. Resolve
and pin the manifest digest for the strongest reproducibility:

```bash
docker buildx imagetools inspect ghcr.io/yzm1/boundver:<version>
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  ghcr.io/yzm1/boundver@sha256:<manifest-digest> --version
```

## Homebrew

```bash
brew install yzm1/boundver/boundver
boundver --version
```

The tap formula installs the release's self-contained `.pyz` with its reviewed
SHA-256 digest. That archive includes the lock-pinned pure-Python PyYAML runtime,
so YAML configs, YAML version sources, and YAML/OpenAPI providers work without a
second package install. Formula updates are generated from the retained release
assets; the tap does not rebuild the package.

## GitLab CI/CD Catalog

After the mirrored component project is enabled in the GitLab Catalog, include
an exact semantic version:

```yaml
include:
  - component: gitlab.com/boundver-project/boundver/boundver@0.17.0
    inputs:
      stage: test
      operation: review
      base: $CI_MERGE_REQUEST_DIFF_BASE_SHA
      target: $CI_COMMIT_SHA
      merge-base: true
      transitive: true
      history-depth: "0"
```

The component runs the matching GHCR image and exposes verification plus the
same explicit base/target, merge-base, facet-policy, and direct/transitive
review controls as the GitHub Action. Every run retains
`boundver-result.json` and `boundver-summary.md` for one week, including failed
jobs. The review JSON is the complete machine plan; the Markdown is the bounded
job-log/artifact presentation. GitLab has no line mapping for canonical
structural pointers, so the summary names exact source files without inventing
line annotations. Do not use `~latest` in a protected pipeline.

## Standalone zipapp

GitHub Releases also contain `boundver-<version>.pyz` and `SHA256SUMS`:

```bash
python3 boundver-<version>.pyz --version
```

The zipapp requires Python 3.10 or newer but no package installation. It bundles
the lock-pinned pure-Python PyYAML runtime and its license; platform-specific
LibYAML extensions are intentionally omitted. JSON and YAML work on every
supported Python version. TOML configuration requires Python 3.11 or newer.
