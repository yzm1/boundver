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
