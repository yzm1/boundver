"""The argv boundver is willing to hand to Git.

Every Git call goes through one builder, and that builder is the security
boundary: a subcommand outside its allowlist must be refused before a process
exists, because the refused ones reach the network or run code the repository
supplies. The allowlist check is not a simple `args[0] in ...` - a legal prefix
of global options may precede the subcommand - so the walk over that prefix is
the part worth testing, and it is only ever exercised on the accept path.

`diff` needs a second rule of its own. A worktree diff runs clean filters, so
one is refused unless it names two revisions or asks for the index, and every
accepted diff has three neutralising flags inserted. Where they are inserted
matters as much as that they are: Git takes the last spelling of an option, so
a flag appended at the end would silently lose to a caller's own.

Covers OBL-GIT-SOURCE-034 and OBL-GIT-SOURCE-035.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from boundver._git import (
    _OFFLINE_GIT_SUBCOMMANDS,
    _offline_git_command,
)

#: Subcommands the allowlist exists to keep out. The first four reach the
#: network; the rest run repository-supplied code or rewrite history.
FORBIDDEN = (
    "fetch",
    "push",
    "ls-remote",
    "remote",
    "submodule",
    "clone",
    "gc",
    "hook",
    "apply",
    "am",
)

#: The flags every accepted diff must carry, in this order.
INJECTED = ["--no-ext-diff", "--no-textconv", "--ignore-submodules=dirty"]

#: Diffs whose tokens before `--` are short options rather than revisions. Git
#: spells many diff options with a single dash, so a revision count that only
#: discounted `--` tokens would read these as revision names and let the
#: worktree diff run.
SHORT_FLAG_DIFFS = (
    ("-M", "-w"),
    ("-M", "-w", "--", "svc"),
    ("-U0",),
    ("-w", "--stat"),
    ("-M", "HEAD"),
)

ROOT = Path(__file__).resolve().parents[1]


def _prefixes(root: Path):
    """Every legal way to spell a prefix before the subcommand token."""
    safe = f"safe.directory={root.resolve()}"
    return {
        "none": [],
        "literal pathspecs": ["--literal-pathspecs"],
        "safe directory": ["-c", safe],
        "both": ["--literal-pathspecs", "-c", safe],
        "both reversed": ["-c", safe, "--literal-pathspecs"],
        "two configs": ["-c", safe, "-c", safe],
    }


class AllowlistTests(unittest.TestCase):
    """OBL-GIT-SOURCE-034: refused wherever the token appears."""

    def test_every_forbidden_subcommand_is_refused_behind_every_prefix(self):
        for subcommand in FORBIDDEN:
            for label, prefix in _prefixes(ROOT).items():
                with self.subTest(subcommand=subcommand, prefix=label):
                    with self.assertRaises(ValueError) as raised:
                        _offline_git_command(ROOT, [*prefix, subcommand, "--help"])
                    self.assertIn("offline allowlist", str(raised.exception))

    def test_none_of_them_is_on_the_allowlist(self):
        """The premise: the refusals above are the allowlist, not a typo."""
        self.assertEqual(set(FORBIDDEN) & _OFFLINE_GIT_SUBCOMMANDS, set())

    def test_an_approved_subcommand_survives_every_prefix(self):
        for label, prefix in _prefixes(ROOT).items():
            with self.subTest(prefix=label):
                command = _offline_git_command(ROOT, [*prefix, "rev-parse", "HEAD"])
                self.assertEqual(command[-2:], ["rev-parse", "HEAD"])
                for token in prefix:
                    self.assertIn(token, command)

    def test_the_prefix_walk_does_not_fall_off_the_end(self):
        """A prefix with no subcommand after it is refused, not accepted."""
        for prefix in _prefixes(ROOT).values():
            if not prefix:
                continue
            with self.subTest(prefix=" ".join(prefix)):
                with self.assertRaises(ValueError):
                    _offline_git_command(ROOT, list(prefix))

    def test_an_empty_argv_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            _offline_git_command(ROOT, [])
        self.assertIn("offline allowlist", str(raised.exception))

    def test_only_safe_directory_may_be_set_on_the_command_line(self):
        for value in ("core.pager=sh", "safe.directory", "alias.x=!sh", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    _offline_git_command(ROOT, ["-c", value, "rev-parse", "HEAD"])
                self.assertIn("command-scoped Git configuration", str(raised.exception))

    def test_a_dangling_config_flag_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            _offline_git_command(ROOT, ["-c"])
        self.assertIn("command-scoped Git configuration", str(raised.exception))

    def test_an_unknown_global_option_is_not_walked_over(self):
        """Only --literal-pathspecs is a recognised prefix."""
        with self.assertRaises(ValueError) as raised:
            _offline_git_command(ROOT, ["--exec-path=/tmp", "rev-parse", "HEAD"])
        self.assertIn("offline allowlist", str(raised.exception))

    def test_a_forbidden_token_after_an_approved_one_is_an_argument(self):
        """The check is positional: only the first token is the subcommand."""
        command = _offline_git_command(ROOT, ["rev-parse", "--verify", "fetch"])
        self.assertEqual(command[-3:], ["rev-parse", "--verify", "fetch"])


class DiffGuardTests(unittest.TestCase):
    """OBL-GIT-SOURCE-035: which diffs run, and what they always carry."""

    def _diff(self, *arguments: str):
        return _offline_git_command(ROOT, ["diff", *arguments])

    def test_a_worktree_diff_is_refused(self):
        for arguments in ((), ("--", "path"), ("--name-only",), ("HEAD",)):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError) as raised:
                    self._diff(*arguments)
                self.assertIn("clean filters", str(raised.exception))

    def test_two_revisions_are_accepted(self):
        command = self._diff("HEAD~1", "HEAD")
        self.assertEqual(command[-2:], ["HEAD~1", "HEAD"])

    def test_a_short_flag_does_not_count_as_a_revision(self):
        """MUT-GIT-SOURCE-405: `-M` and its kind are options, not revisions.

        The refusal table above only ever fed the builder diffs whose tokens
        before `--` were double-dash options or nothing at all, so nothing
        noticed when the revision filter was narrowed from `startswith("-")`
        to `startswith("--")`. Under that narrowing a short option counts as a
        revision, `git diff -M -w` appears to name two of them, and the builder
        accepts a worktree diff. A worktree diff is precisely what this guard
        exists to stop, because reading the working tree runs whatever clean
        filter the repository configures. Every shape below names fewer than
        two real revisions once its short options are discounted, so every one
        of them has to be refused.
        """
        for arguments in SHORT_FLAG_DIFFS:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError) as raised:
                    self._diff(*arguments)
                self.assertIn("clean filters", str(raised.exception))

    def test_the_short_flag_table_really_holds_short_flags(self):
        """The premise: those refusals turn on single-dash spellings.

        If someone rewrote the rows as `--minimal` or `--ignore-all-space`
        they would still be refused, but only for the reason the older table
        already covered, and a miscount of short options would go unnoticed
        once more. So each row has to carry at least one token spelled with
        exactly one dash, and each row has to name at most one token that is
        not an option at all.
        """
        for arguments in SHORT_FLAG_DIFFS:
            with self.subTest(arguments=arguments):
                short_flags = [
                    token
                    for token in arguments
                    if token.startswith("-") and not token.startswith("--")
                ]
                self.assertTrue(short_flags)
                plain_tokens = [
                    token for token in arguments if not token.startswith("-")
                ]
                self.assertLessEqual(len(plain_tokens), 1)

    def test_a_short_flag_beside_two_revisions_is_still_accepted(self):
        """The contrast: the rule counts revisions, it does not ban options.

        A guard that simply refused every diff carrying a short option would
        satisfy the refusals above while breaking the ordinary call, so the
        ordinary call is pinned here. Two real revisions introduced by `-M`
        are accepted, the caller's own argv survives in order at the end of
        the command, and the neutralising flags still sit immediately behind
        the subcommand. An index diff carrying a short option is accepted too.
        """
        command = self._diff("-M", "HEAD~1", "HEAD")
        self.assertEqual(command[-3:], ["-M", "HEAD~1", "HEAD"])
        index = command.index("diff")
        self.assertEqual(command[index + 1 : index + 4], INJECTED)
        cached = self._diff("-M", "--cached")
        self.assertEqual(cached[-2:], ["-M", "--cached"])

    def test_a_cached_diff_is_accepted_with_no_revisions(self):
        command = self._diff("--cached")
        self.assertIn("--cached", command)

    def test_a_revision_after_the_separator_does_not_count(self):
        """Everything past `--` is a pathspec, whatever it looks like."""
        with self.assertRaises(ValueError):
            self._diff("HEAD", "--", "HEAD")

    def test_the_flags_are_inserted_immediately_after_the_subcommand(self):
        for label, arguments in (
            ("two revisions", ("HEAD~1", "HEAD")),
            ("cached", ("--cached",)),
            ("with a pathspec", ("HEAD~1", "HEAD", "--", "svc")),
        ):
            with self.subTest(diff=label):
                command = self._diff(*arguments)
                index = command.index("diff")
                self.assertEqual(command[index + 1 : index + 4], INJECTED)

    def test_a_later_spelling_still_wins_under_last_option_wins(self):
        """Which is the whole point of inserting rather than appending."""
        command = self._diff("--ignore-submodules=all", "HEAD~1", "HEAD")
        self.assertLess(
            command.index("--ignore-submodules=dirty"),
            command.index("--ignore-submodules=all"),
        )

    def test_the_injection_follows_the_subcommand_behind_a_prefix(self):
        command = _offline_git_command(
            ROOT, ["--literal-pathspecs", "diff", "HEAD~1", "HEAD"]
        )
        index = command.index("diff")
        self.assertEqual(command[index + 1 : index + 4], INJECTED)
        self.assertLess(command.index("--literal-pathspecs"), index)

    def test_diff_tree_is_neutralised_the_same_way(self):
        command = _offline_git_command(ROOT, ["diff-tree", "-r", "HEAD"])
        index = command.index("diff-tree")
        self.assertEqual(command[index + 1 : index + 4], INJECTED)

    def test_diff_tree_needs_no_revision_pair(self):
        """The two-revision rule is diff's alone."""
        self.assertIn("diff-tree", _offline_git_command(ROOT, ["diff-tree", "HEAD"]))

    def test_the_helper_modes_are_refused_outright(self):
        for subcommand in ("diff", "diff-tree"):
            for argument in ("--ext-diff", "--textconv", "--no-index", "--submodule=log"):
                with self.subTest(subcommand=subcommand, argument=argument):
                    with self.assertRaises(ValueError) as raised:
                        _offline_git_command(
                            ROOT, [subcommand, argument, "HEAD~1", "HEAD"]
                        )
                    self.assertIn("helper or submodule", str(raised.exception))

    def test_the_pager_and_signature_display_are_disabled_for_every_call(self):
        command = _offline_git_command(ROOT, ["rev-parse", "HEAD"])
        self.assertIn("--no-pager", command)
        self.assertIn("log.showSignature=false", command)
        self.assertIn("core.fsmonitor=false", command)


if __name__ == "__main__":
    unittest.main()
