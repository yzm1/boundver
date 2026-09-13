"""Three shells render, and three shells must parse.

Rendering a completion script is not the same as it being loadable. The module
builds all three at import, so a malformed zsh case block or fish complete line
ships as a string that only fails when a user sources it - and the two shells
that were never syntax-checked are exactly the two nobody runs by hand while
developing on Windows.

The second half is a rule that only bash expresses as branching code: `--format`
offers a third value for `review` and two everywhere else, written as a case
over $command. That arm is generated, so a regression in the generator would
leave every table test green while quietly offering `plan` to `verify`.

Covers OBL-OUTPUT-025.
"""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import unittest
from pathlib import Path

from boundver import _completions

#: How the parse-only flag is spelled for each shell.
SYNTAX_ONLY = {
    "bash": ("-n",),
    "zsh": ("-n",),
    "fish": ("--no-execute",),
}


def _resolve_bash():
    platform_helpers = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "_release_platform.py")
    )
    return platform_helpers["resolve_bash"](os.environ.get("PATH"))


def _interpreter(shell: str):
    if shell == "bash":
        return _resolve_bash()
    return shutil.which(shell)


class ScriptSyntaxTests(unittest.TestCase):
    """OBL-OUTPUT-025: every rendered script parses under its own shell."""

    def _check(self, shell: str) -> None:
        interpreter = _interpreter(shell)
        if not interpreter:
            self.skipTest(f"{shell} is unavailable")
        script = _completions._COMPLETION_SCRIPTS[shell]
        result = subprocess.run(
            [interpreter, *SYNTAX_ONLY[shell]],
            input=script.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0, result.stderr.decode("utf-8", "replace")
        )

    def test_bash_parses(self):
        self._check("bash")

    def test_zsh_parses(self):
        self._check("zsh")

    def test_fish_parses(self):
        self._check("fish")

    def test_every_shipped_shell_is_covered_by_a_check(self):
        """So a fourth shell cannot be added without a parse test."""
        self.assertEqual(set(_completions._COMPLETION_SCRIPTS), set(SYNTAX_ONLY))

    def test_a_broken_script_would_be_caught(self):
        """The premise: the parse-only flag really does reject bad syntax."""
        bash = _resolve_bash()
        if not bash:
            self.skipTest("Bash is unavailable")
        result = subprocess.run(
            [bash, "-n"],
            input=b"case $x in\n",
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)


class FormatChoiceTests(unittest.TestCase):
    """OBL-OUTPUT-025: --format offers plan to review and to nothing else."""

    def setUp(self):
        self.script = _completions._BASH_COMPLETION

    def test_the_review_arm_offers_the_plan_format(self):
        self.assertIn('review) options="json text plan" ;;', self.script)

    def test_the_default_arm_offers_the_two_common_formats(self):
        self.assertIn('*) options="json text" ;;', self.script)

    def test_the_arm_is_a_case_over_the_command(self):
        assignment = _completions._bash_choice_assignment("--format", " " * 12)
        self.assertTrue(assignment.lstrip().startswith('case "$command" in'))
        self.assertEqual(
            self.script.count(assignment), 2, "expected both --format paths"
        )

    def test_both_spellings_of_the_option_get_the_override(self):
        """`--format value` and `--format=value` complete the same way."""
        self.assertEqual(
            self.script.count('review) options="json text plan" ;;'), 2
        )

    def test_no_other_command_is_given_the_plan_format(self):
        for line in self.script.splitlines():
            if 'options="json text plan"' in line:
                self.assertIn("review)", line, line)

    def test_the_tables_agree_with_the_rendered_script(self):
        """The premise: the override the generator reads is the review one."""
        overrides = {
            command: choices
            for (command, option), choices in
            _completions._COMMAND_OPTION_CHOICES.items()
            if option == "--format"
        }
        self.assertEqual(overrides, {"review": ("json", "text", "plan")})
        self.assertEqual(
            _completions._OPTION_CHOICES["--format"], ("json", "text")
        )

    def test_the_other_shells_carry_the_same_distinction(self):
        self.assertIn("json text plan", _completions._ZSH_COMPLETION)
        self.assertIn(
            _completions._fish_option("review", "--format"),
            _completions._FISH_COMPLETION,
        )


if __name__ == "__main__":
    unittest.main()
