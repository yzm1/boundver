"""Two ways to read the same blobs, and what happens when the stream lies.

Hashing a captured tree can read each blob on demand or take them from one
batch stream. The batch is faster and gives up the ability to ask for a
specific object: it must arrive in the order the captured tree implies. So the
stream is checked against that order at every step, and the two paths must
agree on the digest they produce - including for a tree where one object
appears under several paths, which is where a cache sits between them.

Covers OBL-GIT-SOURCE-004 and OBL-GIT-SOURCE-005.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import boundver._hashing as hashing
from boundver._git import _capture_git_source_snapshot
from boundver._hashing import (
    HASH_DOMAIN_EXACT,
    _normalize_hash_content,
    _stream_tree_digest,
)

from tests._scenarios import Scenario

#: Four files. Two share one object, so the duplicate cache between the
#: two branches is exercised. The fourth is written with CRLF endings,
#: because both branches normalize content before framing it and an
#: all-LF tree cannot tell whether either of them actually did.
CONTENT = {
    "svc/a.py": "same\n",
    "svc/b.py": "same\n",
    "svc/c.py": "different\n",
    "svc/d.py": "crlf\r\nlines\r\n",
}


class _Closeable:
    """An iterator that records whether it was closed."""

    def __init__(self, items) -> None:
        self._items = list(items)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._items:
            raise StopIteration
        return self._items.pop(0)

    def close(self) -> None:
        self.closed = True


class _CapturedTree:
    """A committed tree, its snapshot, and the descriptors that hash it."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        for path, text in CONTENT.items():
            scene.file(path, text)
        scene.commit()
        self.scene = scene
        self.captured = _capture_git_source_snapshot(scene.root, "head")
        self.paths = sorted(CONTENT)
        self.descriptors = [(path.encode("utf-8"), path) for path in self.paths]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def oids(self):
        return [self.captured.entries[path].oid for path in self.paths]

    def blob(self, oid: str) -> bytes:
        return subprocess.run(
            ["git", "cat-file", "blob", oid], cwd=self.scene.root,
            capture_output=True,
        ).stdout

    def digest(self, descriptors=None, *, read_blob_fn=None) -> str:
        return _stream_tree_digest(
            self.scene.root,
            self.descriptors if descriptors is None else descriptors,
            "head",
            self.captured,
            domain=HASH_DOMAIN_EXACT,
            read_blob_fn=read_blob_fn,
        )

    def read_blob_fn(self):
        return lambda oid, remaining: self.blob(oid)

    def stream_of(self, pairs):
        """Patch the batch reader to yield exactly *pairs*."""
        iterator = _Closeable(pairs)

        def fake(*args, **kwargs):
            return iterator

        return iterator, patch.object(hashing, "_iter_git_blobs", fake)


class BranchEquivalenceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-005: one digest, whichever way the blobs arrive."""

    def test_two_of_the_files_share_one_object(self):
        """The premise: the duplicate cache is actually exercised."""
        with _CapturedTree() as tree:
            oids = tree.oids()
            self.assertEqual(len(set(oids)), 3, oids)

    def test_normalization_rewrites_at_least_one_captured_blob(self):
        """The other premise: the branches are compared over content that
        normalization actually rewrites.

        Both branches fold CRLF before framing. If every blob in the
        fixture were already LF, normalization would be the identity on
        all of them, and the two branches would agree whether or not
        either one performed it - so the equality below would hold for a
        reason it does not mean.
        """
        with _CapturedTree() as tree:
            raw = [tree.blob(oid) for oid in tree.oids()]
            rewritten = [
                blob for blob in raw
                if _normalize_hash_content(blob) != blob
            ]
            self.assertTrue(rewritten, raw)

    def test_the_two_branches_agree(self):
        with _CapturedTree() as tree:
            self.assertEqual(
                tree.digest(read_blob_fn=tree.read_blob_fn()), tree.digest()
            )

    def test_they_agree_for_a_repeated_path_too(self):
        with _CapturedTree() as tree:
            repeated = tree.descriptors + [tree.descriptors[0]]
            self.assertEqual(
                tree.digest(repeated, read_blob_fn=tree.read_blob_fn()),
                tree.digest(repeated),
            )

    def test_a_repeated_path_changes_the_digest(self):
        """The contrast: the equality is not over a degenerate input."""
        with _CapturedTree() as tree:
            repeated = tree.descriptors + [tree.descriptors[0]]
            self.assertNotEqual(tree.digest(), tree.digest(repeated))


class StreamDisagreementTests(unittest.TestCase):
    """OBL-GIT-SOURCE-004: the stream must match the captured tree exactly."""

    def _pairs(self, tree):
        return [(oid, tree.blob(oid)) for oid in dict.fromkeys(tree.oids())]

    def test_a_faithful_stream_hashes_and_closes(self):
        """The premise: the harness reproduces the real digest."""
        with _CapturedTree() as tree:
            iterator, patched = tree.stream_of(self._pairs(tree))
            with patched:
                self.assertEqual(tree.digest(), tree.digest(read_blob_fn=tree.read_blob_fn()))
            self.assertTrue(iterator.closed)

    def test_a_reordered_stream_is_refused(self):
        with _CapturedTree() as tree:
            pairs = self._pairs(tree)
            iterator, patched = tree.stream_of(list(reversed(pairs)))
            with patched, self.assertRaises(ValueError) as raised:
                tree.digest()
            self.assertEqual(
                str(raised.exception),
                "Git blob stream order disagreed with captured tree",
            )
            self.assertTrue(iterator.closed)

    def test_an_exhausted_stream_names_the_path_it_wanted(self):
        with _CapturedTree() as tree:
            iterator, patched = tree.stream_of([])
            with patched, self.assertRaises(ValueError) as raised:
                tree.digest()
            self.assertEqual(
                str(raised.exception), f"Missing streamed Git blob for {tree.paths[0]}"
            )
            self.assertTrue(iterator.closed)

    def test_a_leftover_blob_is_refused(self):
        with _CapturedTree() as tree:
            pairs = self._pairs(tree)
            iterator, patched = tree.stream_of(pairs + [pairs[0]])
            with patched, self.assertRaises(ValueError) as raised:
                tree.digest()
            self.assertEqual(
                str(raised.exception), "Unexpected extra blob in Git batch stream"
            )
            self.assertTrue(iterator.closed)

    def test_a_non_blob_entry_is_refused_before_any_streaming(self):
        """The check that runs before the stream is opened at all."""
        with _CapturedTree() as tree:
            scene = tree.scene
            scene.git(
                "update-index", "--add", "--cacheinfo",
                f"160000,{'1' * 40},svc/sub",
            )
            scene.git("commit", "-m", "gitlink")
            captured = _capture_git_source_snapshot(scene.root, "head")
            with self.assertRaises(ValueError) as raised:
                _stream_tree_digest(
                    scene.root,
                    [(b"svc/sub", "svc/sub")],
                    "head",
                    captured,
                    domain=HASH_DOMAIN_EXACT,
                )
            self.assertIn("Cannot hash non-blob Git entry", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
