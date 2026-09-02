"""Contracts for byte-reproducible release artifacts."""

from __future__ import annotations

import gzip
import importlib.util
import io
import os
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from tests._project_metadata import CURRENT_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_EPOCH = 1_700_000_000


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_wheel(path: Path, timestamp: tuple[int, ...], reverse: bool) -> None:
    entries = [
        (
            "boundver/__init__.py",
            f"__version__ = '{CURRENT_VERSION}'\n".encode(),
        ),
        (f"boundver-{CURRENT_VERSION}.dist-info/RECORD", b""),
    ]
    if reverse:
        entries.reverse()
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in entries:
            info = ZipInfo(name, timestamp)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100755 << 16
            archive.writestr(info, payload)


def _write_sdist(path: Path, timestamp: int, uid: int) -> None:
    payload = (
        f"Metadata-Version: 2.4\nName: boundver\nVersion: {CURRENT_VERSION}\n"
    ).encode()
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename=path.name, mode="wb", fileobj=raw, mtime=timestamp
        ) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                root = tarfile.TarInfo(f"boundver-{CURRENT_VERSION}")
                root.type = tarfile.DIRTYPE
                root.mtime = timestamp
                root.uid = uid
                root.gid = uid
                archive.addfile(root)
                metadata = tarfile.TarInfo(
                    f"boundver-{CURRENT_VERSION}/PKG-INFO"
                )
                metadata.size = len(payload)
                metadata.mtime = timestamp
                metadata.uid = uid
                metadata.gid = uid
                metadata.mode = 0o755
                archive.addfile(metadata, io.BytesIO(payload))


class ArchiveCanonicalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = _load_script("build_release_artifacts.py")

    def test_wheel_canonicalization_removes_order_time_mode_and_compression(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_raw = root / "first.whl"
            second_raw = root / "second.whl"
            first = root / "first-canonical.whl"
            second = root / "second-canonical.whl"
            _write_wheel(first_raw, (2024, 1, 2, 3, 4, 6), False)
            _write_wheel(second_raw, (2026, 7, 8, 9, 10, 12), True)

            self.builder._canonicalize_wheel(first_raw, first, RELEASE_EPOCH)
            self.builder._canonicalize_wheel(second_raw, second, RELEASE_EPOCH)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with ZipFile(first) as archive:
                self.assertTrue(
                    all(item.compress_type == ZIP_STORED for item in archive.infolist())
                )
                self.assertTrue(
                    all(
                        item.date_time == self.builder._zip_timestamp(RELEASE_EPOCH)
                        for item in archive.infolist()
                    )
                )
                self.assertTrue(
                    all(item.external_attr == 0o100644 << 16 for item in archive.infolist())
                )

    def test_sdist_canonicalization_removes_tar_and_gzip_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_raw = root / "first.tar.gz"
            second_raw = root / "second.tar.gz"
            first = root / "first-canonical.tar.gz"
            second = root / "second-canonical.tar.gz"
            _write_sdist(first_raw, 1_710_000_000, 1000)
            _write_sdist(second_raw, 1_720_000_000, 501)

            self.builder._canonicalize_sdist(first_raw, first, RELEASE_EPOCH)
            self.builder._canonicalize_sdist(second_raw, second, RELEASE_EPOCH)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            header = first.read_bytes()[:10]
            self.assertEqual(struct.unpack("<I", header[4:8])[0], RELEASE_EPOCH)
            self.assertEqual(header[3] & 0x08, 0, "gzip header contains a filename")
            with tarfile.open(first) as archive:
                members = archive.getmembers()
                self.assertTrue(all(item.mtime == RELEASE_EPOCH for item in members))
                self.assertTrue(all(item.uid == item.gid == 0 for item in members))
                self.assertTrue(all(not item.uname and not item.gname for item in members))
                file_member = next(item for item in members if item.isfile())
                self.assertEqual(file_member.mode, 0o644)

    def test_two_attempt_gate_does_not_expose_mismatched_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "dist"
            attempt = 0

            def fake_build(root: Path, version: str, epoch: int) -> None:
                nonlocal attempt
                attempt += 1
                canonical = root / "canonical"
                canonical.mkdir(parents=True)
                (canonical / f"boundver-{version}.pyz").write_bytes(
                    f"attempt-{attempt}".encode()
                )

            with mock.patch.object(
                self.builder, "_single_build", side_effect=fake_build
            ):
                with self.assertRaisesRegex(RuntimeError, "not byte-reproducible"):
                    self.builder.build_release_artifacts(output, RELEASE_EPOCH)
            self.assertEqual(list(output.iterdir()), [])


class ReproducibleBuildContractTests(unittest.TestCase):
    def test_standalone_honors_source_date_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "first.pyz"
            second = root / "second.pyz"
            env = os.environ.copy()
            env.update({"SOURCE_DATE_EPOCH": str(RELEASE_EPOCH), "TZ": "UTC"})
            command = [
                sys.executable,
                str(REPO_ROOT / "scripts" / "build_standalone.py"),
                "--output",
            ]
            subprocess.run(
                command + [str(first)],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                command + [str(second)],
                cwd=REPO_ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with ZipFile(first) as archive:
                self.assertTrue(
                    all(item.compress_type == ZIP_STORED for item in archive.infolist())
                )

    def test_build_toolchain_and_double_build_are_explicit(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        smoke = (REPO_ROOT / "scripts" / "packaging_smoke.sh").read_text(
            encoding="utf-8"
        )
        builder = (REPO_ROOT / "scripts" / "build_release_artifacts.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'requires = ["setuptools==84.0.0", "wheel==0.48.0"]',
            pyproject,
        )
        self.assertIn("scripts/install_locked_tools.py release", smoke)
        self.assertIn("--no-index --no-deps --no-build-isolation", smoke)
        self.assertIn('"--no-isolation"', builder)
        self.assertIn("scripts/build_release_artifacts.py --output-dir dist", smoke)
        self.assertNotIn("python scripts/build_standalone.py", smoke)
        self.assertIn('if [[ ! -f "$pyz_path" ]]', smoke)
        self.assertIn("_single_build(first", builder)
        self.assertIn("_single_build(second", builder)
        self.assertIn("first_digests != second_digests", builder)


if __name__ == "__main__":
    unittest.main()
