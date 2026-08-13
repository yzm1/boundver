"""Completion scripts stay in lockstep with the public argparse surface."""

import argparse
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from boundver import _completions
from boundver import core


class _ParserCaptured(Exception):
    pass


def _capture_cli_parser():
    captured = []

    def capture(parser, *args, **kwargs):
        captured.append(parser)
        raise _ParserCaptured

    with patch.object(argparse.ArgumentParser, "parse_args", capture):
        with patch.object(sys, "argv", ["boundver"]):
            try:
                core.main()
            except _ParserCaptured:
                pass
    if len(captured) != 1:
        raise AssertionError("CLI parser was not captured exactly once")
    return captured[0]


def _option_strings(parser):
    return tuple(
        option
        for action in parser._actions
        if not isinstance(action, argparse._SubParsersAction)
        for option in action.option_strings
    )


class CompletionParserParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = _capture_cli_parser()
        cls.subparsers = next(
            action
            for action in cls.parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices

    def test_command_and_option_tables_match_live_parser(self):
        self.assertEqual(tuple(self.subparsers), _completions._COMMANDS)
        self.assertEqual(
            _option_strings(self.parser), _completions._GLOBAL_OPTIONS
        )
        self.assertEqual(
            set(_completions._COMMAND_OPTIONS), set(self.subparsers)
        )
        for command, parser in self.subparsers.items():
            with self.subTest(command=command):
                self.assertEqual(
                    _option_strings(parser),
                    _completions._COMMAND_OPTIONS[command],
                )

    def test_option_arity_and_choices_match_live_parser(self):
        actual_value_options = set()
        actual_choices = {}
        for parser in self.subparsers.values():
            for action in parser._actions:
                if action.nargs != 0:
                    actual_value_options.update(action.option_strings)
                if action.choices is not None:
                    for option in action.option_strings:
                        actual_choices[option] = tuple(action.choices)

        table_options = {
            option
            for options in _completions._COMMAND_OPTIONS.values()
            for option in options
        }
        self.assertEqual(
            actual_value_options,
            set(_completions._OPTION_ARGUMENTS) & table_options,
        )
        self.assertEqual(actual_choices, _completions._OPTION_CHOICES)

    def test_zsh_positional_specs_match_live_parser(self):
        for command, parser in self.subparsers.items():
            positionals = [
                action for action in parser._actions if not action.option_strings
            ]
            completion_specs = _completions._COMMAND_POSITIONALS.get(command, ())
            with self.subTest(command=command):
                self.assertEqual(len(positionals), len(completion_specs))

    def test_renderers_cover_every_command_option(self):
        bash = _completions._BASH_COMPLETION
        zsh = _completions._ZSH_COMPLETION
        fish = _completions._FISH_COMPLETION

        for command in _completions._COMMANDS:
            options = _completions._options_after_command(command)
            with self.subTest(shell="bash", command=command):
                self.assertIn(
                    '{0}) options="{1}"'.format(
                        command, " ".join(options)
                    ),
                    bash,
                )

            zsh_start = zsh.index("          {0})".format(command))
            zsh_end = zsh.index("            ;;", zsh_start)
            zsh_case = zsh[zsh_start:zsh_end]
            with self.subTest(shell="zsh", command=command):
                for option in options:
                    self.assertIn(_completions._zsh_option(option), zsh_case)

            with self.subTest(shell="fish", command=command):
                for option in _completions._COMMAND_OPTIONS[command]:
                    expected = _completions._fish_option(command, option)
                    if expected:
                        self.assertIn(expected, fish)

    def test_post_command_verbosity_is_intentionally_accepted(self):
        self.assertEqual(
            _completions._POST_COMMAND_GLOBAL_OPTIONS,
            ("--quiet", "--verbose"),
        )
        for option in _completions._POST_COMMAND_GLOBAL_OPTIONS:
            with self.subTest(option=option):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with patch.object(
                    sys,
                    "argv",
                    ["boundver", "completions", "--shell", "bash", option],
                ):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        core.main()
                self.assertIn("_boundver_completions", stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")

    @unittest.skipIf(
        sys.platform == "win32",
        "Bash syntax is exercised by the Linux CI matrix",
    )
    def test_bash_script_has_valid_syntax(self):
        result = subprocess.run(
            ["bash", "-n"],
            input=_completions._BASH_COMPLETION.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0, result.stderr.decode("utf-8", "replace")
        )


if __name__ == "__main__":
    unittest.main()
