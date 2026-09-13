"""Three shells rendered from one table, and the one option they disagree on.

The completions module says its three scripts come from shared tables so that
adding an option cannot update one shell and silently leave another behind.
That is a claim worth checking rather than trusting, because the tables are
consulted differently by each renderer.

Covers OBL-OUTPUT-005.
"""

from __future__ import annotations

import re
import unittest

import boundver._completions as completions

#: The one option whose path completion differs between shells. zsh gets a
#: directory-only action from _OPTION_ARGUMENTS; bash filters that same table
#: for an action of exactly `_files` and so leaves it out, and fish reads
#: _FILE_OPTIONS, which omits it too.
KNOWN_DIVERGENCE = "--exclude"

#: The `case "$cur" in` block of the rendered bash script, which decides what
#: `--option=value` completes to. Its `esac` sits at four spaces, while the
#: `case "$command" in` nested inside the choice arms closes at twelve, so
#: anchoring on the indent picks out the right one.
_BASH_EQUALS_CASE = re.compile(r'\n    case "\$cur" in\n(.*?)\n    esac\n', re.DOTALL)

#: The `case "$prev" in` block, which decides what `--option <TAB>` completes
#: to. It closes at eight spaces.
_BASH_PREV_CASE = re.compile(
    r'\n        case "\$prev" in\n(.*?)\n        esac\n', re.DOTALL
)


def _zsh_completes_paths(option: str) -> bool:
    _label, action = completions._OPTION_ARGUMENTS[option]
    return "_files" in action


def _bash_arms(script: str, block: "re.Pattern[str]") -> tuple:
    """Split one bash `case` block into its path arm and its refusing arm.

    Bash decides path completion in the rendered script rather than in a
    Python table, so the oracle has to read the script the way
    `_fish_completes_paths` reads the fish one. Each arm is labelled by the
    patterns before its first `)`, and the two that matter here are the arm
    whose body runs `compgen -f` and the arm whose body is
    `COMPREPLY=(); return`. Arms carrying neither marker are ignored, which
    covers the choice arms and the stray fragments that splitting on `;;`
    carves out of the nested `case "$command" in`.
    """
    found = block.search(script)
    if found is None:
        raise AssertionError("the rendered bash script has no such case block")
    completing_paths = set()
    refusing = set()
    for arm in found.group(1).split(";;"):
        label, separator, body = arm.partition(")")
        if not separator:
            continue
        patterns = set(label.strip().split("|"))
        if "compgen -f" in body:
            completing_paths |= patterns
        elif "COMPREPLY=(); return" in body:
            refusing |= patterns
    return completing_paths, refusing


def _bash_completes_paths(script: str, option: str) -> bool:
    completing_paths, _refusing = _bash_arms(script, _BASH_EQUALS_CASE)
    return "{0}=*".format(option) in completing_paths


def _fish_completes_paths(script: str, option: str) -> bool:
    match = re.search(r"-l\s+" + re.escape(option.lstrip("-")) + r"\b[^\n]*", script)
    return bool(match and "-F" in match.group(0))


class CompletionTableParityTests(unittest.TestCase):
    """OBL-OUTPUT-005: the three scripts must agree, option by option."""

    @classmethod
    def setUpClass(cls):
        cls.bash = completions._render_bash()
        cls.zsh = completions._render_zsh()
        cls.fish = completions._render_fish()
        cls.options = sorted(completions._OPTION_ARGUMENTS)

    def test_the_three_scripts_render(self):
        """The premise: each is non-empty and mentions the tool."""
        for name, script in (("bash", self.bash), ("zsh", self.zsh), ("fish", self.fish)):
            with self.subTest(shell=name):
                self.assertGreater(len(script), 200)
                self.assertIn("boundver", script)

    def test_every_option_that_takes_a_value_appears_in_all_three(self):
        """One table drives all three, so an option cannot reach only one."""
        for option in self.options:
            with self.subTest(option=option):
                self.assertIn(option, self.bash)
                self.assertIn(option, self.zsh)
                self.assertIn(option.lstrip("-"), self.fish)

    def test_fish_marks_every_value_taking_option_as_requiring_one(self):
        for option in self.options:
            with self.subTest(option=option):
                match = re.search(
                    r"-l\s+" + re.escape(option.lstrip("-")) + r"\b[^\n]*", self.fish
                )
                self.assertIsNotNone(match, option)
                self.assertIn("-r", match.group(0))

    def test_path_completion_agrees_for_every_option_but_one(self):
        for option in self.options:
            if option == KNOWN_DIVERGENCE:
                continue
            with self.subTest(option=option):
                answers = {
                    _zsh_completes_paths(option),
                    _bash_completes_paths(self.bash, option),
                    _fish_completes_paths(self.fish, option),
                }
                self.assertEqual(len(answers), 1, option)

    def test_the_one_divergence_is_asserted_rather_than_unified(self):
        """The obligation allows either. This records which was chosen.

        zsh offers directory completion for --exclude, because its action is
        `_files -/`. bash offers none, because its renderer keeps only the
        options whose action is exactly `_files`, and fish offers none because
        it reads _FILE_OPTIONS and that tuple does not list it. Recording the
        choice here means unifying the tables later will fail this test rather
        than pass silently.
        """
        self.assertTrue(_zsh_completes_paths(KNOWN_DIVERGENCE))
        self.assertFalse(_bash_completes_paths(self.bash, KNOWN_DIVERGENCE))
        self.assertFalse(_fish_completes_paths(self.fish, KNOWN_DIVERGENCE))
        self.assertEqual(
            completions._OPTION_ARGUMENTS[KNOWN_DIVERGENCE][1], "_files -/"
        )
        self.assertNotIn(KNOWN_DIVERGENCE, completions._FILE_OPTIONS)

    def test_the_bash_case_arms_parse_into_two_populated_sets(self):
        """The premise under every bash answer above and below.

        The bash oracle now reads the rendered script instead of a Python
        table, so a parse that quietly matched nothing would make the parity
        assertions vacuous. Every option would look as though it completes no
        paths, the three shells would agree by accident, and MUT-OUTPUT-414
        would slip through again. This asserts the parse really found both
        arms, that they are disjoint, and that it sorted two known options
        into the arms they belong in.
        """
        completing_paths, refusing = _bash_arms(self.bash, _BASH_EQUALS_CASE)
        self.assertTrue(completing_paths)
        self.assertTrue(refusing)
        self.assertEqual(completing_paths & refusing, set())
        self.assertIn("--config=*", completing_paths)
        self.assertIn("--components=*", refusing)
        for pattern in sorted(completing_paths | refusing):
            with self.subTest(pattern=pattern):
                self.assertTrue(pattern.endswith("=*"), pattern)

    def test_bash_offers_paths_for_exactly_the_declared_file_options(self):
        """MUT-OUTPUT-414: the bash renderer's file filter must stay exact.

        _render_bash builds its `compgen -f` arm by keeping the options whose
        _OPTION_ARGUMENTS action is exactly `_files`. Loosening that test to a
        prefix match pulls in --exclude, whose action is `_files -/`, and bash
        then offers full file completion for an option that zsh restricts to
        directories and fish does not complete at all. Nothing caught that
        before, because the bash side of this parity check consulted the
        _FILE_OPTIONS tuple, which the bash renderer never reads.
        """
        completing_paths, _refusing = _bash_arms(self.bash, _BASH_EQUALS_CASE)
        self.assertEqual(
            completing_paths,
            {"{0}=*".format(option) for option in completions._FILE_OPTIONS},
        )

    def test_bash_still_completes_paths_for_the_ordinary_file_options(self):
        """The contrast: an exact filter must not end up refusing everything.

        A renderer whose filter matched no action at all would drop every
        option into the `COMPREPLY=(); return` arm, and the assertion that
        --exclude completes nothing in bash would then pass for entirely the
        wrong reason. Each ordinary file option has to keep reaching the
        `compgen -f` arm.
        """
        for option in completions._FILE_OPTIONS:
            with self.subTest(option=option):
                self.assertTrue(_bash_completes_paths(self.bash, option))

    def test_bash_agrees_with_itself_across_the_two_option_spellings(self):
        """One filter feeds both bash arms, so the two cannot disagree.

        `--config=<TAB>` is answered by the `case "$cur" in` block and
        `--config <TAB>` by the `case "$prev" in` block, and both patterns are
        built from the same comprehension. Reading them back separately keeps
        a change to one spelling from leaving the other behind.
        """
        equals_paths, equals_refusing = _bash_arms(self.bash, _BASH_EQUALS_CASE)
        prev_paths, prev_refusing = _bash_arms(self.bash, _BASH_PREV_CASE)
        self.assertTrue(prev_paths)
        self.assertTrue(prev_refusing)
        self.assertEqual({option[:-2] for option in equals_paths}, prev_paths)
        self.assertEqual({option[:-2] for option in equals_refusing}, prev_refusing)

    def test_every_file_option_has_a_matching_zsh_action(self):
        """The two tables must not drift apart in the other direction."""
        for option in completions._FILE_OPTIONS:
            with self.subTest(option=option):
                self.assertIn(option, completions._OPTION_ARGUMENTS)
                self.assertTrue(_zsh_completes_paths(option))

    def test_choice_lists_reach_every_shell(self):
        for option, choices in completions._OPTION_CHOICES.items():
            for choice in choices:
                with self.subTest(option=option, choice=choice):
                    self.assertIn(choice, self.bash)
                    self.assertIn(choice, self.zsh)
                    self.assertIn(choice, self.fish)


if __name__ == "__main__":
    unittest.main()
