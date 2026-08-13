# Lockfile merge strategy

`boundary.lock.json` is generated from the repository's source and configuration. When branches change components concurrently, regenerate the complete lockfile from the real merged tree instead of combining fingerprint JSON by hand.

## Why not use a merge driver?

A Git merge driver runs while Git is still constructing the merge. It receives individual conflict stages, not a guaranteed materialization of the final source tree, and it may run on a machine without the expected boundver version. Regenerating there can bless fingerprints from the wrong snapshot.

Keep the lockfile as ordinary text in `.gitattributes`. Perform regeneration only after the source and `boundary.config.json` represent the intended merged result.

## Resolve a lockfile conflict

From the repository root:

```bash
# 1. Resolve source and configuration conflicts first.
git status --short

# 2. Regenerate the whole lock from the materialized merged tree.
boundver validate-config
boundver generate --source working-tree

# 3. Check the exact same snapshot and inspect the generated diff.
boundver verify \
  --source working-tree \
  --facets exact
git diff -- boundary.lock.json

# 4. Mark the generated file resolved, then finish the merge.
git add boundary.config.json boundary.lock.json
git status --short
git commit
```

Only add `boundary.config.json` in step 4 if the merge actually changed it. Add every resolved source file separately as usual.

After the merge commit exists, verify the committed snapshot:

```bash
boundver verify --source head --facets exact
```

The source pairing matters: use `working-tree` before the merge commit, then `head` after it. `head` during conflict resolution still names the pre-merge commit and cannot represent both branches.

## Clean merge but stale lockfile

Git may merge the JSON without a textual conflict even though the combined source requires a different aggregate or slice fingerprint. Run the same regeneration after every merge that touches components, configuration, or the lockfile:

```bash
boundver generate --source working-tree
boundver verify \
  --source working-tree \
  --facets exact
git diff --exit-code -- boundary.lock.json || {
  echo "Review and commit the regenerated boundary.lock.json"
}
```

## Optional post-merge hook

A local post-merge hook runs after Git has materialized the merged tree. It can regenerate and leave any required lockfile update visible for review:

```sh
#!/bin/sh
# .git/hooks/post-merge
set -eu

if command -v boundver >/dev/null 2>&1 && test -f boundary.config.json; then
  boundver generate --source working-tree
  boundver verify \
    --source working-tree \
    --facets exact

  if ! git diff --quiet -- boundary.lock.json; then
    echo "boundver regenerated boundary.lock.json; review and commit it."
  fi
fi
```

Make the hook executable:

```bash
chmod +x .git/hooks/post-merge
```

Hooks are local and are not cloned with the repository. Treat this as a convenience, not enforcement. The authoritative safeguard is a CI job that runs:

```bash
boundver verify --source head
```

## Rules of thumb

- Resolve configuration and source before regenerating the lockfile.
- Regenerate the full lockfile; a partial refresh is inappropriate for a merge.
- Never hand-edit fingerprint values.
- Review affected-consumer and slice changes in the generated diff; use
  `verify --transitive` when the configured graph should drive wider checks.
- Keep CI on `head` so it verifies exactly what the pull request commits.
