# boundver

<p align="center">
  <a href="https://yzm1.github.io/boundver/">
    <img src="https://yzm1.github.io/boundver/assets/logo.png" alt="Boundver boundary-event logo" width="128">
  </a>
</p>

> A Git-aware lockfile and CI check for contracts shared across components.

[![CI](https://github.com/yzm1/boundver/actions/workflows/ci.yml/badge.svg)](https://github.com/yzm1/boundver/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-6674f8)](https://yzm1.github.io/boundver/)
[![PyPI](https://img.shields.io/pypi/v/boundver)](https://pypi.org/project/boundver/)
[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-boundver-blue?logo=github)](https://github.com/marketplace/actions/boundver)
[![GitLab Catalog](https://img.shields.io/badge/GitLab%20Catalog-boundver-FC6D26?logo=gitlab)](https://gitlab.com/boundver-project/boundver)
[![Python 3.10+](https://img.shields.io/pypi/pyversions/boundver)](https://pypi.org/project/boundver/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](https://github.com/yzm1/boundver/blob/main/LICENSE)

## Did we change something other teams depend on?

Declare what each component publishes and who consumes it. Boundver records
those declarations in a lockfile committed with the code. In CI, it compares
that lockfile with an exact Git snapshot and reports:

- what kind of declared contract changed;
- which direct or transitive consumers may need verification; and
- enough machine-readable output to route the next check.

[![A boundver verification reports boundary drift and affected consumers](https://yzm1.github.io/boundver/assets/verify-demo.svg)](https://yzm1.github.io/boundver/demo/)

Boundver does not replace a compiler, build graph, compatibility checker, or
consumer test. It supplies the repository-level signal that connects them.

## Try it in one minute

Run this in a Git repository with tracked code under `src/`:

```bash
python -m pip install "boundver[schema,yaml]"
boundver init
boundver validate-config
boundver generate --source working-tree
boundver verify --source working-tree --facets exact
```

Review and commit `boundary.config.json` and `boundary.lock.json` together.
Plain `boundver init` creates a placeholder component rooted at `src/`. For a
different layout, use `boundver init --discover` and review its proposal, or
edit the generated component path before validation. Add a real boundary,
version source, and consumers before gating those signals.

For a runnable repository and expected output, use the
[one-minute demo](https://yzm1.github.io/boundver/demo/).

## The gap between existing tools

A compiler protects the code it understands. In a repository with a Python
service, a TypeScript client, generated OpenAPI, and JSON Schema, a shared
contract can cross language and build-system boundaries.

Git reports changed files. A build graph schedules known targets. A
format-specific checker can judge one schema family. Boundver records which
artifacts form each component's contract and who consumes them, so CI can send
the changed contract to the appropriate checks.

## Four signals, one result

Each component can provide up to four facets. More than one facet can change in
the same comparison.

| Facet | What changed | Exit code when gated |
|---|---|---:|
| `exact` | Tracked content, path, or file identity; text CRLF/LF are equivalent | 1 |
| `behavior` | A declared runtime-relevant input | 3 |
| `boundary` | A declared published artifact | 4 |
| `compat` | The configured compatibility family | 5 |

Exit `0` means no unacknowledged gated facet drift remains. With a verification
baseline, acknowledged drift can still be present. Exit `2` means boundver
could not complete the check reliably, such as when configuration, history, or
declared files are missing. When several gated facets drift, the highest
applicable drift code is returned.

A common policy is to gate `boundary` and `compat`, report the other signals,
then run a format-specific compatibility check and the affected consumer suites.
See [exit-code handling](https://yzm1.github.io/boundver/ci-cookbook/#exit-code-aware-automation)
and the [comparison guide](https://yzm1.github.io/boundver/comparison/).

## What it will not tell you

Boundver reports drift in what you declared. It does not decide whether a
change breaks a consumer, run consumer tests, or discover dependencies. Files
left out of a selector are invisible to that facet.

A clean result means no unacknowledged gated drift remains. With a verification
baseline, acknowledged lock drift can still be present. It does not prove
backward compatibility or guarantee that every consumer is safe. Use oasdiff,
Buf, GraphQL Inspector, compilers, and consumer tests for the judgments they are
designed to make.

## A practical configuration

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.15.0/boundary.config.schema.json",
  "project": "payments-platform",
  "defaults": {
    "compat_mode": "major",
    "verify_facets": ["boundary", "compat"]
  },
  "components": {
    "payment-api": {
      "path": "services/payment",
      "version_source": {"file": "package.json", "field": "version"},
      "boundary": {
        "provider": "openapi-canonical",
        "paths": ["openapi/**/*.yaml"]
      },
      "behavior": {
        "paths": ["openapi/**/*.yaml", "config/**/*.json"]
      },
      "consumers": ["checkout-web"],
      "external_consumers": ["external-risk-service"]
    },
    "checkout-web": {
      "path": "apps/checkout",
      "version_source": {"file": "package.json", "field": "version"},
      "boundary": {"provider": "leaf", "paths": []},
      "verify_facets": ["exact"]
    }
  },
  "slices": {
    "checkout-contracts": {
      "description": "Contracts required by checkout",
      "mode": "boundary",
      "components": ["payment-api"]
    }
  }
}
```

This records one API boundary, one direct internal consumer, one external
consumer, and a named slice. Paths are repository-relative for components and
component-relative inside boundary, behavior, and version declarations.

The [configuration reference](https://yzm1.github.io/boundver/reference/)
documents providers, selectors, source modes, facets, and graph limits. The
[glossary](https://yzm1.github.io/boundver/glossary/) defines the terms used
throughout the project.

## Reconcile a branch, then review the range

`review` compares reconciled checkpoints. Both endpoint commits must already
contain locks that match their source trees.

```bash
# Inspect the candidate before accepting its drift.
boundver verify --source working-tree
boundver why payment-api --source working-tree

# Record intentional drift, inspect the lock change, and commit the checkpoint.
boundver verify --source working-tree --update
git diff -- boundary.lock.json
git add boundary.lock.json path/to/changed-file
git commit -m "chore: reconcile boundver lock"
boundver verify --source head

# Now compare the two reconciled commits.
boundver review origin/main..HEAD --merge-base --transitive
```

`review` is read-only and compares two immutable Git trees. `verify` remains
the integrity gate for the current candidate. Repositories that update locks
only periodically can review their reconciled checkpoints, but cannot use an
unreconciled pull-request tip as a review endpoint. Generated boundary artifacts
need their own deterministic freshness check before verification; see
[troubleshooting](https://yzm1.github.io/boundver/troubleshooting/).

## GitHub Actions

Pin the Action to the same release used to write the lock:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    fetch-depth: 0
- uses: yzm1/boundver@v0.15.0
  with:
    operation: verify
    source: head
```

Omitting `facets` applies each component's configured `verify_facets` policy.

The [CI cookbook](https://yzm1.github.io/boundver/ci-cookbook/) covers GitHub,
GitLab, pre-commit, range review, outputs, and shallow-history failures.

## Install and run

- PyPI: `python -m pip install "boundver[schema,yaml]"`
- GitHub Action: [Marketplace](https://github.com/marketplace/actions/boundver)
- GitLab CI/CD: [Catalog project](https://gitlab.com/boundver-project/boundver)
- Container: `docker run --rm ghcr.io/yzm1/boundver:0.15.0 --version`
- Homebrew: `brew install yzm1/boundver/boundver`
- Standalone archive: download `boundver-0.15.0.pyz` from
  [GitHub Releases](https://github.com/yzm1/boundver/releases)

The project supports Python 3.10 or newer and requires Git. Release channels
and least-privilege container use are documented in the
[distribution guide](https://yzm1.github.io/boundver/distribution/).

## Trust and privacy

The built-in boundver CLI is telemetry-free. It does not send source, usage,
analytics, update checks, or crash reports anywhere. Custom Python providers
are explicitly enabled trusted code and are outside that built-in guarantee.

Read the [privacy policy](https://yzm1.github.io/boundver/privacy/), the
[security model](https://yzm1.github.io/boundver/security-model/), and the
[normative specification](https://yzm1.github.io/boundver/specification/).

Using or evaluating boundver? You can identify yourself voluntarily in the
[adopter discussion](https://github.com/yzm1/boundver/discussions/100).

## Project

Boundver is beta software under the MIT license. Issues and pull requests are
welcome. Start with
[CONTRIBUTING.md](https://github.com/yzm1/boundver/blob/main/CONTRIBUTING.md);
maintainers should use
the checked-in [release process](https://github.com/yzm1/boundver/blob/main/docs/RELEASING.md).

The [changelog](https://github.com/yzm1/boundver/blob/main/CHANGELOG.md) records
user-visible changes. The v0.15 line is focused on historical range review,
security hardening, and making the public documentation easier to use.
Semantic-provider implementation remains separately gated work for a later
release.
