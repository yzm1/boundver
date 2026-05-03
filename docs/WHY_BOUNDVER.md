# Why boundver?

Use boundver when your components have consumers but **no static verifier** checking their interface — and you need to automatically distinguish internal changes from boundary changes from compatibility breaks.

## The core problem

In a statically-typed, single-language monorepo, the compiler tells you when an API breaks. But most real systems don't have that:

- A Python service exposes an OpenAPI spec — nothing verifies that a PR changed the contract vs. just refactored internals.
- An internal platform publishes config schemas — nothing classifies whether a change is safe for consumers.
- A Go library has importers — without a type-checked boundary, consumers can't know if they need to re-verify.

boundver provides **machine-verifiable change classification** at these boundaries: declare what constitutes your boundary, and every change is automatically categorized as implementation-only, boundary-affecting, or breaking.

## Quick decision guide

### You probably **do not** need boundver if…
- Your entire stack is one statically-typed language with compiler-enforced API contracts.
- You already use Bazel/Pants/Nx/Turborepo and their affected-graph + caching covers all three questions (did it change? did the API change? is it compatible?).

### You probably **do** need boundver if…
- Your components have consumers but lack static verification of their interface.
- You want to distinguish "internals changed" from "boundary changed" from "compatibility broke" — automatically, not via commit messages.
- You need a portable, source-controlled lockfile (`boundary-lock/v1`) that CI, scripts, and downstream tools can consume.
- You want stable CI cache keys and verification signals without migrating your entire build stack.

## Positioning

- **Compilers / type systems**: Verify contracts statically within one language. boundver operates where no compiler exists for the boundary.
- **Bazel/Pants**: Broad build + dependency graph platforms. High power, higher adoption cost. Know *what* changed but don't classify *how* it changed relative to a declared boundary.
- **Nx/Turborepo**: Task graph + affected/caching ecosystems, primarily JS/TS-centric. Provide "did it change?" but not "did the API change?"
- **boundver**: Narrow scope — automated change classification at declared boundaries, for any language, with zero dependencies.

## Current reality
- Change classification (exact/boundary/compat) is fully implemented.
- Semantic providers (openapi-canonical, json-canonical) strip non-contract content before hashing — formatting and comment changes don't trigger false positives.
- Custom provider protocol allows language-specific boundary extraction (e.g., AST-based export analysis).
- Dependency impact graph features are roadmap work; current slices are explicit sets.

## Adoption pattern

1. Start with one slice that maps to one deployable unit.
2. Gate PRs with `verify` and use slice fingerprint for cache keying.
3. Expand component coverage incrementally.
4. Add semantic providers later as they mature.

For a step-by-step walkthrough, see [getting-started.md](getting-started.md).
For a staged adoption strategy with common pitfalls, see [gradual-adoption.md](gradual-adoption.md).
For CI integration patterns and cache-key recipes, see [ci-cookbook.md](ci-cookbook.md).
