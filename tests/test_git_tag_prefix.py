"""Literal Git tag-prefix validation and source-resolution contracts."""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from boundver._config import validate_config
from boundver._config_contract import (
    GIT_TAG_PREFIX_PATTERN,
    MAX_GIT_TAG_PREFIX_CHARS,
    git_tag_prefix_error,
)
from boundver._git import git_latest_tag
from boundver._lockfile import generate_lockfile
from boundver._utils import ConfigError
from boundver.versions import extract_version
from tests._repo_fixtures import commit_all, init_git_repo


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _config(prefix: object) -> dict:
    return {
        "project": "tag-prefix-test",
        "components": {
            "svc": {
                "path": "svc",
                "version_source": {"git_tag_prefix": prefix},
                "boundary": {
                    "provider": "implicit",
                    "paths": ["api.json"],
                },
            }
        },
        "slices": {},
    }


def _check_ref_format(prefix: str) -> int:
    """Ask Git itself whether ``prefix`` can name a tag once a version is added."""
    return subprocess.run(
        ["git", "check-ref-format", f"refs/tags/{prefix}0.0.0"],
        check=False,
        capture_output=True,
    ).returncode


def _initialize_component(root: Path) -> None:
    init_git_repo(root)
    component = root / "svc"
    component.mkdir()
    (component / "api.json").write_text('{"version": 1}\n', encoding="utf-8")
    (component / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    commit_all(root, "initial component")


class GitTagPrefixGrammarTests(unittest.TestCase):
    def test_valid_prefixes_match_git_candidate_rules(self) -> None:
        prefixes = (
            "v",
            "service-v",
            "team/service-v",
            "rélease-v",
            "releases/",
            "@",
            "candidate.",
            "candidate.lock",
            "team/beta.lock",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertIsNone(git_tag_prefix_error(prefix))
                result = subprocess.run(
                    ["git", "check-ref-format", f"refs/tags/{prefix}0.0.0"],
                    check=False,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_git_forbidden_prefixes_are_rejected(self) -> None:
        prefixes = (
            " bad",
            "bad ",
            "bad prefix",
            "bad\t",
            "v*",
            "v?",
            "v[",
            "v\\",
            "v~",
            "v^",
            "v:",
            "v..next",
            "v@{next",
            "/v",
            "team//v",
            ".hidden",
            "team/.hidden",
            "team.lock/v",
            "team/beta.lock/v",
            "a/b.lock/",
            "x/y.lock/rel-",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(git_tag_prefix_error(prefix))
                result = subprocess.run(
                    ["git", "check-ref-format", f"refs/tags/{prefix}0.0.0"],
                    check=False,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_type_empty_control_and_length_limits_are_bounded(self) -> None:
        for prefix in (None, True, 1, "", "v\x00", "v\x1f", "v\x7f"):
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(git_tag_prefix_error(prefix))
        self.assertIsNone(git_tag_prefix_error("v" * MAX_GIT_TAG_PREFIX_CHARS))
        error = git_tag_prefix_error("v" * (MAX_GIT_TAG_PREFIX_CHARS + 1))
        self.assertIn(str(MAX_GIT_TAG_PREFIX_CHARS), error or "")

    def test_completed_lock_component_after_the_first_is_rejected(self) -> None:
        r"""A completed '.lock' component is refused wherever it sits in the path.

        Obligation OBL-GIT-SOURCE-032 requires a configured prefix to be able
        to form a real Git tag, and Git refuses any completed ref component
        that ends in '.lock'. Until now the rejected-prefix table exercised
        that rule only through "team.lock/v", where the offending component is
        the first one, and the pattern's ``(?![^/]*\.lock/)`` clause catches
        that case on its own. Mutant MUT-GIT-SOURCE-433 deletes the sibling
        clause ``(?!.*/[^/]*\.lock/)``, which is the only thing that rejects a
        '.lock' component further along the path, and the entire file stayed
        green while prefixes such as "team/beta.lock/v" began passing config
        validation and then failing at tag-generation time. Every prefix here
        is put to Git itself, which is the oracle the obligation appeals to.
        """
        for prefix in ("team/beta.lock/v", "a/b.lock/", "x/y.lock/rel-"):
            with self.subTest(prefix=prefix):
                self.assertIsNotNone(git_tag_prefix_error(prefix))
                self.assertNotEqual(_check_ref_format(prefix), 0)

    def test_lock_component_premise_isolates_the_non_leading_clause(self) -> None:
        r"""PREMISE for MUT-GIT-SOURCE-433: only the '.lock' makes those bad.

        The refusals above would say nothing about the non-leading '.lock'
        clause if the fixtures were being rejected for some unrelated reason,
        such as a dot-prefixed or empty component. This test shows that the
        sole defect in each of them is the '.lock' component. The leading
        component of every fixture is clean, so the leading-component clause
        ``(?![^/]*\.lock/)`` cannot be what fires, and removing the '.lock'
        suffix from the offending component leaves a prefix that both this
        validator and Git accept.
        """
        for prefix, repaired in (
            ("team/beta.lock/v", "team/beta/v"),
            ("a/b.lock/", "a/b/"),
            ("x/y.lock/rel-", "x/y/rel-"),
        ):
            with self.subTest(prefix=prefix):
                leading = prefix.split("/", 1)[0]
                self.assertNotIn(".lock", leading)
                self.assertIsNone(git_tag_prefix_error(repaired))
                self.assertEqual(_check_ref_format(repaired), 0)

    def test_partial_lock_component_is_still_accepted(self) -> None:
        """CONTRAST for MUT-GIT-SOURCE-433: a trailing '.lock' still validates.

        A validator that refused every prefix containing '.lock', or every
        prefix built from more than one path component, would also kill the
        mutant while turning away schemes Git is perfectly happy to tag. The
        prefixes below end in '.lock' without completing a component, because
        the version is appended directly to them, so both this validator and
        Git must keep accepting them.
        """
        for prefix in ("team/beta.lock", "candidate.lock", "team/service-v"):
            with self.subTest(prefix=prefix):
                self.assertIsNone(git_tag_prefix_error(prefix))
                self.assertEqual(_check_ref_format(prefix), 0)

    def test_prefix_length_cap_admits_its_own_boundary_value(self) -> None:
        """The declared character cap is inclusive at exactly the limit.

        Obligation OBL-GIT-SOURCE-032 bounds a literal prefix at
        ``MAX_GIT_TAG_PREFIX_CHARS`` characters, and that number is the longest
        prefix which still validates rather than the first one refused. Only
        the over-limit half of the bound was ever asserted, so mutant
        MUT-GIT-SOURCE-434 could change ``len(value) > MAX_GIT_TAG_PREFIX_CHARS``
        to ``>=``, move the real limit down by one and refuse a legal scheme,
        while every test in this file stayed green. Asserting both sides of the
        boundary means neither tightening the comparison nor loosening it by
        one can survive, and the over-limit assertion keeps the cap itself
        pinned so a validator that simply dropped the check fails too.
        """
        self.assertIsNone(git_tag_prefix_error("v" * (MAX_GIT_TAG_PREFIX_CHARS - 1)))
        self.assertIsNone(git_tag_prefix_error("v" * MAX_GIT_TAG_PREFIX_CHARS))
        over_limit = git_tag_prefix_error("v" * (MAX_GIT_TAG_PREFIX_CHARS + 1))
        self.assertIsNotNone(over_limit)
        self.assertIn(str(MAX_GIT_TAG_PREFIX_CHARS), over_limit or "")

    def test_length_boundary_premise_holds_a_grammatical_prefix(self) -> None:
        """PREMISE for MUT-GIT-SOURCE-434: the boundary value is otherwise legal.

        Accepting a prefix of exactly the maximum length only says something
        about the length comparison if that same prefix would be accepted at
        any shorter length as well. This test shows that the boundary string
        really is ``MAX_GIT_TAG_PREFIX_CHARS`` characters long, that it
        satisfies the shared grammar pattern, and that Git is willing to name
        the resulting tag. The cap is therefore boundver's own policy rather
        than something Git imposes, which is why the position of its boundary
        is the only thing that decides where the policy falls.
        """
        boundary = "v" * MAX_GIT_TAG_PREFIX_CHARS

        self.assertEqual(len(boundary), MAX_GIT_TAG_PREFIX_CHARS)
        self.assertIsNotNone(re.fullmatch(GIT_TAG_PREFIX_PATTERN, boundary))
        self.assertEqual(_check_ref_format(boundary), 0)

    def test_invalid_prefix_never_reaches_tag_resolver(self) -> None:
        resolver = MagicMock(return_value="1.2.3")
        self.assertIsNone(
            extract_version(
                Path("."),
                "svc",
                {"git_tag_prefix": "v*"},
                resolver,
            )
        )
        resolver.assert_not_called()

    def test_direct_tag_lookup_rejects_invalid_prefix_before_git(self) -> None:
        with patch("boundver._git._git_run") as git_run:
            with self.assertRaisesRegex(ValueError, "Invalid literal Git tag prefix"):
                git_latest_tag(Path("."), "bad prefix")
        git_run.assert_not_called()


class GitTagPrefixConfigAndGenerationTests(unittest.TestCase):
    def test_dependency_free_validation_rejects_invalid_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_component(root)
            for prefix in ("bad prefix", "v*", "team//v", ".hidden"):
                with self.subTest(prefix=prefix):
                    with patch(
                        "boundver._config._schema_engine_errors", return_value=[]
                    ):
                        errors = validate_config(_config(prefix), root)
                    prefix_errors = [
                        error for error in errors if "git_tag_prefix" in error
                    ]
                    self.assertTrue(prefix_errors, errors)
                    self.assertTrue(
                        any("literal prefix" in error for error in prefix_errors),
                        prefix_errors,
                    )

    def test_public_schema_matches_dependency_free_validation(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:  # pragma: no cover - optional schema extra
            self.skipTest("jsonschema is not installed")
        schema_path = Path(__file__).parents[1] / "boundary.config.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)

        for prefix, valid in (
            ("team/rélease-v", True),
            ("releases/", True),
            ("team/beta.lock", True),
            ("bad prefix", False),
            ("v*", False),
            ("team//v", False),
            (".hidden", False),
            ("team.lock/v", False),
            # The published schema and the Python validator must agree that a
            # completed '.lock' component is forbidden anywhere in the path,
            # not only in the leading component (MUT-GIT-SOURCE-433).
            ("team/beta.lock/v", False),
        ):
            with self.subTest(prefix=prefix):
                schema_errors = list(validator.iter_errors(_config(prefix)))
                self.assertEqual(not schema_errors, valid, schema_errors)
                self.assertEqual(git_tag_prefix_error(prefix) is None, valid)

    def test_validation_accepts_unicode_and_namespaced_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_component(root)
            for prefix in ("rélease-v", "team/service-v", "releases/"):
                with self.subTest(prefix=prefix):
                    errors = validate_config(_config(prefix), root)
                    self.assertFalse(
                        any("git_tag_prefix" in error for error in errors),
                        errors,
                    )

    def test_generation_rejects_invalid_prefix_before_source_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch("boundver._lockfile._capture_git_source_snapshot") as capture:
                with self.assertRaisesRegex(ConfigError, "literal prefix"):
                    generate_lockfile(
                        _config("v*"),
                        Path(td),
                        source="head",
                    )
            capture.assert_not_called()

    def test_head_snapshot_resolves_unicode_namespaced_tag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _initialize_component(root)
            prefix = "team/rélease-v"
            _git(root, "tag", f"{prefix}1.2.3")

            lock = generate_lockfile(_config(prefix), root, source="head")

            self.assertEqual(lock["components"]["svc"]["version"], "1.2.3")

    def test_valid_prefix_without_reachable_shallow_tag_is_not_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fixture = Path(td)
            source = fixture / "source"
            source.mkdir()
            _initialize_component(source)
            prefix = "svc-v"
            _git(source, "tag", f"{prefix}1.2.3")
            (source / "svc" / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
            commit_all(source, "move beyond tagged commit")

            shallow = fixture / "shallow"
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--no-tags",
                    source.as_uri(),
                    str(shallow),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                _git(shallow, "rev-parse", "--is-shallow-repository").stdout.strip(),
                "true",
            )

            with self.assertRaisesRegex(
                ConfigError,
                "Configured version source did not produce a version",
            ) as raised:
                generate_lockfile(_config(prefix), shallow, source="head")

            self.assertNotIn("Invalid literal Git tag prefix", str(raised.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
