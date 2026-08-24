# Examples

The repository contains small, tested configurations for each built-in boundary
style:

| Example | Demonstrates |
|---|---|
| [Consumer impact](https://github.com/yzm1/boundver/tree/main/examples/consumer-impact) | Boundary drift through a transitive consumer graph |
| [Behavior](https://github.com/yzm1/boundver/tree/main/examples/behavior) | Separate behavior and boundary identities |
| [OpenAPI](https://github.com/yzm1/boundver/tree/main/examples/openapi) | Raw OpenAPI as a service boundary |
| [JSON](https://github.com/yzm1/boundver/tree/main/examples/json-file) | Generic JSON contracts |
| [Python package](https://github.com/yzm1/boundver/tree/main/examples/python-package) | Raw hashing of a declared Python export file |
| [TypeScript package](https://github.com/yzm1/boundver/tree/main/examples/typescript-package) | Raw hashing of a TypeScript export barrel |
| [Implicit and leaf](https://github.com/yzm1/boundver/tree/main/examples/implicit-and-leaf) | Gradual adoption and intentional leaves |

Run commands from the repository root because example component paths are
repository-relative. Each example contains an expected lockfile generated from
its checked-in files.

The Python and TypeScript examples hash the declared file's bytes; they do not
analyse an export surface, so formatting changes in the selected file rotate the
boundary digest. See
[public and custom providers](public-vs-custom-providers.md#built-in-providers).
