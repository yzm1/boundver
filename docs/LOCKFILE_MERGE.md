# Merge source first, then regenerate the lock

Use an isolated merge checkout, or stop other writers while preparing the
merge. Never combine fingerprint JSON by hand. Do not use a merge driver or an
automatic post-merge writer: neither proves that the selected source is the
reviewed final merge result.

## Resolve a real lock conflict

1. Inspect `git status --short`. Resolve and review source/config conflicts;
   review auto-merged files too. Stage each resolved source/config file by its
   explicit path. Do not stage other sessions' changes.
2. Inspect the generated lock conflict. Choose one branch's generated lock as a
   **temporary placeholder**, not an accepted merged contract. The example below
   chooses HEAD's version. Preserve uncommitted manual notes before this step:
   it overwrites the lock's conflict work file. Both committed branch versions
   remain available in Git.

```sh
git restore --source=HEAD --worktree -- boundary.lock.json
git add -- boundary.lock.json
git diff --name-only --diff-filter=U
```

The last command must print nothing. Even a lock-only unmerged index prevents
coherent index capture. `working-tree` also needs tracked membership from the
index, so switching to it does not bypass this condition.

3. With all intended merged inputs staged, regenerate the **whole** lock from
   that index. Do not use `--components` for merge reconciliation.

```sh
boundver validate-config --source index
boundver generate --source index --out boundary.lock.json
git diff -- boundary.lock.json
git add -- boundary.lock.json
boundver verify --source index
git diff --cached --check
git diff --cached
```

Generation writes the candidate to disk; it does not stage it. Verification
before the second `git add` would still read the placeholder. Inspect component,
consumer and slice changes before accepting the final staged result.

4. Commit only after review and successful index verification, then verify the
   committed merge snapshot:

```sh
git commit
boundver verify --source head
```

HEAD before that commit is still the pre-merge branch. It is useful for checking
the committed state, not for claiming the proposed merge was evaluated.

## A clean textual merge can still need relocking

If Git merged the JSON without a conflict, review/stage the intended source and
config, then use the same full index regeneration and verification sequence.
Textual success does not prove that aggregate or slice fingerprints describe
the combined source.

## Failure and concurrency limits

Before commit admission, unresolved merge stages, index-lock contention,
source/provider failures and expired computation deadlines refuse publication.
Expiry after admission does not roll back a completed atomic replacement.
Tests exercise real Git
source/lock conflicts, an auto-merged second component, a shared slice, manual
placeholder preparation and verification before/after the merge commit.

The existing guarded atomic writer is sufficient; no wrapper is needed. Atomic
publication is not a transaction spanning Git staging, validation, generation
and commit. Do not coordinate concurrent writers by generating into the same
destination. If a failure reports an unconfirmed publication outcome, inspect
the destination before retrying; see [generation deadlines](generation-deadline-design.md).

CI should remain read-only with `boundver verify --source head`. Use
[shared-checkout source guidance](shared-checkouts.md) to choose the intended
view explicitly. Neither a new lock nor a merge commit proves deployment.
