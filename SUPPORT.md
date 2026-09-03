# Support

boundver is maintained as an open source project on a best-effort basis.

Before requesting help:

1. Check the [troubleshooting guide](https://yzm1.github.io/boundver/troubleshooting/)
   and [documentation](https://yzm1.github.io/boundver/).
2. Search [existing issues](https://github.com/yzm1/boundver/issues) for the same
   error or use case.
3. Retry with the latest supported release when practical.

For reproducible bugs, open a
[bug report](https://github.com/yzm1/boundver/issues/new/choose) and include a
minimal repository layout, sanitized config, command, output, boundver version,
Python version, operating system, and Git version. For product ideas, use the
feature request template. A blank issue is available for focused usage
questions that do not fit either template.

Using or evaluating boundver in a real repository? The voluntary [adopter
discussion](https://github.com/yzm1/boundver/discussions/100) is the place to
share a sanitized use case, integration path, and wishlist without filing a
bug. The boundver CLI is telemetry-free and never reports usage for you.

Please do not use public issues for vulnerabilities. Follow
[`SECURITY.md`](https://github.com/yzm1/boundver/blob/main/SECURITY.md) to
report them privately. Questions about third-party services or unsupported
versions may need to be handled by those providers or by the wider community.

## Versions and compatibility

The latest release is the supported version. Pin an exact patch release in
automation so an update is reviewed with its lockfile and documentation.

Boundver follows Semantic Versioning, but releases before 1.0 may change config,
lock, or machine-output contracts in a minor release. Breaking migrations are
called out in the changelog and maintained upgrade guide. Patch releases should
not intentionally change a documented contract; a security or integrity fix
may instead make previously accepted unsafe input fail closed.
