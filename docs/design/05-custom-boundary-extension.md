# 05 — Custom Boundary Extension Design

## Goal
Make custom boundaries first-class so users can extend boundver without modifying core logic.

## Problem statement
Users need a clear way to hash API boundaries that are not built-in provider types.

## Design
1. **Provider interface contract**
   - Input: component root, boundary config, source mode.
   - Output: `digest`, `status`, `errors`, optional metadata.
2. **Config-driven provider registration**
   - Add `boundary.provider` and `boundary.options` in config.
   - Support built-ins and local plugin providers.
3. **Execution model**
   - Provider resolves concrete files/content.
   - Core canonicalizes resolved payload and hashes deterministically.
4. **Safety constraints**
   - Providers are pure/read-only.
   - Fail closed (explicit `error`) if provider cannot resolve artifacts.

## Example config sketch
```json
{
  "components": {
    "billing": {
      "path": "services/billing",
      "boundary": {
        "kind": "custom",
        "provider": "jsonpath-extract",
        "options": {
          "file": "contract.json",
          "select": ["$.paths", "$.components.schemas"]
        }
      }
    }
  }
}
```

## Deliverables
- Provider API doc.
- Two reference custom providers.
- End-to-end examples showing custom boundary setup.
