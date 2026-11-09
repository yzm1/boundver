"""Captured source survives worker handoff without consulting mutable Git state."""

import copy
import json

import pytest

from boundver._generation_snapshot import snapshot_from_value, snapshot_to_value
from boundver._git import GitSourceSnapshot, GitTreeEntry


@pytest.fixture
def snapshot():
    entry = GitTreeEntry("odd\tname\n\u03bb.json", "100644", "blob", "a" * 40)
    return GitSourceSnapshot(
        "working-tree", "b" * 40, {entry.path: entry}, head_oid="c" * 40,
        filemode=False, tracked_paths=frozenset([entry.path, "intent-to-add"]),
        skip_worktree_paths=frozenset([entry.path]),
    )


def test_roundtrip_preserves_every_snapshot_field_without_git(snapshot):
    raw = json.dumps(snapshot_to_value(snapshot))
    assert snapshot_from_value(json.loads(raw)) == snapshot


@pytest.mark.parametrize("field,value", [
    ("source", "HEAD"), ("tree_oid", "main"), ("head_oid", "HEAD"),
    ("filemode", 1), ("tracked_paths", []), ("tracked_paths", ["../escape"]),
    ("tracked_paths", ["a", "a"]), ("skip_worktree_paths", ["missing"]),
    ("entries", [["file", "100644", "commit", "a" * 40]]),
    ("entries", [["file", "100644", "blob", "main"]]),
    ("entries", [["/outside", "100644", "blob", "a" * 40]]),
    ("entries", [["file", "100644\t", "blob", "a" * 40]]),
    ("extra", True),
])
def test_invalid_snapshot_is_rejected(snapshot, field, value):
    encoded = snapshot_to_value(snapshot)
    encoded[field] = value
    with pytest.raises(ValueError):
        snapshot_from_value(encoded)


def test_head_snapshot_requires_captured_commit(snapshot):
    value = snapshot_to_value(snapshot)
    value.update(source="head", head_oid=None)
    with pytest.raises(ValueError, match="captured commit"):
        snapshot_from_value(value)


def test_duplicate_tree_entries_are_rejected(snapshot):
    value = snapshot_to_value(snapshot)
    value["entries"].append(copy.deepcopy(value["entries"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        snapshot_from_value(value)


def test_mismatched_in_memory_entry_key_is_not_silently_repaired(snapshot):
    snapshot.entries["wrong"] = next(iter(snapshot.entries.values()))
    with pytest.raises(ValueError, match="key"):
        snapshot_to_value(snapshot)


@pytest.mark.parametrize("mode,kind", [("100755", "blob"), ("120000", "blob"), ("160000", "commit")])
def test_sha256_objects_and_non_regular_entries_are_not_reinterpreted(mode, kind):
    entry = GitTreeEntry("entry", mode, kind, "d" * 64)
    snapshot = GitSourceSnapshot("head", "a" * 64, {"entry": entry}, head_oid="b" * 64)
    assert snapshot_from_value(snapshot_to_value(snapshot)) == snapshot


def test_object_protocol_newline_is_not_part_of_request_identity(snapshot):
    value = snapshot_to_value(snapshot)
    value["tree_oid"] += "\n"
    with pytest.raises(ValueError):
        snapshot_from_value(value)


@pytest.mark.parametrize("limit", ["MAX_GIT_TREE_ENTRIES", "MAX_GIT_PATH_BYTES", "MAX_GIT_TOTAL_PATH_BYTES"])
def test_snapshot_decode_preserves_input_work_budgets(snapshot, monkeypatch, limit):
    import boundver._generation_snapshot as codec
    value = snapshot_to_value(snapshot)
    monkeypatch.setattr(codec, limit, 0)
    with pytest.raises(ValueError):
        snapshot_from_value(value)
