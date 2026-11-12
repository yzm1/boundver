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

The v0.17 container still contains Debian libraries
with unpatched HIGH findings. The package/CVE allowlist in `.trivyignore.yaml`
expires on **November 22, 2026**; it is not a claim that the libraries are fixed.
An independent, no-exceptions scan still rejects fixable HIGH/CRITICAL findings.
New findings outside the explicit allowlist block publication.

The Debian feed still reports three Git records against the custom upstream
package: CVE-2026-105437, CVE-2026-111760 and CVE-2026-114466. Upstream Git 2.50.2
contains their fixes. On November 12 the owner authorized a fixed assessment
for these three only, not acceptance of vulnerable Git. They are not added to
the residual-risk allowlist.

`scripts/prepare_git_vex.py` checks the exact offline image, release commit,
architecture, layer digests, pinned source inputs, reviewed build recipe,
installed Git executable hashes and package identity before generating three
temporary OpenVEX `fixed` statements. Offline Trivy matches component PURLs,
not root image PURLs: image binding is enforced by the CI wrapper and its
before/after input fingerprints, not by a self-contained root-bound VEX file.
Never reuse that temporary file for another image. This relies on the trusted
exact-source build and retained-artifact chain; it is not independent
source-to-binary recompilation or an upstream signature of our binaries.

Only the scoped policy scan receives VEX. Raw and independent fixable-only
scans receive neither VEX nor the residual-risk allowlist. A final accounting
check requires every raw finding to remain visible or have exactly the
authorized disposition; unexpected suppression fails closed. Exact raw,
fixable-only, scoped and VEX JSON bytes are retained in bounded, digest-checked
job-log envelopes. Both final release architectures still require fresh scans
and independent review. Existing library exceptions retain their November 22
expiry and operating restrictions.

Acceptance applies only to the built-in CLI running non-root, with no network,
all capabilities dropped and `no-new-privileges`. Custom providers, overridden
entrypoints and additional services are excluded. Keep the read-only root and
repository mounts recommended above for verification. The launcher-specific
review exception in PR #181 does not approve this risk assessment or waive any
security finding, independent review, expiry or scan gate.

The reduced-feature container Git is built from pinned upstream Git 2.50.2
without curl, Expat, gettext, OpenSSL or Perl/Tcl/Python helpers. The archive's
detached signature is verified against a vendored, fingerprint-pinned maintainer
key before extraction. Its local-inspection contract, original sources, complete build
recipes, compiler/package records and licences accompany the image. The build
checks every installed ELF's dependency set, architecture, PIE, non-executable
stack, RELRO and immediate binding; shell-script interpreters are checked too.
It tests SHA1 collision detection and requires byte-identical packages from two
separate source extractions. These checks do not prove general Git feature
parity, whole-image reproducibility or absence of every vulnerability.

The custom `git` package records the actual upstream `Source` version and
adds a `+boundver1` binary suffix. It is not Debian's official Git package, and
the suffix does not assert a patch. Version 2.50.2 is an upstream security
release, not a local relabelling of the older 2.47 source. Source archives,
signature, public key and recipes are
under `/usr/share/licenses/git/source`, with full COPYING outside dpkg's
documentation filter. See the [container guide](https://yzm1.github.io/boundver/distribution/#container).

Earlier owner acceptances of Kerberos findings (four IDs on November 10 and six
on November 11) were conditional on review and both final-platform scans, with
the same November 22 expiry. They do not authorize other CVEs, extend the
expiry, or waive independent review. A dependency-removal candidate must pass
fresh inventory and vulnerability scans before its obsolete exception entries
can be removed. The complete amd64 candidate's November 11 inventory and scans
allow removing curl, Expat and Kerberos package exceptions and narrowing Perl
to `perl-base`. The remaining policy is a subset of the original acceptance:
11 CVEs and 47 package/CVE pairs, all with the unchanged November 22 expiry.
No new findings are accepted by this reduction. Final exact-image checks on
both amd64 and arm64 remain mandatory; the removed libraries are not being
declared patched. Prefer patched, reviewed distribution inputs whenever available.
