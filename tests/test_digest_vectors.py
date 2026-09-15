"""Pinned digest vectors, so a platform difference cannot pass unnoticed.

boundver's central promise is that the same declared contract digests the same
everywhere. The suite checks a great deal of behaviour but almost no literal
output: a grep for 64-hex literals across tests finds a synthetic single-entry
vector and sha256("hello"). Nothing pins what a repository digests to, so a
platform-dependent change in ordering, line-ending handling or mode
classification would keep every behavioural test green.

The CI matrix already runs this file on Linux, Windows and macOS across five
interpreters, so recording the expected hex here is enough. It needs no extra
job.

Every entry that a filesystem might refuse is written through the index rather
than through the filesystem: `git update-index --cacheinfo` creates a symlink
and an executable blob on a host that can represent neither. That is what makes
one vector legal on every platform, and it is why the vector is pinned for
source=head, which reads Git objects rather than the working tree.

Covers OBL-GIT-SOURCE-071.
"""

from __future__ import annotations

import subprocess
import unittest

from tests import _git_oracle as oracle
from tests._scenarios import Scenario

#: Recorded on Linux, Windows and macOS. A change here is either a deliberate
#: hashing change, which must be accompanied by a lockfile contract bump, or a
#: platform-dependent bug. Never update these to make a run pass.
EXPECTED_FACETS = {
    "svc": {
        "exact": "bed29325c75fa47a4e2a3a559af7e3bbf36639ddf08eecc47b5e1664cf746a52",
        "behavior": "9f69af50096e77dcdca3c508b832b23c177fe32b15155d6ec9e3efbaba486b0a",
        "boundary": "21641c14bc8473ad2db7d27e50f4394c5746928440e7974ea87cfb0ea1b22396",
        "compat": None,
    },
    "sdk": {
        "exact": "187c6bac121a75bfdf1ff23215500ca33c231464658b91e880a91506e5f2e2b0",
        "behavior": None,
        "boundary": None,
        "compat": None,
    },
}

EXPECTED_SLICE = "f90be4af5c2789b295193b28f43fc228c68e14d8597d094681d7c7c55eb5568e"
EXPECTED_CONFIG_DIGEST = (
    "255f6e603c7aec0dbb8caee833116870f0e90b29889f6e320221ff57c7d6e652"
)


def _write_blob(scene: Scenario, payload: bytes) -> str:
    """Store *payload* as a Git object and return its id."""
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=scene.root,
        input=payload,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("ascii").strip()


def build_corpus(scene: Scenario) -> Scenario:
    """One repository holding every entry kind the digest must handle.

    Deliberately fixed: no timestamps, no tags, no author-dependent content
    reaches a facet digest, so the recorded hex is a property of the declared
    contract rather than of the run.
    """
    scene.component(
        "svc",
        path="svc",
        boundary=["api"],
        behavior=["api", "cfg.json"],
        consumers=["sdk"],
    )
    scene.component("sdk", path="sdk", provider="leaf")
    scene.slice("everything", mode="exact", components=["sdk", "svc"])

    scene.file("svc/api/lf.yaml", "openapi: 3.1.0\nkey: value\n")
    scene.file("svc/api/crlf.yaml", "openapi: 3.1.0\nkey: value\n", crlf=True)
    scene.file("svc/api/lone-cr.yaml", "a\rb\rc\n")
    scene.file("svc/api/empty.yaml", "")
    scene.file("svc/api/.hidden.yaml", "openapi: 3.1.0\n")
    scene.file("svc/api/nested/deep/spec.yaml", "openapi: 3.1.0\n")
    scene.file("svc/api/unicode-é中.yaml", "openapi: 3.1.0\n")
    (scene.root / "svc" / "api" / "binary.bin").write_bytes(
        bytes(range(256)) + bytes([0]) + b"tail"
    )
    scene.json_file("svc/cfg.json", {"retries": 3, "nested": {"a": [1, 2]}})
    scene.file("svc/run.sh", "#!/bin/sh\necho hi\n")
    scene.file("sdk/index.ts", "export const x = 1;\n")
    scene.commit("corpus")

    # Entries the filesystem may not be able to hold, created through the index
    # so the vector is identical on every host.
    scene.git("update-index", "--chmod=+x", "svc/run.sh")
    link = _write_blob(scene, b"api/lf.yaml")
    scene.git("update-index", "--add", "--cacheinfo", f"120000,{link},svc/api/link.yaml")
    # Outside the component, because boundver fails closed on a non-blob entry
    # under a component path. The nested repository commits with fixed dates,
    # so its id is the same on every run.
    scene.submodule("vendor/sub")
    scene.commit_index("index-only entries")
    return scene


class DigestVectorTests(unittest.TestCase):
    """OBL-GIT-SOURCE-071: the recorded hex must hold on every host."""

    def setUp(self):
        self.scene = build_corpus(Scenario("vectors"))
        self.addCleanup(self.scene.close)
        self.lock = self.scene.generate(source="head")

    def test_the_corpus_holds_every_entry_kind(self):
        """A vector over a corpus missing its hard cases proves little."""
        # Through the oracle helper, which reads -z output. Plain ls-files
        # quotes a non-ASCII path, so the unicode entry would go unfound.
        modes = oracle.index_modes(self.scene.root)
        self.assertEqual(modes["svc/run.sh"], "100755")
        self.assertEqual(modes["svc/api/link.yaml"], "120000")
        self.assertEqual(modes["vendor/sub"], "160000")
        self.assertEqual(modes["svc/api/lf.yaml"], "100644")
        self.assertIn("svc/api/unicode-é中.yaml", modes)
        self.assertIn("svc/api/.hidden.yaml", modes)

    def test_the_recorded_facet_digests_hold(self):
        for component, facets in EXPECTED_FACETS.items():
            recorded = self.lock["components"][component]["fingerprints"]
            for facet, expected in facets.items():
                with self.subTest(component=component, facet=facet):
                    self.assertEqual(recorded[facet], expected)

    def test_the_recorded_slice_fingerprint_holds(self):
        self.assertEqual(
            self.lock["slices"]["everything"]["fingerprint"], EXPECTED_SLICE
        )

    def test_the_recorded_config_digest_holds(self):
        self.assertEqual(self.lock["config_digest"], EXPECTED_CONFIG_DIGEST)

    def test_regenerating_gives_the_same_answer(self):
        """Digesting twice in one process must not drift."""
        self.assertEqual(self.scene.generate(source="head"), self.lock)


if __name__ == "__main__":
    unittest.main()
