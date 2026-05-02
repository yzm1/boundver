# Lockfile Merge Strategy

`boundary.lock.json` is a generated artifact. When parallel branches both regenerate it, Git conflicts are expected.

## Future-proof rule
Do **not** hand-edit lockfile conflict hunks.
Always regenerate from `boundary.config.json`.

## Manual resolution
```bash
# After merge conflict appears
boundver generate --deterministic

git add boundary.lock.json
```

## Optional Git merge driver (recommended)

1. Add to `.gitattributes`:

```gitattributes
boundary.lock.json merge=boundver-lock
```

2. Register merge driver locally:

```bash
git config merge.boundver-lock.name "boundver lockfile regenerate"
git config merge.boundver-lock.driver "scripts/boundver-merge-driver.sh %A"
```

3. Ensure `boundver` is available in your environment.

Now when `boundary.lock.json` conflicts, Git invokes the driver, which regenerates deterministic lock output and writes `%A`.

## CI note
If your CI runs `boundver verify`, merge-driver output is naturally validated during PR checks.
