# Why boundver?

Use boundver when you want **deterministic component and boundary fingerprints** in a monorepo without adopting a full build system.

## Quick decision guide

### You probably **do not** need boundver if…
- You already use Bazel/Pants/Nx/Turborepo for end-to-end affected graph, caching, and orchestration.
- Your existing build system already provides the dependency-impact and cache-key granularity you need.

### You probably **do** need boundver if…
- You have a multi-component repo and need a lightweight, declarative contract for:
  - implementation identity (`exact`),
  - declared boundary identity (`boundary`),
  - compatibility family identity (`compat`).
- You want stable CI cache keys and verification signals without migrating your entire build stack.
- You need a portable, source-controlled lockfile contract (`boundary-lock/v1`) that can be consumed by scripts/tools.

## Positioning vs larger build tools
- **Bazel/Pants**: broad build + dependency graph platforms. High power, higher adoption cost.
- **Nx/Turborepo**: task graph + affected/caching ecosystems, primarily JS/TS-centric workflows.
- **boundver**: narrow scope; deterministic fingerprints/spec-first contract for component boundaries.

boundver is best when you want a small primitive that can plug into existing CI/CD, not a full workflow replacement.

## Current reality
- Boundver today is strongest as a deterministic fingerprint/lockfile tool.
- Semantic boundary understanding (e.g., canonical OpenAPI/TS/Python contract diffs) is on the roadmap.
- Dependency impact graph features are roadmap work; current slices are explicit sets.

## Adoption pattern
1. Start with one slice that maps to one deployable unit.
2. Gate PRs with `verify` and use slice fingerprint for cache keying.
3. Expand component coverage incrementally.
4. Add semantic providers later as they mature.
