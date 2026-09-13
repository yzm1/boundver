"""Operation-wide path-glob compilation and matching guardrails."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boundver._utils as utils
from boundver._config import _expand_component_paths, validate_config
from boundver._output import analyze_explain_changes
from boundver._utils import (
    GuardrailError,
    _compile_path_glob,
    _match_path_glob,
    _PathGlobOperation,
)
from boundver.providers import PathHashProvider, ProviderContext
from tests._repo_fixtures import commit_all, init_git_repo


def _provider_context(pattern: str, *, matching: bool) -> ProviderContext:
    files = {
        f"svc/file-{index}.txt": b"contract\n"
        for index in range(20)
    }
    selected_pattern = pattern if matching else "missing-*.txt"

    def read_file(path: str) -> bytes:
        return files[path]

    def list_files(prefix: str) -> list[str]:
        prefix = prefix.rstrip("/")
        return sorted(
            path
            for path in files
            if path == prefix or path.startswith(prefix + "/")
        )

    return ProviderContext(
        repo_root=Path("/repo"),
        component_path="svc",
        boundary_cfg={"paths": [selected_pattern]},
        source="working-tree",
        read_file=read_file,
        list_files=list_files,
    )


class CompiledPathGlobTests(unittest.TestCase):
    def test_operation_compiles_each_normalized_pattern_once(self) -> None:
        operation = _PathGlobOperation("test", max_steps=100_000)
        expected = []
        actual = []
        for index in range(100):
            candidate = f"api/route-{index}.yaml"
            expected.append(_match_path_glob(candidate, "api/*.yaml"))
            actual.append(operation.matches(candidate, "api/*.yaml"))

        self.assertEqual(actual, expected)
        self.assertEqual(operation.compiled_patterns, 1)

    def test_representative_valid_cross_product_fits_default_budget(self) -> None:
        patterns = [f"group-{index:02d}/*.json" for index in range(32)]
        candidates = [
            f"group-{index % 32:02d}/contract-{index}.json"
            for index in range(5_000)
        ]
        operation = _PathGlobOperation("representative corpus")
        matched = 0

        for pattern in patterns:
            operation.prepare(pattern)
            for candidate in candidates:
                matched += operation.matches(candidate, pattern)

        self.assertEqual(matched, len(candidates))
        self.assertEqual(operation.compiled_patterns, len(patterns))
        self.assertLess(operation.steps, utils.MAX_GLOB_OPERATION_STEPS // 4)

    def test_matching_and_nonmatching_provider_cross_products_fail_closed(self) -> None:
        for matching in (True, False):
            with self.subTest(matching=matching), patch.object(
                utils,
                "MAX_GLOB_OPERATION_STEPS",
                50,
            ):
                resolved = PathHashProvider().resolve(
                    _provider_context("file-*.txt", matching=matching)
                )

            self.assertEqual(resolved.status, "error")
            self.assertEqual(len(resolved.errors), 1)
            self.assertIn("aggregate glob compile/match steps", resolved.errors[0])
            self.assertIn("reduce wildcard declarations", resolved.errors[0])

    def test_literal_provider_path_does_not_consume_glob_budget(self) -> None:
        files = {"svc/file.txt": b"contract\n"}
        context = ProviderContext(
            repo_root=Path("/repo"),
            component_path="svc",
            boundary_cfg={"paths": ["file.txt"]},
            source="working-tree",
            read_file=files.__getitem__,
            list_files=lambda prefix: [
                path
                for path in files
                if path == prefix or path.startswith(prefix.rstrip("/") + "/")
            ],
        )

        with patch.object(utils, "MAX_GLOB_OPERATION_STEPS", 0):
            resolved = PathHashProvider().resolve(context)

        self.assertEqual(resolved.status, "ok", resolved.errors)
        self.assertEqual(resolved.entries, [("file:file.txt", b"contract\n")])


class PrimaryGlobOperationTests(unittest.TestCase):
    def _repo_with_files(self, root: Path, count: int = 20) -> None:
        init_git_repo(root)
        component = root / "svc"
        component.mkdir()
        for index in range(count):
            (component / f"file-{index}.txt").write_text(
                "contract\n",
                encoding="utf-8",
            )
        commit_all(root)

    def test_config_expansion_budget_covers_every_source_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo_with_files(root)

            for source in ("head", "index", "working-tree"):
                with self.subTest(source=source), patch.object(
                    utils,
                    "MAX_GLOB_OPERATION_STEPS",
                    50,
                ):
                    with self.assertRaisesRegex(
                        GuardrailError,
                        "aggregate glob compile/match steps",
                    ):
                        _expand_component_paths(
                            root,
                            "svc",
                            ["file-*.txt"],
                            source=source,
                        )

    def test_validation_shares_one_budget_across_boundary_and_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo_with_files(root)
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": ["file-*.txt"],
                        },
                        "behavior": {"paths": ["file-*.txt"]},
                    }
                },
            }

            with patch.object(utils, "MAX_GLOB_OPERATION_STEPS", 1_500):
                errors = validate_config(config, root, source="head")

            aggregate_errors = [
                error
                for error in errors
                if "aggregate glob compile/match steps" in error
            ]
            self.assertEqual(len(aggregate_errors), 1, errors)
            self.assertIn(
                "path expansion could not be validated",
                aggregate_errors[0],
            )

    def test_explain_returns_one_actionable_budget_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._repo_with_files(root)
            for path in sorted((root / "svc").glob("*.txt")):
                path.write_text("changed\n", encoding="utf-8")
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {
                            "provider": "path-hash",
                            "paths": ["file-*.txt"],
                        },
                    }
                },
            }

            with patch.object(utils, "MAX_GLOB_OPERATION_STEPS", 50):
                result = analyze_explain_changes(
                    config,
                    root,
                    "svc",
                    base_ref="HEAD",
                    source="working-tree",
                )

            self.assertEqual(
                set(result),
                {"error"},
            )
            self.assertIn("failed closed", result["error"])
            self.assertIn("reduce wildcard declarations", result["error"])


def _independent_compile_steps(pattern: str) -> int:
    """Measure what compiling one pattern costs, outside any operation.

    The count comes from the compiler itself rather than from a number
    written down here, so the tests below can name the admission charge as
    its own quantity without also freezing the compiler's arithmetic.
    """
    charges: list[int] = []
    _compile_path_glob(pattern, _step_consumer=charges.append)
    return sum(charges)


class AdmissionChargeTests(unittest.TestCase):
    """The step an operation charges for a pattern it has not compiled yet.

    ``_PathGlobOperation.prepare`` charges one step for admitting a pattern
    that is not already compiled, and then charges every compile primitive
    that pattern spends on top of it. The refusal half of the budget is
    asserted all over this file. The charging half was asserted only
    relatively: the property machine in tests/test_chunk_24_globs.py
    asserts that a fresh compile leaves ``steps`` higher than it found it,
    which stays true when the admission charge is dropped to zero, because
    the compile primitives are charged either way. Dropping it
    (MUT-GIT-SOURCE-521) therefore left this file, the glob property
    machine and the git-source tests green together. That change does not
    lose the ceiling, it quietly moves it: every distinct pattern gets one
    step of work free, so a configuration with fifty wildcard declarations
    gets fifty.

    These tests measure the compile cost independently and assert that the
    operation's total is that cost plus one per distinct pattern, which is
    the only form of the claim in which an admission charge of zero has to
    fail. The pair at the end says the same thing behaviourally: a budget
    of exactly the compile work refuses the pattern, and one step more
    accepts it.

    Covers OBL-GIT-SOURCE-055.
    """

    CONTEXT = "Component path expansion"

    PATTERN = "svc/**/*.py"

    PATTERNS = (
        "svc/**/*.py",
        "api/*.yaml",
        "api/v2/openapi.yaml",
        "docs/**/*.md",
        "src/**/internal/*.go",
        "pkg/a?c/*.rs",
        "web/[abc]*/index.ts",
        "lib/**",
        "tools/build.py",
        "svc/**/test_*.py",
    )

    def test_premise_each_pattern_charges_compile_work_of_its_own(self) -> None:
        """PREMISE: compiling these patterns is not free, and they differ.

        If compiling a pattern cost nothing, the equalities below would be
        carried entirely by the admission charge, and they would say
        nothing about the two charges being separate quantities. This also
        confirms that every pattern really compiles, because a pattern the
        compiler refuses reports a compile cost as well, and that the ten
        patterns are genuinely distinct, so the operation admits ten of
        them rather than nine and a cache hit.
        """
        self.assertEqual(len(set(self.PATTERNS)), len(self.PATTERNS))
        self.assertIn(self.PATTERN, self.PATTERNS)
        for pattern in self.PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(_compile_path_glob(pattern))
                self.assertGreater(_independent_compile_steps(pattern), 1)

    def test_preparing_a_pattern_costs_its_compile_work_and_one_more(self) -> None:
        """One fresh pattern costs its compile primitives plus a single step.

        The admission charge is asserted here as a quantity of its own, so
        an operation that admits a pattern for nothing fails even though
        the compile primitives it still charges keep the total moving
        upwards. That is exactly what MUT-GIT-SOURCE-521 does, and what
        every relative assertion about ``steps`` misses.
        """
        compile_steps = _independent_compile_steps(self.PATTERN)
        operation = _PathGlobOperation(self.CONTEXT)

        compiled = operation.prepare(self.PATTERN)

        self.assertEqual(compiled.pattern, self.PATTERN)
        self.assertEqual(operation.compiled_patterns, 1)
        self.assertEqual(
            operation.steps - compile_steps,
            1,
            "admitting a pattern the operation has not compiled before "
            "must cost exactly one step on top of its compile work",
        )
        self.assertEqual(operation.steps, compile_steps + 1)

    def test_ten_distinct_patterns_are_charged_ten_admission_steps(self) -> None:
        """The admission charge is per pattern, so it scales with the config.

        Asserting one instance of the charge would still admit a version
        that charges for the first pattern and lets the rest in free, and
        the size of an undercharge grows with the number of wildcard
        declarations a repository writes. This drives ten distinct
        patterns through one operation and pins the whole difference
        between its total and the compile work those patterns account for.
        """
        compile_steps = sum(
            _independent_compile_steps(pattern) for pattern in self.PATTERNS
        )
        operation = _PathGlobOperation(self.CONTEXT)

        for pattern in self.PATTERNS:
            operation.prepare(pattern)

        self.assertEqual(operation.compiled_patterns, len(self.PATTERNS))
        self.assertEqual(
            operation.steps - compile_steps,
            len(self.PATTERNS),
            "each distinct pattern must be charged its own admission step",
        )

    def test_contrast_a_pattern_already_compiled_is_admitted_for_free(self) -> None:
        """CONTRAST: the charge falls on a distinct pattern, not on a call.

        An accounting rule that charged a step on every call would satisfy
        the assertions above and would bill an operation once for every
        candidate path it matches against a declaration. Repeating one
        prepared pattern twenty times has to leave the total exactly where
        the first preparation left it.
        """
        compile_steps = _independent_compile_steps(self.PATTERN)
        operation = _PathGlobOperation(self.CONTEXT)
        operation.prepare(self.PATTERN)
        after_first = operation.steps

        for _ in range(20):
            operation.prepare(self.PATTERN)

        self.assertEqual(operation.steps, after_first)
        self.assertEqual(operation.steps, compile_steps + 1)
        self.assertEqual(operation.compiled_patterns, 1)

    def test_a_budget_of_exactly_the_compile_work_refuses_the_pattern(self) -> None:
        """The admission step is where the budget binds, not a comment.

        The budget here comes from the independently measured compile cost
        rather than from the operation's own total, because a total read
        back from the same object moves along with the charge and would
        agree with itself under MUT-GIT-SOURCE-521. A budget that pays for
        every compile primitive and nothing more has to refuse this
        pattern, because admitting it costs one step beyond that.
        """
        compile_steps = _independent_compile_steps(self.PATTERN)
        operation = _PathGlobOperation(self.CONTEXT, max_steps=compile_steps)

        with self.assertRaisesRegex(
            GuardrailError,
            "aggregate glob compile/match steps",
        ):
            operation.prepare(self.PATTERN)

    def test_contrast_a_budget_one_step_larger_accepts_the_pattern(self) -> None:
        """CONTRAST: the ordinary pattern is still accepted at the boundary.

        The refusal above has to come from the arithmetic rather than from
        an operation that refuses everything, so the same pattern under a
        budget of the compile work plus the admission step is prepared
        successfully and spends that budget exactly. Under
        MUT-GIT-SOURCE-521 it spends one step less than the budget, and
        this assertion is the one that says so.
        """
        compile_steps = _independent_compile_steps(self.PATTERN)
        operation = _PathGlobOperation(self.CONTEXT, max_steps=compile_steps + 1)

        compiled = operation.prepare(self.PATTERN)

        self.assertEqual(compiled.pattern, self.PATTERN)
        self.assertEqual(operation.steps, operation.max_steps)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
