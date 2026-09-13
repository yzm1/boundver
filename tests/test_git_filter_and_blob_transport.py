"""Two places where a repository's own contents reach a subprocess.

A filter driver is executable configuration: a repository that declares one is
asking Git to run a command on every file it reads. boundver answers by
overriding each declared driver to nothing, which only works if the override
set is complete - a partial set leaves one driver live - so every malformed or
oversized answer to that question has to raise rather than return what it
managed to parse.

The blob transport has the same shape one layer down. `cat-file --batch` is a
line protocol over a long-lived process, so a request carrying a newline would
be two requests, and a failed response leaves the stream at an unknown offset.
Both are answered the same way: validate before writing a byte, and abort the
transport after any failure rather than reading on.

Covers OBL-GIT-SOURCE-038 and OBL-GIT-SOURCE-043.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from boundver import _git as git
from boundver._git import (
    MAX_GIT_FILTER_DRIVERS,
    MAX_GIT_FILTER_KEY_BYTES,
    _GitBlobSession,
    _repository_filter_config_overrides,
    _validated_git_object_id,
)
from boundver._utils import GuardrailError

from tests._scenarios import Scenario

#: The four keys one declared driver must produce.
SUFFIXES = ("clean", "smudge", "process", "required")


def _repository() -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", provider="leaf")
    scene.file("svc/main.py", "x\n")
    scene.commit()
    return scene


class _Answer:
    """One crafted reply from the filter-config query."""

    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.result = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=stdout, stderr=stderr
        )

    def __call__(self, *args, **kwargs):
        return self.result


class FilterOverrideCompletenessTests(unittest.TestCase):
    """OBL-GIT-SOURCE-038: four overrides per driver, or nothing at all."""

    def setUp(self):
        _repository_filter_config_overrides.cache_clear()

    def tearDown(self):
        _repository_filter_config_overrides.cache_clear()

    def _overrides(self, scene) -> tuple:
        return _repository_filter_config_overrides(str(scene.root.resolve()))

    def _with_answer(self, scene, stdout: str = "", stderr: str = ""):
        with mock.patch.object(git, "_git_run", _Answer(stdout, stderr)):
            return self._overrides(scene)

    def test_each_declared_driver_produces_exactly_its_four_overrides(self):
        with _repository() as scene:
            scene.git("config", "filter.one.clean", "cat")
            scene.git("config", "filter.two.smudge", "cat")
            overrides = dict(self._overrides(scene))
            for name in ("one", "two"):
                for suffix in SUFFIXES:
                    key = f"filter.{name}.{suffix}"
                    self.assertIn(key, overrides, key)
                    self.assertEqual(
                        overrides[key], "false" if suffix == "required" else ""
                    )
            self.assertEqual(len(overrides), 8)

    def test_a_driver_reached_through_an_include_is_found_too(self):
        """Which is what the query's --includes flag is for."""
        with _repository() as scene:
            (scene.root / "extra.config").write_text(
                "[filter \"included\"]\n\tclean = cat\n", encoding="utf-8"
            )
            scene.git("config", "include.path", "../extra.config")
            overrides = dict(self._overrides(scene))
            self.assertEqual(overrides.get("filter.included.clean"), "")
            self.assertEqual(overrides.get("filter.included.required"), "false")

    def test_a_repository_declaring_nothing_produces_nothing(self):
        """The premise: an empty answer is a real answer, not a refusal."""
        with _repository() as scene:
            self.assertEqual(self._overrides(scene), ())

    def test_a_malformed_answer_raises_rather_than_returning_a_partial_set(self):
        cases = {
            "no trailing separator": "filter.a.clean",
            "bare filter prefix": "filter..clean\0",
            "unknown suffix": "filter.a.wrapper\0",
            "not a filter key": "core.editor\0",
            "empty key": "\0",
            "a control character": "filter.a\x01b.clean\0",
            "an over-long key": "filter." + "a" * MAX_GIT_FILTER_KEY_BYTES + ".clean\0",
        }
        for label, stdout in cases.items():
            with self.subTest(answer=label):
                _repository_filter_config_overrides.cache_clear()
                with _repository() as scene:
                    with self.assertRaises(GuardrailError):
                        self._with_answer(scene, stdout)

    def test_a_diagnostic_on_stderr_is_never_an_answer(self):
        with _repository() as scene:
            with self.assertRaises(GuardrailError) as raised:
                self._with_answer(scene, "filter.a.clean\0", "warning: something")
            self.assertIn("ambiguous diagnostic", str(raised.exception))

    def test_too_many_drivers_refuse_rather_than_truncate(self):
        limit = MAX_GIT_FILTER_DRIVERS
        with _repository() as scene:
            accepted = self._with_answer(
                scene,
                "".join(f"filter.d{index:03d}.clean\0" for index in range(limit)),
            )
            self.assertEqual(len(accepted), 4 * limit)
        _repository_filter_config_overrides.cache_clear()
        with _repository() as scene:
            with self.assertRaises(GuardrailError) as raised:
                self._with_answer(
                    scene,
                    "".join(
                        f"filter.d{index:03d}.clean\0" for index in range(limit + 1)
                    ),
                )
            self.assertIn("driver limit", str(raised.exception))

    def test_a_failed_query_is_not_read_as_an_empty_answer(self):
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(
                128, ["git"], output="", stderr="fatal: bad config"
            )

        with _repository() as scene:
            with mock.patch.object(git, "_git_run", fail):
                with self.assertRaises(GuardrailError) as raised:
                    self._overrides(scene)
            self.assertIn("Cannot safely inspect", str(raised.exception))

    def test_the_no_match_exit_status_is_an_empty_answer(self):
        """Exit 1 with nothing on either stream is how git says 'no keys'."""
        def no_match(*args, **kwargs):
            raise subprocess.CalledProcessError(1, ["git"], output="", stderr="")

        with _repository() as scene:
            with mock.patch.object(git, "_git_run", no_match):
                self.assertEqual(self._overrides(scene), ())

    def test_exit_one_with_output_still_raises(self):
        def noisy(*args, **kwargs):
            raise subprocess.CalledProcessError(
                1, ["git"], output="filter.a.clean\0", stderr=""
            )

        with _repository() as scene:
            with mock.patch.object(git, "_git_run", noisy):
                with self.assertRaises(GuardrailError):
                    self._overrides(scene)


class ObjectIdValidationTests(unittest.TestCase):
    """OBL-GIT-SOURCE-043: only a bare object ID enters the line protocol."""

    VALID = "a" * 40

    def test_a_bare_object_id_is_accepted(self):
        for oid in ("a" * 40, "0" * 64, "AbCdEf" + "0" * 34):
            with self.subTest(oid=oid[:8]):
                self.assertEqual(_validated_git_object_id(oid, "test"), oid)

    def test_a_revision_expression_is_refused(self):
        for value in (
            "HEAD:file",
            ":path",
            "HEAD",
            "refs/heads/main",
            "a" * 41,
            "a" * 39,
            "g" * 40,
            "",
            "a" * 40 + ":path",
        ):
            with self.subTest(value=value[:16]):
                with self.assertRaises(ValueError) as raised:
                    _validated_git_object_id(value, "Git blob request")
                self.assertIn("malformed object ID", str(raised.exception))

    def test_an_interior_newline_is_refused(self):
        """The one that would make a single request into two."""
        for separator in ("\n", "\r", "\r\n"):
            with self.subTest(separator=repr(separator)):
                with self.assertRaises(ValueError):
                    _validated_git_object_id(
                        "a" * 40 + separator + "b" * 40, "Git blob request"
                    )

    def test_surrounding_whitespace_is_refused(self):
        """Whitespace cannot be silently removed from a transport identity."""
        with self.assertRaises(ValueError):
            _validated_git_object_id("  " + self.VALID + "  ", "Git blob request")

    def test_each_padded_spelling_is_refused(self):
        for padded in (" " + self.VALID, self.VALID + "\t", "\n" + self.VALID):
            with self.subTest(value=repr(padded)):
                with self.assertRaises(ValueError):
                    _validated_git_object_id(padded, "Git blob request")


class BlobTransportTests(unittest.TestCase):
    """OBL-GIT-SOURCE-043: nothing is written, and failures reset the stream."""

    def _session(self, scene) -> _GitBlobSession:
        return _GitBlobSession(scene.root)

    def _oid(self, scene, path: str) -> str:
        return scene.git("rev-parse", f"HEAD:{path}").strip()

    def test_a_rejected_request_never_starts_the_process(self):
        with _repository() as scene:
            session = self._session(scene)
            try:
                for value in ("HEAD:svc/main.py", ":svc/main.py", "a" * 41):
                    with self.subTest(value=value[:14]):
                        with self.assertRaises(ValueError):
                            session.read_blob(value)
                        self.assertIsNone(session._proc)
            finally:
                session.close()

    def test_a_valid_request_does_start_one(self):
        """The premise: the assertion above is about the rejection."""
        with _repository() as scene:
            session = self._session(scene)
            try:
                content = session.read_blob(self._oid(scene, "svc/main.py"))
                self.assertEqual(content, b"x\n")
                self.assertIsNotNone(session._proc)
            finally:
                session.close()

    def test_a_negative_limit_is_refused_before_anything_is_written(self):
        with _repository() as scene:
            session = self._session(scene)
            try:
                with self.assertRaises(ValueError):
                    session.read_blob(self._oid(scene, "svc/main.py"), max_bytes=-1)
                self.assertIsNone(session._proc)
            finally:
                session.close()

    def test_a_failed_read_aborts_the_transport(self):
        with _repository() as scene:
            scene.file("svc/big.py", "y" * 200)
            scene.commit("big")
            session = self._session(scene)
            try:
                first = session.read_blob(self._oid(scene, "svc/main.py"))
                self.assertEqual(first, b"x\n")
                started = session._proc
                with self.assertRaises(GuardrailError):
                    session.read_blob(self._oid(scene, "svc/big.py"), max_bytes=8)
                self.assertIsNone(session._proc)
                again = session.read_blob(self._oid(scene, "svc/main.py"))
                self.assertEqual(again, b"x\n")
                self.assertIsNot(session._proc, started)
            finally:
                session.close()

    def test_every_success_returns_the_right_bytes_for_its_own_oid(self):
        """The state machine, walked with failures interleaved."""
        with _repository() as scene:
            scene.file("svc/one.py", "one\n")
            scene.file("svc/two.py", "two" * 40 + "\n")
            scene.commit("more")
            session = self._session(scene)
            try:
                expected = {
                    "svc/main.py": b"x\n",
                    "svc/one.py": b"one\n",
                    "svc/two.py": ("two" * 40 + "\n").encode("ascii"),
                }
                oids = {path: self._oid(scene, path) for path in expected}
                for path, content in expected.items():
                    with self.subTest(step="read", path=path):
                        self.assertEqual(session.read_blob(oids[path]), content)
                    with self.subTest(step="missing", path=path):
                        with self.assertRaises(ValueError):
                            session.read_blob("b" * 40)
                    with self.subTest(step="reread", path=path):
                        self.assertEqual(session.read_blob(oids[path]), content)
            finally:
                session.close()

    def test_a_closed_session_refuses_further_reads(self):
        with _repository() as scene:
            session = self._session(scene)
            oid = self._oid(scene, "svc/main.py")
            session.read_blob(oid)
            session.close()
            with self.assertRaises(ValueError):
                session.read_blob(oid)


if __name__ == "__main__":
    unittest.main()
