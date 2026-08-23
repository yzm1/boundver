# Installation and distribution

Every channel originates from the same reviewed release commit. Prefer an exact
version or immutable digest in CI.

## PyPI and uv

```bash
python -m pip install "boundver[schema,yaml]"
uvx --from "boundver[schema,yaml]" boundver verify --source head
```

PyPI is the canonical Python distribution. TestPyPI is release staging and is
not intended for production installation.

## GitHub Action

```yaml
- uses: yzm1/boundver@v0.13.0
```

Pin the exact patch release used to write the lockfile. The compatibility alias
is convenient for controlled updates but is intentionally mutable.

## Container

```bash
docker run --rm \
  --volume "$PWD:/repo:ro" \
  --workdir /repo \
  ghcr.io/yzm1/boundver:<version> \
  verify --source head
```

Release images target `linux/amd64` and `linux/arm64`, carry OCI source,
version, and revision labels, and receive GitHub artifact attestations. Resolve
and pin the manifest digest for the strongest reproducibility:

```bash
docker buildx imagetools inspect ghcr.io/yzm1/boundver:<version>
docker run --rm ghcr.io/yzm1/boundver@sha256:<manifest-digest> --version
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
  - component: gitlab.com/yzm1/boundver/boundver@<version>
    inputs:
      stage: test
      source: head
      transitive: true
```

The component runs the matching GHCR image and exposes the same source, facet,
component, changed-ref, transitive, and fail-fast controls as the GitHub Action.
Do not use `~latest` in a protected pipeline.

## Standalone zipapp

GitHub Releases also contain `boundver-<version>.pyz` and `SHA256SUMS`:

```bash
python3 boundver-<version>.pyz --version
```

The zipapp requires Python 3.9 or newer but no package installation. It bundles
the lock-pinned pure-Python PyYAML runtime and its license; platform-specific
LibYAML extensions are intentionally omitted. JSON and YAML work on every
supported Python version. TOML configuration requires Python 3.11 or newer.
