---
hide:
  - navigation
  - toc
---

<section class="bv-hero" markdown>

<div class="bv-eyebrow">Git-aware contract lockfile</div>

# Did we change something other teams depend on?

Boundver records the contracts shared across components, checks them against an
exact Git snapshot, and names the consumers that may need to re-test.

<div class="bv-actions" markdown>

[Run the one-minute demo](demo.md){ .md-button .md-button--primary }
[Read how it works](executive-summary.md){ .md-button }

</div>

![A boundver verification reports boundary drift and affected consumers](assets/verify-demo.svg)

</section>

## One lockfile. Four useful signals.

Declare each component, the artifacts it publishes, and its downstream
consumers. Commit the generated `boundary.lock.json`. Later checks tell you
which identities moved.

<div class="bv-facet-grid" markdown>

<article markdown>

### `exact`

Any tracked component byte or file identity changed.

</article>

<article markdown>

### `behavior`

A declared runtime-relevant input changed.

</article>

<article markdown>

### `boundary`

A declared published artifact changed.

</article>

<article markdown>

### `compat`

The configured compatibility family changed.

</article>

</div>

Several facets can change at once. You choose which ones fail CI. Boundary and
compatibility drift can also identify direct or transitive consumers.

## From Git change to the right check

<ol class="bv-steps">
  <li><strong>Declare</strong><span>components, published artifacts, and consumer edges</span></li>
  <li><strong>Compare</strong><span>the committed lock with HEAD, the index, a working tree, or a branch range</span></li>
  <li><strong>Route</strong><span>format-specific checks and consumer suites using bounded text or JSON output</span></li>
</ol>

```bash
python -m pip install "boundver[schema,yaml]"
boundver init
boundver generate --source working-tree
boundver verify --source working-tree
```

## Use it with what you already trust

Boundver does not replace compilers, build graphs, compatibility tools, or
consumer tests. It adds a repository-level contract signal between them.

```text
Git snapshot -> contract drift -> compatibility check -> affected consumer tests
```

For example, an OpenAPI boundary change can trigger oasdiff and the suites
reachable from that API, while an internal refactor remains visible without
being mislabeled as a public-contract change.

<div class="bv-route-grid" markdown>

-   **First evaluation**

    Follow [Getting started](getting-started.md) or run the
    [reproducible demo](demo.md).

-   **Existing monorepo**

    Adopt one component at a time with [Gradual adoption](gradual-adoption.md).

-   **Pull-request routing**

    Use [Historical range review](reference.md#historical-range-review) and the
    [CI cookbook](ci-cookbook.md).

-   **Need exact semantics**

    Read the [reference](reference.md), [glossary](glossary.md), and
    [normative specification](specification.md).

</div>

## Know the boundary of the answer

Boundver reports drift in what you declared. It does not decide whether a
change is backward compatible, run consumer tests, or discover dependencies.
Files omitted from a selector remain outside that identity.

A clean result means the chosen snapshot agrees with the committed record. It
does not prove that every consumer is safe. Start with
[What boundver does and does not do](WHY_BOUNDVER.md), or compare it with
[build graphs and schema-specific tools](comparison.md).

## Local by design

The built-in boundver CLI is telemetry-free. It analyzes local Git state and
does not collect or transmit source, usage, analytics, update checks, or crash
reports. Read the [privacy policy](privacy.md) and [security model](security-model.md).

Using or evaluating boundver? You can identify yourself voluntarily in the
[adopter discussion](https://github.com/yzm1/boundver/discussions/100).
