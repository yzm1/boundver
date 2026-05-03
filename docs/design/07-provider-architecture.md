# 07 — Provider Architecture Design

## Problem

Today `boundary.provider` is a validation label only. Every provider (`openapi`,
`python-exports`, `typescript-exports`, `json-file`, `leaf`, `implicit`) runs the
same code path: list `boundary.paths`, read raw bytes, hash. No provider can
normalize its content, explain a diff, or carry options beyond a path list.

Consequences:
- A comment added to an OpenAPI file changes the boundary digest even when the
  API contract is identical.
- Custom provider logic lives outside boundver and must own its own hashing,
  removing the guarantee that fingerprints are computed consistently.
- `explain_diff` can only say "bytes changed", not "endpoint `/billing` removed".

---

## Goals

1. Each provider controls **what content** is hashed, not how it is hashed.
2. Core always owns the **canonical digest step** — same algorithm, same encoding.
3. Providers are **pure/read-only** — no side effects.
4. Built-in providers are thin wrappers; their current behavior is preserved
   exactly until a semantic provider replaces them.
5. Custom providers plug in without modifying core.

---

## Core types

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, runtime_checkable


# ------------------------------------------------------------------
# Passed to every provider method
# ------------------------------------------------------------------
@dataclass
class ProviderContext:
    repo_root: Path
    component_path: str            # repo-relative, e.g. "services/billing"
    boundary_cfg: dict             # full boundary config dict
    source: str                    # "head" | "index" | "working-tree"
    # Injected by core so providers never call git directly
    read_file: Callable[[str], bytes]   # repo_rel_path → bytes
    list_files: Callable[[str], List[str]]  # repo_rel_prefix → [repo_rel_path, ...]


# ------------------------------------------------------------------
# What a provider returns from resolve()
# ------------------------------------------------------------------
@dataclass
class ResolvedBoundary:
    # Ordered list of (label, content) pairs.
    # Core hashes them as: sha256(concat("entry:<label>\n", content, ...))
    # Labels must be deterministic; providers must sort them.
    entries: List[tuple[str, bytes]] = field(default_factory=list)

    status: str = "ok"            # "ok" | "partial" | "error"
    errors: List[str] = field(default_factory=list)

    # Optional structured metadata stored in the lockfile alongside the digest.
    # Passed back to explain_diff() when available.
    metadata: Optional[dict] = None
```

**Design note:** `entries` replaces the implicit `file:{path}\n{content}` format
in the current code. The label is still `file:{rel_path}` for path-based providers,
so all existing lockfile digests are preserved when built-ins are migrated.

---

## Provider protocol

```python
@runtime_checkable
class BoundaryProvider(Protocol):
    # Stable identifier. Must match boundary.provider values in config.
    name: str

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        """Return the normalized content to be hashed.

        Providers MUST:
        - Return entries in deterministic (sorted) order.
        - Not call subprocesses beyond what ctx.read_file / ctx.list_files provide.
        - Not mutate any file on disk.
        - Produce status="error" rather than raising for expected failure modes.
        """
        ...

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        """Return validation error strings. Empty list = valid."""
        ...

    def explain_diff(
        self,
        old_metadata: Optional[dict],
        new_metadata: Optional[dict],
        ctx: ProviderContext,
    ) -> str:
        """One-line human summary of what changed between two digests.

        Receives the metadata blobs stored at generate-time.
        If metadata is None (old lockfiles), return a generic message.
        """
        ...
```

---

## How core uses a provider

```python
def compute_boundary(
    provider: BoundaryProvider,
    ctx: ProviderContext,
) -> tuple[Optional[str], str, List[str]]:
    """Returns (digest | None, status, errors)."""
    resolved = provider.resolve(ctx)
    if resolved.status == "error" or not resolved.entries:
        return None, resolved.status, resolved.errors
    # Core owns hashing — providers never touch hashlib directly
    h = hashlib.sha256()
    for label, content in resolved.entries:
        h.update(f"entry:{label}\n".encode("utf-8"))
        h.update(content)
    return h.hexdigest(), resolved.status, resolved.errors
```

This is the **only** place SHA-256 is called for boundary digests.

---

## Provider registry

```python
_REGISTRY: Dict[str, BoundaryProvider] = {}

def register_provider(p: BoundaryProvider) -> None:
    _REGISTRY[p.name] = p

def get_provider(name: str) -> Optional[BoundaryProvider]:
    return _REGISTRY.get(name)
```

Built-in providers are registered at module import time. Custom providers are
registered by loading a Python module path specified in the config (see §Custom
providers below).

---

## Built-in providers under the new model

Each built-in is a thin class that replicates current raw-bytes behavior. No
existing lockfile digests change.

### `PathHashProvider` (base for `openapi`, `json-file`, `python-exports`, etc.)

```python
class PathHashProvider:
    """Hash declared boundary paths as raw normalized bytes — current behavior."""
    name: str  # set per subclass

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for explicit boundary provider"],
            )
        entries = []
        for rel in sorted(paths):
            full_base = f"{ctx.component_path}/{rel}".lstrip("/")
            for repo_rel in sorted(ctx.list_files(full_base)):
                content = ctx.read_file(repo_rel)
                # CRLF normalisation preserved from current _read_path_content
                if b"\r\n" in content and b"\x00" not in content:
                    content = content.replace(b"\r\n", b"\n")
                child_rel = repo_rel[len(ctx.component_path) + 1:]
                entries.append((f"file:{child_rel}", content))
        if not entries:
            return ResolvedBoundary(
                status="error",
                errors=["Declared boundary paths produced no digest"],
            )
        return ResolvedBoundary(entries=entries)

    def validate_config(self, boundary_cfg, component_path, repo_root):
        errors = []
        paths = boundary_cfg.get("paths", [])
        for rel in paths:
            full = repo_root / component_path / rel
            if not full.exists():
                errors.append(
                    f"Boundary path not found: {component_path}/{rel}"
                    " — ensure the file exists before running generate"
                )
        return errors

    def explain_diff(self, old_meta, new_meta, ctx):
        return "declared boundary artifact changed"
```

`ImplicitProvider` and `LeafProvider` mirror their current inline logic but as
proper classes with `status="partial"` and `status="ok"` respectively.

---

## Semantic providers (future built-ins)

These live in `boundver/providers/` as opt-in imports (no mandatory dependencies).

| Provider name | What `resolve()` does |
|---|---|
| `openapi-canonical` | Parse YAML/JSON, emit sorted paths+schemas as canonical JSON, strip `description`/`x-*` fields |
| `json-canonical` | Parse JSON, emit RFC 8785 canonical form |
| `python-exports` (semantic) | Parse `__all__` and public symbol signatures via `ast`, emit sorted symbol list |
| `typescript-exports` (semantic) | Parse `.d.ts` public surface, emit sorted declaration list |

These providers produce digests that are **stable across formatting changes** but
change when the logical API changes. They are additive — existing raw providers
remain available.

---

## Custom providers

A custom provider is a Python class in a file the user owns:

```python
# my_team/boundver_providers.py
from boundver.providers import BoundaryProvider, ProviderContext, ResolvedBoundary

class ServiceDefinitionProvider:
    name = "custom.acme.service-definition.v1"

    def resolve(self, ctx):
        contract_file = ctx.boundary_cfg.get("options", {}).get("file", "contract.json")
        content = ctx.read_file(f"{ctx.component_path}/{contract_file}")
        normalized = _extract_stable_contract(content)  # team-owned logic
        return ResolvedBoundary(entries=[("contract", normalized)])

    def validate_config(self, boundary_cfg, component_path, repo_root):
        return []

    def explain_diff(self, old_meta, new_meta, ctx):
        return "service contract changed"
```

**Config registration:**

```json
{
  "providers": [
    {"module": "my_team.boundver_providers", "class": "ServiceDefinitionProvider"}
  ],
  "components": {
    "billing": {
      "boundary": {
        "provider": "custom.acme.service-definition.v1",
        "options": {"file": "contract.json"}
      }
    }
  }
}
```

Core loads `providers` entries at startup, before processing components.
The `custom.*` namespace is required for all user-defined providers; core
refuses to load a class whose `name` does not start with `custom.`.

---

## Security constraints

- Custom provider modules are only loaded when `--allow-custom-providers` is
  passed on the CLI (or `BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1`). Without it, core
  errors out if config references a `custom.*` provider with a `module` key.
- Providers must not call `subprocess`, `os.system`, or write files. Core
  enforces this in test mode via a restricted `ProviderContext` that replaces
  `read_file`/`list_files` with sandboxed implementations.
- Path arguments passed to `ctx.list_files` and `ctx.read_file` are validated
  by core against the component root before the provider is invoked.

---

## Migration plan

1. **Phase 1 — Protocol in place, built-ins as thin wrappers** (no behavior change)
   - Introduce `ProviderContext`, `ResolvedBoundary`, `BoundaryProvider` in
     `boundver/providers.py`.
   - Port `PathHashProvider`, `ImplicitProvider`, `LeafProvider` as classes.
   - Replace inline hashing in `generate_lockfile()` with `compute_boundary()`.
   - All 176 existing tests must pass unchanged (lockfile digests stable).

2. **Phase 2 — `options` field + custom provider loading**
   - Add `boundary.options` to config schema.
   - Add `providers` top-level config key + loading logic.
   - Add `--allow-custom-providers` flag.

3. **Phase 3 — Semantic built-ins (opt-in)**
   - Ship `openapi-canonical` and `json-canonical` in `boundver/providers/`.
   - Gated behind explicit provider name; no existing lockfile affected.

---

## What changes in lockfile output

Phase 1: **nothing** — digests identical, `boundary_provider` field unchanged.

Phase 2+: lockfile gains optional `boundary_metadata` per component when the
provider returns non-null metadata. `verify` ignores it (does not hash it).
`explain` uses it for richer diffs.

---

## Files to create/modify

| File | Action |
|---|---|
| `src/boundver/providers.py` | New: `ProviderContext`, `ResolvedBoundary`, `BoundaryProvider` protocol, built-in classes, registry |
| `src/boundver/core.py` | Modify: replace inline boundary hashing with `compute_boundary()` call |
| `boundary.config.schema.json` | Add: `providers` array, `boundary.options` object |
| `tests/test_providers.py` | New: unit tests per provider class |
