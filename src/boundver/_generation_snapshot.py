"""Lossless JSON-ready snapshot handoff; never resolve a moving ref here."""

import os

from ._git import (
    GitSourceSnapshot,
    MAX_GIT_PATH_BYTES,
    MAX_GIT_TOTAL_PATH_BYTES,
    MAX_GIT_TREE_ENTRIES,
    _collect_ls_tree_entries,
    _validated_git_object_id,
)


def _paths(value: object) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > MAX_GIT_TREE_ENTRIES:
        raise ValueError("Invalid snapshot path collection")
    total = 0
    seen = set()
    for path in value:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("Invalid snapshot path")
        if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
            raise ValueError("Snapshot paths must be repository relative")
        raw = os.fsencode(path)
        total += len(raw)
        if len(raw) > MAX_GIT_PATH_BYTES or total > MAX_GIT_TOTAL_PATH_BYTES:
            raise ValueError("Snapshot paths exceed the Git path budget")
        if path in seen:
            raise ValueError("Duplicate snapshot path")
        seen.add(path)
    return frozenset(seen)


def snapshot_to_value(snapshot: GitSourceSnapshot) -> dict:
    value = {
        "source": snapshot.source,
        "tree_oid": snapshot.tree_oid,
        "head_oid": snapshot.head_oid,
        "filemode": snapshot.filemode,
        "entries": [[entry.path, entry.mode, entry.object_type, entry.oid]
                    for _, entry in sorted(snapshot.entries.items())],
        "tracked_paths": sorted(snapshot.tracked_paths),
        "skip_worktree_paths": sorted(snapshot.skip_worktree_paths),
    }
    # Refuse malformed in-memory snapshots instead of repairing their identity
    # while encoding. In particular, dict keys must equal entry paths.
    if any(path != entry.path for path, entry in snapshot.entries.items()):
        raise ValueError("Snapshot entry key does not match its path")
    snapshot_from_value(value)
    return value


def snapshot_from_value(value: object) -> GitSourceSnapshot:
    if not isinstance(value, dict) or set(value) != {
        "source", "tree_oid", "head_oid", "filemode", "entries",
        "tracked_paths", "skip_worktree_paths",
    }:
        raise ValueError("Invalid snapshot fields")
    if value["source"] not in ("head", "index", "working-tree"):
        raise ValueError("Invalid snapshot source")
    if type(value["filemode"]) is not bool:
        raise ValueError("Invalid snapshot filemode")
    for field in ("tree_oid", "head_oid"):
        oid = value[field]
        if field == "head_oid" and oid is None:
            continue
        if not isinstance(oid, str):
            raise ValueError("Invalid snapshot object identity")
        if _validated_git_object_id(oid, "generation snapshot") != oid:
            raise ValueError("Snapshot object identity has a protocol delimiter")
    if value["source"] == "head" and value["head_oid"] is None:
        raise ValueError("HEAD snapshot requires a captured commit")
    rows = value["entries"]
    if not isinstance(rows, list) or len(rows) > MAX_GIT_TREE_ENTRIES:
        raise ValueError("Invalid snapshot entries")

    def records():
        for row in rows:
            if not isinstance(row, list) or len(row) != 4 or not all(isinstance(item, str) for item in row):
                raise ValueError("Invalid snapshot entry")
            path, mode, kind, oid = row
            _paths([path])
            if any("\0" in item or "\t" in item or " " in item for item in (mode, kind, oid)):
                raise ValueError("Invalid snapshot entry header")
            yield f"{mode} {kind} {oid}\t".encode("ascii") + os.fsencode(path)

    entries = _collect_ls_tree_entries(records())
    tracked = _paths(value["tracked_paths"])
    skipped = _paths(value["skip_worktree_paths"])
    if not set(entries) <= tracked or not skipped <= set(entries):
        raise ValueError("Inconsistent snapshot tracking membership")
    return GitSourceSnapshot(
        source=value["source"], tree_oid=value["tree_oid"],
        head_oid=value["head_oid"], filemode=value["filemode"],
        entries=entries, tracked_paths=tracked, skip_worktree_paths=skipped,
    )
