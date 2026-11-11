# Security Policy

## Supported versions

Security fixes are provided for the latest release.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Earlier versions | No |

## Reporting a vulnerability

Please report suspected vulnerabilities through GitHub's
[private vulnerability reporting](https://github.com/yzm1/boundver/security/advisories/new).
Do not disclose security-sensitive details in a public issue, discussion, or
pull request.

Include, when possible:

- the affected boundver version and environment;
- a minimal reproduction or proof of concept;
- the impact and any known prerequisites;
- suggested mitigations, if you have them.

The maintainer will acknowledge the report as soon as practical, investigate it,
and coordinate disclosure and a fix with the reporter. Please allow a reasonable
amount of time for remediation before publishing details.

## Scope

Reports about boundver's source, packaged CLI, GitHub Action, provider loading
and isolation, maintained provider artifacts, and release artifacts are in
scope. Vulnerabilities in third-party services or community providers should be
reported to their owner unless boundver's integration, trust labels, sandbox,
or curation process is the cause.

## Trust boundaries

Ordinary repository configuration, tracked files, Git object data, file names,
and contract documents are treated as untrusted input. Boundver applies byte,
entry, nesting, work, diagnostic, and wall-clock limits and fails closed when a
complete deterministic result cannot be produced. It suppresses repository Git
hooks, replacement refs, fsmonitor callbacks, external diff/text-conversion
helpers, repository/worktree clean, smudge, and long-running filter commands,
interactive credential prompts, lazy object fetching, and repository-local
executable shadowing. Active Git filter drivers are enumerated through a
bounded config-name query and neutralized in process-local configuration; an
ambiguous or over-budget filter configuration fails closed.
Git 2.32 or newer is required, and boundver verifies that the selected
executable applies the process-local configuration block before repository
inspection. Partial-clone configuration is detected and requires Git 2.45 or
newer, where lazy object fetching can be disabled.
Submodules are treated as opaque Gitlinks: a changed checked-out Gitlink is
visible, but boundver does not inspect a submodule worktree or recurse into its
local configuration. This prevents a nested repository from reintroducing a
filter command during superproject inspection.

`--allow-custom-providers` is intentionally outside that data-only boundary.
A custom provider is arbitrary in-process Python code with the invoking user's
authority; use it only after reviewing and trusting the provider and repository.
Repository configuration cannot enable custom providers by itself. Resource
guardrails are denial-of-service limits, not an operating-system sandbox.

Boundver trusts the installed Python interpreter, operating system, selected
system Git executable, and immutable Git objects supplied by that Git process.
An attacker who already controls those dependencies, the invoking account, or
the host can bypass application-level checks. Working-tree reads detect path,
type, identity, size, and modification races and fail closed, but they are not
a substitute for host isolation against a concurrent privileged attacker.

For untrusted repositories, prefer an immutable release-container digest and
the least-privilege invocation in the
[distribution guide](https://yzm1.github.io/boundver/distribution/): no network,
no capabilities, `no-new-privileges`, a read-only root and repository mount,
and no writable temporary filesystem. Do not opt into custom providers in that
environment.

## Temporary v0.17 container risk acceptance

As of November 11, 2026, the proposed v0.17 image contains Debian libraries
with unpatched HIGH findings. The package/CVE allowlist in `.trivyignore.yaml`
expires on **November 22, 2026**; it is not a claim that the libraries are fixed.
An independent, no-exceptions scan still rejects fixable HIGH/CRITICAL findings.
New findings outside the explicit allowlist block publication.

The maintainer accepted four additional Kerberos IDs on November 10, conditional
on independent review and successful final exact-image scans on both amd64 and
arm64: CVE-2026-112321, CVE-2026-112322, CVE-2026-112323 and CVE-2026-119079.
This adds only 16 package/CVE pairs, against `libgssapi-krb5-2`, `libk5crypto3`,
`libkrb5-3` and `libkrb5support0`. The pinned Debian inputs currently install
version `1.21.3-5+deb13u1`; other packages are not covered by these four entries.
Existing acceptance for the other 17 IDs is not extended or renewed.

On November 11, the maintainer accepted six further IDs under the same review,
scan and runtime conditions: CVE-2026-112315 through CVE-2026-112320. These add
only 24 package/CVE pairs against those same four Kerberos packages. They do
not renew the original 17-ID or November 10 acceptance, change the November 22
expiry, or waive review identity, fixable findings or other publication gates.
The complete allowlist now names 27 IDs and 96 package/CVE pairs; those are
policy limits, not a claim that a fresh scan found exactly that inventory.

Acceptance applies only to the built-in CLI running non-root, with no network,
all capabilities dropped and `no-new-privileges`. Custom providers, overridden
entrypoints and additional services are excluded. Keep the read-only root and
repository mounts recommended above for verification. The launcher-specific
review exception in PR #181 does not approve this risk assessment or waive any
security finding, independent review, expiry or scan gate.

The first three IDs concern the legacy AUTH_GSSAPI server path used by
`kadmind`; the fourth concerns ASN.1 decoding exploited through KDC requests.
See Debian's trackers for [112321](https://security-tracker.debian.org/tracker/CVE-2026-112321),
[112322](https://security-tracker.debian.org/tracker/CVE-2026-112322),
[112323](https://security-tracker.debian.org/tracker/CVE-2026-112323) and
[119079](https://security-tracker.debian.org/tracker/CVE-2026-119079).
The inspected local runtime has no `krb5-kdc`, `krb5-admin-server`, `kadmind`,
`krb5kdc` or `libgssrpc` installed. This is preliminary reachability evidence,
not proof that every remaining library path is unreachable or that the final
release image has been scanned. The built-in CLI performs local Git inspection,
not Kerberos server operations; the vulnerable libraries remain installed.

The later six IDs concern RPC/GSS decoding, kadmin policy processing and KDC
keytab lookup; see Debian's [krb5 tracker](https://security-tracker.debian.org/tracker/source-package/krb5).
The absent server packages reduce exposure in the approved invocation, not the
need for review or fresh scans of the final images.

[MIT's 1.22.3 release](https://web.mit.edu/kerberos/krb5-1.22/krb5-1.22.3.html)
contains upstream fixes. Debian currently reports its packaged version unfixed.
Prefer patched, reviewed distribution inputs when available; do not renew the
exceptions automatically or substitute an unreviewed native-library build.
