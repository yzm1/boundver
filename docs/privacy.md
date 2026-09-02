# Privacy and telemetry

## Product invariant

The built-in boundver CLI is telemetry-free. It does not collect or transmit
usage or analytics data, phone home, check for updates, or submit crash
reports. It creates no tracking identifier.

Boundary inputs, repository paths, component names, hashes, configuration, and
command results stay on the machine or CI runner where boundver executes. The
CLI writes only artifacts explicitly requested by the user, such as a config,
lockfile, or verification baseline.

Static URLs in generated schemas and lockfiles identify public formats. Their
presence does not cause the CLI to fetch those URLs. Installation tools such as
pip, and hosting platforms such as GitHub, PyPI, GitLab, or a container
registry, may independently record downloads, page views, or workflow activity.
Those platform-side counters are not boundver telemetry.

## Enforced architecture

Repository tests preserve this promise as a reviewed architecture boundary:

- the built-in runtime cannot import outbound-network or telemetry clients;
- runtime and optional-runtime dependencies are explicitly allowlisted; and
- process creation in the built-in runtime is confined to statically
  Git-rooted commands, with an offline subcommand allowlist that rejects
  network-capable Git operations before launch. Git filesystem-monitor hooks,
  external diff and text-conversion helpers, trace sinks, pagers, prompts, and
  partial-clone lazy fetching are disabled for those subprocesses. Boundver
  compares worktree bytes with its own bounded reader, so repository-defined
  clean and process filters are never launched during analysis.

Changing one of those constraints requires changing the invariant test and
this policy in the same reviewed pull request. There is no hidden opt-out
because there is nothing to opt out of.

## Explicitly user-controlled extensions

Custom providers are arbitrary Python code supplied by the user and run only
after the user explicitly enables custom provider loading. Their behavior is
outside the built-in CLI's telemetry guarantee; review custom provider code and
its dependencies before enabling it. CI wrappers and other tools that invoke
boundver likewise retain their own privacy policies.

## Voluntary feedback

If you use or evaluate boundver, you can optionally describe a sanitized use
case in the [adopter discussion](https://github.com/yzm1/boundver/discussions/100).
Do not include secrets, proprietary contract contents, or security-sensitive
details. Security reports belong in the private channel described by the
[security policy](https://github.com/yzm1/boundver/blob/main/SECURITY.md).
