"""Every command, both formats, one hostile name.

The escaping helpers are tested where they live. What is not testable there is
whether every command actually goes through them: a single raw f-string, a
traceback, or a module that never imported the shadowed `print` would put
repository-controlled bytes on a stream without any of the helpers being
involved, and a per-helper test would still pass.

So this file works from the outside in. One component name and one slice name
carry the whole adversarial alphabet, every command runs over them in every
format it offers, and the bytes that come back are classified. It then closes
the loop from the inside by enumerating the package's own emitters, because a
command added tomorrow is only covered by the sweep if it is in the list.

Covers OBL-OUTPUT-017.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests._scenarios import Scenario

_SRC = Path(__file__).resolve().parents[1] / "src"

#: Everything the obligation names, in one string. The name is a JSON object
#: key rather than a path, so no filesystem refuses it.
HOSTILE = (
    "a"
    + chr(0x1B) + "[31m"      # ESC and a CSI sequence
    + chr(0x01) + chr(0x1F)   # C0
    + chr(0x7F) + chr(0x9B)   # DEL and C1 CSI
    + chr(0x85)               # NEL
    + chr(0x0D)               # CR
    + chr(0x2028) + chr(0x2029)
    + chr(0x202E) + chr(0x2066)   # RLO and LRI
    + chr(0x1F600)
    + "b"
)

#: Codepoints that reorder or hide neighbouring text.
BIDI = tuple(range(0x202A, 0x2030)) + tuple(range(0x2066, 0x206A))

SLICE = HOSTILE + "-slice"

BACKSLASH = chr(92)


def _forbidden(text: str, *, include_bidi: bool) -> list:
    """The classes the obligation forbids, found in emitted text."""
    found = []
    for character in text:
        point = ord(character)
        if character == "\n":
            continue
        if point < 0x20 or 0x7F <= point <= 0x9F or point in (0x2028, 0x2029):
            found.append(f"{point:#06x}")
        elif include_bidi and point in BIDI:
            found.append(f"{point:#06x}")
    return sorted(set(found))


class _Repository:
    """One repository whose component and slice names are hostile."""

    def __init__(self) -> None:
        self.scene = Scenario()
        self.scene.config["components"] = {
            HOSTILE: {"path": "svc", "boundary": {"provider": "leaf", "paths": []}}
        }
        self.scene.config["slices"] = {SLICE: {"components": [HOSTILE]}}
        self.scene.file("svc/main.py", "x\n")
        self.scene.commit()
        assert self.run("generate", "--source", "head").returncode == 0
        self.scene.commit("lock")
        self.base = self.scene.head()
        self.scene.append_line("svc/main.py", "y\n")
        self.scene.commit("edit")
        assert self.run("generate", "--source", "head").returncode == 0
        self.scene.commit("relock")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def run(self, *args: str) -> subprocess.CompletedProcess:
        """Bytes, not text: the question is what reached the stream."""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(_SRC) + (
            os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH")
            else ""
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-m", "boundver", *args],
            cwd=self.scene.root,
            capture_output=True,
            env=environment,
        )

    def emitted(self, *args: str) -> str:
        result = self.run(*args)
        blob = result.stdout + result.stderr
        # A Windows text stream writes CRLF for every newline boundver emits.
        # That is the platform's line ending, not repository-controlled text.
        return blob.replace(b"\r\n", b"\n").decode("utf-8", "surrogatepass")

    def commands(self):
        """Every command, in every format it offers."""
        target = self.scene.head()
        yield from (
            ("status", "--format", "json"),
            ("status", "--format", "text"),
            ("verify", "--source", "head", "--format", "json"),
            ("verify", "--source", "head", "--format", "text"),
            ("generate", "--source", "head", "--dry-run", "--format", "json"),
            ("generate", "--source", "head", "--dry-run", "--format", "text"),
            ("review", "--base", self.base, "--target", target, "--format", "json"),
            ("review", "--base", self.base, "--target", target, "--format", "text"),
            ("review", "--base", self.base, "--target", target, "--format", "plan"),
            ("slice", SLICE, "--format", "json"),
            ("slice", SLICE, "--format", "text"),
            ("validate-config",),
            ("check-config",),
            ("explain", HOSTILE),
            ("why", HOSTILE, "--format", "json"),
            ("why", HOSTILE, "--format", "text"),
            ("discover", "--format", "json"),
            ("discover", "--format", "text"),
            ("migrate-lock", "--dry-run", "--format", "json"),
            ("migrate-lock", "--dry-run", "--format", "text"),
            ("remove", HOSTILE + "-absent"),
            ("add", HOSTILE, "svc", "--provider", "leaf"),
        )


def _label(command) -> str:
    """A readable key: the command, its format, and nothing hostile."""
    name = command[0]
    if "--format" in command:
        return f"{name} {command[command.index('--format') + 1]}"
    return name


class CommandSweepTests(unittest.TestCase):
    """OBL-OUTPUT-017: the invariant, over every command and format."""

    @classmethod
    def setUpClass(cls):
        cls.repository = _Repository().__enter__()
        cls.output = {
            _label(command): cls.repository.emitted(*command)
            for command in cls.repository.commands()
        }

    @classmethod
    def tearDownClass(cls):
        cls.repository.__exit__(None, None, None)

    def _carrying(self) -> list:
        """The commands whose output shows the name, however it is escaped."""
        return sorted(
            label for label, text in self.output.items()
            if "a\\x1b" in text or "a\\u001b" in text
        )

    def test_the_hostile_name_really_did_reach_the_output(self):
        """The premise: an invariant over empty output would be vacuous."""
        carrying = self._carrying()
        self.assertGreaterEqual(len(carrying), 12, sorted(self.output))
        self.assertIn("status text", carrying)
        self.assertIn("status json", carrying)

    def test_no_control_or_separator_byte_is_emitted(self):
        for label, text in self.output.items():
            with self.subTest(command=label):
                self.assertEqual(_forbidden(text, include_bidi=False), [])

    def test_each_format_shows_the_escape_its_own_way(self):
        """Human output escapes for a terminal; machine output quotes JSON."""
        for label in self._carrying():
            with self.subTest(command=label):
                machine = label.endswith(("json", "plan"))
                spelling = BACKSLASH + ("u001b" if machine else "x1b")
                self.assertIn(spelling, self.output[label])

    def test_no_bidirectional_control_is_emitted(self):
        """Known divergence: _display_text passes these through unchanged."""
        for label, text in self.output.items():
            with self.subTest(command=label):
                self.assertEqual(_forbidden(text, include_bidi=True), [])

    def test_no_format_retains_a_bidirectional_control(self):
        carriers = sorted(
            label for label, text in self.output.items()
            if _forbidden(text, include_bidi=True)
        )
        self.assertEqual(carriers, [])

    def test_json_quoting_escapes_what_the_text_format_does_not(self):
        for label, text in self.output.items():
            if label.endswith("json") and "a\\u001b" in text:
                with self.subTest(command=label):
                    self.assertEqual(_forbidden(text, include_bidi=True), [])
                    self.assertIn("\\u202e", text)


class EmitterEnumerationTests(unittest.TestCase):
    """OBL-OUTPUT-017: nothing reaches a stream by another route."""

    #: The two modules that rebind ``print`` to the escaping printer.
    SHADOWED = {"_output.py", "core.py"}

    def _modules(self):
        for path in sorted(_SRC.joinpath("boundver").glob("*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"))

    def test_exactly_two_modules_rebind_print(self):
        rebinding = set()
        for path, tree in self._modules():
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "print"
                    for target in node.targets
                ):
                    rebinding.add(path.name)
        self.assertEqual(rebinding, self.SHADOWED)

    def test_every_other_print_call_takes_only_literal_text(self):
        """A literal cannot carry a repository-controlled byte."""
        for path, tree in self._modules():
            if path.name in self.SHADOWED:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
                    continue
                with self.subTest(module=path.name, line=node.lineno):
                    for argument in node.args:
                        self.assertIsInstance(argument, ast.Constant)
                        self.assertIsInstance(argument.value, str)

    def test_the_raw_stream_writes_are_the_two_known_ones(self):
        """Both write trusted or JSON-quoted text, never a bare name."""
        writes = []
        for path, tree in self._modules():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr in ("stdout", "stderr")
                ):
                    writes.append((path.name, node.lineno))
        # The two in _output.py are inside safe_print itself, which is the
        # helper every other module reaches a stream through.
        self.assertEqual(sorted(set(name for name, _line in writes)),
                         ["_output.py", "core.py"])


if __name__ == "__main__":
    unittest.main()
