# Frequently asked questions

## Does Boundver decide whether an API change is breaking?

No. It reports which declared identity moved and which declared consumers may
need re-testing. Use a format-specific compatibility checker and consumer tests
to decide whether the change is safe.

## Does it replace Nx, Bazel, Buf, or oasdiff?

No. Build graphs schedule work, and format-specific tools understand one
contract language. Boundver supplies a Git-bound change signal and declared
consumer routing across those tools. See the [comparison](comparison.md).

## When should I update the lock?

After you review and accept the source change. Generate and verify against the
same [source mode](reference.md#source-modes), inspect the lock diff, and commit
the source, config, derivation evidence, and lock together.

## What about generated OpenAPI or schema files?

Declare a [derivation](reference.md#generated-artifact-freshness). Run your
trusted generator, record the input/output digests, and let verification reject
a stale receipt. Boundver records freshness evidence; it does not execute the
generator or prove that the generator is correct.

## Can several components share one version?

Yes. One component may inherit another component's compatibility identity with
`version_source.component`, or use a bounded SemVer constant when no tracked
manifest or tag owns the version. Inheritance does not create a consumer edge.

## Can Boundver find declarations I forgot?

`boundver coverage --strict` finds tracked component files omitted from
available boundary or behavior selectors and source-like directories outside
declared component roots. Reasoned exclusions make intentional omissions
visible. It cannot infer whether the architecture itself is complete.

## Why is exit code 2 different from drift?

Exit `2` means Boundver could not produce a reliable comparison because an
input, configuration, digest, or safety condition failed. Do not accept it as a
contract update. Exit codes `1` and `3` through `5` describe classified drift.

## Does the CLI send telemetry?

No. The built-in CLI has no analytics, update check, crash reporting, or
tracking identifier. The [privacy policy](privacy.md) describes the enforced
boundary and the separate behavior of package hosts and user-enabled custom
providers.

## Where should I ask for help?

Use [GitHub Discussions](https://github.com/yzm1/boundver/discussions) for usage
questions, [GitHub Issues](https://github.com/yzm1/boundver/issues) for public
bugs, and the private channel in the
[security policy](https://github.com/yzm1/boundver/blob/main/SECURITY.md) for a
suspected vulnerability.
