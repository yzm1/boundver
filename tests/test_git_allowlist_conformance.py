"""Whether the Git in front of us still matches the list written against it.

`_offline_git_command` allows a fixed set of subcommands and refuses a fixed
set of option spellings within them. Both lists were written against one Git,
and a list of literal spellings only bounds the Git it was written for: an
allowlisted subcommand that no longer exists would break boundver, and a
denied spelling the installed Git no longer recognises would mean the refusal
is guarding nothing.

Neither can be settled by reading the list. This asks the installed Git.

Covers OBL-GIT-SOURCE-160.
"""

from __future__ import annotations

import subprocess
import unittest

from boundver._git import _OFFLINE_GIT_SUBCOMMANDS

from tests._scenarios import Scenario

#: Every option spelling _offline_git_command refuses, by subcommand.
DENIED = (
    ("cat-file", "--filters"),
    ("cat-file", "--textconv"),
    ("cat-file", "--batch-command"),
    ("ls-files", "--recurse-submodules"),
    ("rev-list", "--show-signature"),
    ("rev-list", "--pretty"),
    ("rev-list", "--format=%H"),
    ("diff", "--ext-diff"),
    ("diff", "--textconv"),
    ("diff", "--no-index"),
    ("diff", "--submodule"),
    ("diff-tree", "--ext-diff"),
    ("diff-tree", "--textconv"),
    ("diff-tree", "--no-index"),
    ("diff-tree", "--submodule"),
)

#: A spelling no Git will ever have, used as the reference for "unknown".
#: Git's parsers disagree about whether to print an "unknown option" line -
#: cat-file and ls-files do, diff and rev-list print only the usage - so the
#: reliable test is whether an option produces the same output as this one.
INVENTED = "--not-a-real-option"

#: Entries the deny-list names for a subcommand that has no such option.
#: Refusing them is harmless, but the list is broader than it needs to be.
INERT = {("diff-tree", "--no-index")}


class _Repository:
    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def run(self, subcommand: str, option: str, *rest) -> str:
        """Git's reply, with the option spelling removed.

        An unknown option is echoed back in the error line, so two unknown
        spellings differ textually even though the reply is the same reply.
        Removing the spelling makes them comparable.
        """
        result = subprocess.run(
            ["git", subcommand, option, *rest],
            cwd=self.scene.root, capture_output=True, text=True,
        )
        reply = (result.stdout + result.stderr).lower()
        # Git echoes an unknown option without its leading dashes, so both
        # spellings have to go.
        for spelling in (option.lower(), option.lower().lstrip("-")):
            reply = reply.replace(spelling, "<opt>")
        return reply

    def help(self, subcommand: str) -> str:
        result = subprocess.run(
            ["git", subcommand, "-h"], cwd=self.scene.root,
            capture_output=True, text=True,
        )
        return (result.stdout + result.stderr).lower()


class AllowlistConformanceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-160: the list against the Git that is installed."""

    def test_every_allowlisted_subcommand_exists(self):
        with _Repository() as repo:
            for subcommand in sorted(_OFFLINE_GIT_SUBCOMMANDS):
                with self.subTest(subcommand=subcommand):
                    output = repo.help(subcommand)
                    self.assertNotIn("is not a git command", output)
                    self.assertTrue(output.strip(), subcommand)

    def test_a_subcommand_that_does_not_exist_is_recognisable_as_such(self):
        """The premise: the check above can fail."""
        with _Repository() as repo:
            self.assertIn("is not a git command", repo.help("not-a-subcommand"))

    def test_two_invented_options_produce_the_same_reply(self):
        """The premise for the comparison below, on every subcommand."""
        with _Repository() as repo:
            for subcommand, _option in DENIED:
                with self.subTest(subcommand=subcommand):
                    self.assertEqual(
                        repo.run(subcommand, INVENTED, "HEAD"),
                        repo.run(subcommand, INVENTED + "-either", "HEAD"),
                    )

    def test_the_deny_list_still_names_options_this_git_has(self):
        """A refusal that names a spelling Git dropped guards nothing."""
        with _Repository() as repo:
            for subcommand, option in DENIED:
                if (subcommand, option) in INERT:
                    continue
                with self.subTest(subcommand=subcommand, option=option):
                    self.assertNotEqual(
                        repo.run(subcommand, option, "HEAD"),
                        repo.run(subcommand, INVENTED, "HEAD"),
                    )

    def test_the_inert_entries_are_exactly_the_recorded_ones(self):
        """Pin what the installed Git does not have, rather than skipping it."""
        with _Repository() as repo:
            inert = {
                (subcommand, option)
                for subcommand, option in DENIED
                if repo.run(subcommand, option, "HEAD")
                == repo.run(subcommand, INVENTED, "HEAD")
            }
            self.assertEqual(inert, INERT)

    def test_the_denied_list_covers_every_subcommand_that_has_refusals(self):
        """Pin the shape: each refused spelling belongs to an allowed command."""
        for subcommand, _option in DENIED:
            with self.subTest(subcommand=subcommand):
                self.assertIn(subcommand, _OFFLINE_GIT_SUBCOMMANDS)


if __name__ == "__main__":
    unittest.main()
