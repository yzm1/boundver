# Why boundver?

Use boundver when your components have consumers but **no static verifier** checking their interface — and you need to automatically distinguish internal changes from behavioral contract changes from boundary changes from compatibility breaks.

## The core problem

In a statically-typed, single-language monorepo, the compiler tells you when an API breaks. But most real systems don't have that:

- A Python service exposes an OpenAPI spec — nothing verifies that a PR changed the contract vs. just refactored internals.
- An internal platform publishes config schemas — nothing classifies whether a change is safe for consumers.
- A Go library has importers — without a type-checked boundary, consumers can't know if they need to re-verify.

boundver provides **machine-verifiable change classification** at these boundaries: declare what constitutes your boundary, and every change is automatically categorized as implementation-only, behavioral, boundary-affecting, or breaking.

## Quick decision guide

### You probably **do not** need boundver if…
- Your entire stack is one statically-typed language with compiler-enforced API contracts.
- You already use Bazel/Pants/Nx/Turborepo and their affected-graph + caching covers all four questions (did it change? did behavior change? did the API change? is it compatible?).

### You probably **do** need boundver if…
- Your components have consumers but lack static verification of their interface.
- You want to distinguish "internals changed" from "behavior changed" from "boundary changed" from "compatibility broke" — automatically, not via commit messages.
- You need a portable, source-controlled `boundary-lock/v3` file that CI,
  scripts, and downstream tools can consume.
- You want stable CI cache keys and verification signals without migrating your entire build stack.

## Positioning

- **Compilers / type systems**: Verify contracts statically within one language. boundver operates where no compiler exists for the boundary.
- **Bazel/Pants**: Broad build + dependency graph platforms. High power, higher adoption cost. Know *what* changed but don't classify *how* it changed relative to a declared boundary.
- **Nx/Turborepo**: Task graph + affected/caching ecosystems, primarily JS/TS-centric. Provide "did it change?" but not declared behavioral/API classification.
- **boundver**: Narrow scope — deterministic drift classification at declared
  boundaries for any language. The base install is dependency-free on Python
  3.11+ (`tomli` is used on 3.9–3.10); schema and YAML support are explicit
  extras.

## Current reality
- Exact, behavior, boundary, and compatibility-family drift classification is
  implemented for declared, tracked inputs.
- Canonical OpenAPI/JSON providers parse and deterministically normalize their
  documented inputs, reducing formatting and selected documentation noise.
- Custom provider protocol allows language-specific boundary extraction (e.g., AST-based export analysis).
- Declared `consumers` edges form a validated internal component graph;
  `external_consumers` adds opaque terminal labels. Impact reporting is direct
  by default and has an opt-in transitive closure. Slices can use explicit
  membership or the declared downstream closure of one component.
- The graph is declared, not discovered. boundver does not replace a build
  system's dependency analysis.

## What boundver detects — and what it doesn't

boundver classifies changes by hashing declared files. It is effective when the change touches a file you've declared. It is blind when it doesn't.

### Changes boundver detects well

| Change type | How it's detected |
|---|---|
| Internal refactor / bug fix | `exact` changes, `behavior` + `boundary` stable |
| Behavioral contract change (defaults, config, migrations) | `behavior` changes, `boundary` stable |
| API surface change (new endpoint, removed field) | `boundary` changes |
| Compatibility-family version change | `compat` changes |

### Changes boundver cannot detect

These are **conscious scope boundaries**, not bugs:

| Change type | Why it's invisible |
|---|---|
| **Undeclared dependency behavior change** | The affected files and relationship are absent from the contract. Transitive reporting follows declared `consumers` edges; dependency discovery remains the domain of Bazel/Nx/Pants. |
| **Environment / infrastructure change** | External to the repository (CI variables, cloud config, runtime environment). |
| **Build toolchain change** | Boundver tracks source files, not compiled artifacts. A different compiler version producing different output is invisible. |
| **Stale generated boundary output** | boundver hashes the declared output but does not yet bind it to generator inputs or execute a derivation. Run the generator's deterministic `--check` before verification. |
| **Behavioral change in an undeclared file** | If a file changes behavior but isn't in `behavior.paths`, boundver can't know about it. The user must declare what matters. |
| **Protocol/wire-format semantic change** | If the `.proto` or schema file type is unchanged but the runtime interpretation differs, no file content changes. |

For the last case — where no static file analysis can detect the change — a trusted custom provider can hash a deterministic test-output artifact. Treat such providers as executable code and enable them only through the explicit trusted-code opt-in.

## Adoption pattern

1. Start with one component and a facet policy its inputs can actually supply.
2. Gate PRs with `verify` and use slice fingerprint for cache keying.
3. Expand component coverage incrementally.
4. Add `behavior` paths (config, migrations, contract tests) for richer classification.
5. Declare direct internal and external consumers, then opt into transitive
   impact where CI needs it.
6. Add semantic providers later as they mature.

For a step-by-step walkthrough, see [getting-started.md](getting-started.md).
For a staged adoption strategy with common pitfalls, see [gradual-adoption.md](gradual-adoption.md).
For CI integration patterns and cache-key recipes, see [ci-cookbook.md](ci-cookbook.md).
