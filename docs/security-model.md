# Security model

Boundver reads repository-controlled data in developer machines and CI, so it
treats configuration, tracked files, paths, Git object data, and contract
documents as untrusted input.

This page explains the practical trust boundary. The
[security policy](https://github.com/yzm1/boundver/blob/main/SECURITY.md)
contains the reporting channel and the complete maintainer-facing model.

## What the built-in CLI can do

The built-in CLI reads bounded repository data, invokes a restricted set of
local Git commands, computes digests, and writes only artifacts requested by
the command. It has no outbound-network or telemetry client in its runtime
dependency surface.

Git subprocesses are constrained to local inspection. Boundver disables hooks,
filters, external diff and text-conversion helpers, filesystem monitors,
pagers, prompts, signature helpers, trace sinks, and partial-clone lazy fetches.
Unsupported or over-budget input fails closed with exit `2` instead of
returning a partial success.

These controls reduce the authority exposed to repository content. They do not
protect against an attacker who already controls the operating system, Python
interpreter, selected Git executable, or invoking account.

## Data-only configuration

`boundary.config.json` declares paths, providers, options, versions, and graph
edges. Ordinary configuration cannot execute a command or enable custom Python
providers by itself. Generated artifacts therefore need a separate,
deterministic freshness check before boundver verifies their output.

Built-in providers parse or hash data under size, count, nesting, and work
limits. A digest proves that the declared identity is unchanged; it is not a
code signature, provenance statement, or compatibility verdict.

## Custom providers are trusted code

A custom provider is arbitrary in-process Python with the same authority as the
boundver process. It runs only when the caller explicitly enables custom
providers. Review the module and all of its dependencies before using
`--allow-custom-providers`, especially in CI or on an untrusted pull request.

The telemetry-free guarantee applies to the built-in CLI, not to custom
providers or wrappers supplied by another project.

## Safer CI use

- Pin boundver and third-party Actions to an immutable release or full commit
  SHA.
- Give the job only the repository and token permissions it needs.
- Keep custom providers disabled for untrusted changes.
- Generate and verify from the same explicit source mode.
- Treat exit `2` as a failed check, never as “no drift.”
- Run generated-artifact freshness checks before boundver.
- For an untrusted repository, prefer the release container with no network,
  no capabilities, `no-new-privileges`, and read-only mounts.

The [CI cookbook](ci-cookbook.md) provides maintained examples, and the
[distribution guide](distribution.md) shows a least-privilege container
invocation.

## Privacy

The built-in boundver CLI is telemetry-free and does not phone home. Package
registries and hosting platforms can independently record downloads or page
views. See [Privacy and telemetry](privacy.md) for the enforced invariant.

## Report a vulnerability

Use GitHub's
[private vulnerability reporting](https://github.com/yzm1/boundver/security/advisories/new).
Do not put sensitive details in a public issue, discussion, or pull request.
