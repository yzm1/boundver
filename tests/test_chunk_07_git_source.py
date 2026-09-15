"""Adversarial provider diagnostics and Git-source obligation tests.

Provider explanations are non-essential output. Hostile names, return values,
and exceptions must stay bounded and must never suppress a drift verdict.

The Git environment obligations need a different kind of care. The index
arithmetic in ``_git_subprocess_env`` is a one-line ``5 + len(process_values)``
that four fixed-shape tests happen to cover, so the property here generates an
arbitrary number of filter drivers and worktree overrides by patching the two
query functions that supply them - the queries themselves are tested elsewhere,
and patching them is the only way to reach "arbitrary" - then re-derives the
whole invariant from the produced mapping alone: the count, a gap-free run of
indices, key/value pairing, and the absence of any stray index above the count.
Patching those suppliers does mean the settings the property promotes come from
a table in this file rather than from the allowlist in ``_git``, so a separate
test compares the two - the key set and every sampled value - and goes red the
day that allowlist gains or loses a setting.
The ambient-stripping tests plant a decoy repository and prove, before
asserting any absence, that a plain ``git rev-parse`` under the same poisoned
environment really does report the decoy. ``SSH_AUTH_SOCK`` sits beside
``SSH_ASKPASS`` in the table for the same reason: without a name that must
survive, "everything was stripped" would pass on an implementation that
stripped the entire environment. The line-ending obligation is metamorphic and
needs no value oracle, but it does need a premise, because Git would happily
make it true by converting CRLF on the way into the index; every fixture here
asserts the stored blob still holds the carriage returns before comparing a
single digest.

Covers OBL-PROVIDERS-049, OBL-PROVIDERS-050, OBL-PROVIDERS-053,
OBL-GIT-SOURCE-036, OBL-GIT-SOURCE-037 and OBL-GIT-SOURCE-063.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Tuple
from unittest import mock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver import _git as git
from boundver.providers import (
    MAX_PROVIDER_ERROR_BYTES,
    ProviderContext,
    explain_provider_diff,
)

from tests._parity import run_cli
from tests._scenarios import SOURCE_MODES, Scenario

CR = bytes([13])
LF = bytes([10])
NUL = bytes([0])


# ---------------------------------------------------------------------------
# providers.explain_provider_diff
# ---------------------------------------------------------------------------


def _context() -> ProviderContext:
    """A context the explain hook never reads: it only has to be well formed."""
    return ProviderContext(
        repo_root=Path("/repo"),
        component_path="svc",
        boundary_cfg={"paths": []},
        source="working-tree",
        read_file=lambda path: b"",
        list_files=lambda prefix: [],
    )


class NamedProvider:
    """The ordinary case: a provider that declares its own identity."""

    name = "custom.named"


class LegacyProvider:
    """No ``name`` at all, which is the backward-compatible shape."""


class RaisingName:
    """A ``name`` descriptor that raises on every read."""

    @property
    def name(self):
        raise RuntimeError("descriptor exploded")


class SystemExitName:
    """A ``name`` descriptor that raises outside ``Exception``."""

    @property
    def name(self):
        raise SystemExit(7)


class NoneName:
    name = None


class EmptyName:
    name = ""


class HugeName:
    """A short registered name rebound to ten megabytes afterwards."""

    def __init__(self) -> None:
        self.name = "n" * (10 * 1024 * 1024)


class _MegabyteStr:
    def __str__(self) -> str:
        return "b" * (5 * 1024 * 1024)


class MegabyteStrName:
    def __init__(self) -> None:
        self.name = _MegabyteStr()


class _UnformattableStr:
    def __format__(self, spec: str) -> str:
        raise RuntimeError("format exploded")

    def __str__(self) -> str:
        return "polite"


class UnformattableName:
    def __init__(self) -> None:
        self.name = _UnformattableStr()


class _LyingText(str):
    """A ``str`` subclass that keeps its type through ``strip`` and understates
    its own length, which is exactly what the two guards measure."""

    def strip(self, *args):
        return self

    def encode(self, *args, **kwargs):
        return b"short"


class LyingExplainer:
    name = "custom.lying"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return _LyingText("z" * 200_000)


class BoundedExplainer:
    """Returns a plain oversized string, which the byte ceiling must reject."""

    name = "custom.bounded"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return "z" * (MAX_PROVIDER_ERROR_BYTES + 1)


class ShortExplainer:
    name = "custom.short"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return "one field was added"


class RaisingExplainer:
    """A hook that raises the caught kind of exception, so its message ends up
    in the function's return value rather than anywhere else."""

    name = "custom.raising"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        raise RuntimeError("the raising explain hook ran")


#: The two conditions OBL-PROVIDERS-049 names, and the exact prose each must
#: produce. Both must degrade to the class name rather than to "None", "" or
#: an exception.
CLASS_NAME_FALLBACKS = {
    "no name attribute at all": (LegacyProvider, "LegacyProvider boundary changed"),
    "a name descriptor that raises": (RaisingName, "RaisingName boundary changed"),
}

#: The exact prose ``explain_provider_diff`` builds when the explain hook
#: raises a caught exception. Both the unit test and the CLI test assert it,
#: because it is the same string that must be missing from the class-poison
#: run's output for that run to mean the hook never fired.
RAISING_PROVIDER_DETAIL = (
    "custom.raising boundary changed "
    "(provider explanation unavailable: the raising explain hook ran)"
)

#: Shapes a provider can give its ``name`` after registration has measured it.
#: Every one of these is a divergence from OBL-PROVIDERS-050 at the unit level.
MUTATED_NAME_PROVIDERS = {
    "a ten megabyte name": HugeName,
    "a name object whose __str__ is megabytes": MegabyteStrName,
    "a name object whose __format__ raises": UnformattableName,
    "an explanation that lies about its own byte length": LyingExplainer,
}


class ProviderNameFallbackTests(unittest.TestCase):
    """OBL-PROVIDERS-049: the identity shown when a provider cannot explain."""

    def test_a_declared_name_is_used_verbatim(self):
        """The premise: the fallback carries the provider's own name, so a
        class-name result below is a real change of path and not the default."""
        self.assertEqual(
            explain_provider_diff(NamedProvider(), None, None, _context()),
            "custom.named boundary changed",
        )

    def test_a_missing_or_raising_name_degrades_to_the_class_name(self):
        for label, (factory, expected) in CLASS_NAME_FALLBACKS.items():
            with self.subTest(provider=label):
                self.assertEqual(
                    explain_provider_diff(factory(), None, None, _context()),
                    expected,
                )

    def test_neither_degraded_case_yields_none_an_empty_name_or_an_exception(self):
        """The prohibition the obligation states, asserted as a prohibition."""
        for label, (factory, _expected) in CLASS_NAME_FALLBACKS.items():
            with self.subTest(provider=label):
                value = explain_provider_diff(factory(), None, None, _context())
                self.assertNotEqual(value, "None boundary changed")
                self.assertNotEqual(value, " boundary changed")
                self.assertTrue(value.strip())
                self.assertNotIn("None", value)

    def test_a_declared_none_or_empty_name_degrades_to_the_class_name(self):
        """A post-registration mutation cannot inject an invalid label."""
        self.assertEqual(
            explain_provider_diff(NoneName(), None, None, _context()),
            "NoneName boundary changed",
        )
        self.assertEqual(
            explain_provider_diff(EmptyName(), None, None, _context()),
            "EmptyName boundary changed",
        )

    def test_a_name_descriptor_raising_baseexception_degrades_safely(self):
        self.assertEqual(
            explain_provider_diff(SystemExitName(), None, None, _context()),
            "SystemExitName boundary changed",
        )


class ProviderExplanationBoundsTests(unittest.TestCase):
    """OBL-PROVIDERS-050: the returned string must be bounded and must not raise."""

    def test_an_oversized_plain_explanation_is_replaced_by_the_fallback(self):
        """The premise: the byte ceiling is live on this path, so a later
        assertion that a string got through is about a bypass, not about a
        ceiling that was never applied."""
        self.assertEqual(
            explain_provider_diff(BoundedExplainer(), None, None, _context()),
            "custom.bounded boundary changed",
        )
        self.assertEqual(
            explain_provider_diff(ShortExplainer(), None, None, _context()),
            "one field was added",
        )

    def test_a_raising_explain_hook_is_folded_into_the_returned_string(self):
        """The reporting path a caught hook takes, established here so the CLI
        tests below can assert an absence against it.

        The guard around the hook does not re-raise and does not
        write anywhere: it appends the message to the fallback and returns it.
        The only way that text reaches an operator is as the function's value,
        which the caller renders as ``provider_detail`` on stdout.
        """
        self.assertEqual(
            explain_provider_diff(RaisingExplainer(), None, None, _context()),
            RAISING_PROVIDER_DETAIL,
        )

    def test_a_mutated_name_still_yields_a_bounded_string_and_never_raises(self):
        """Every hostile post-registration name remains bounded and inert."""
        offenders = []
        for label, factory in MUTATED_NAME_PROVIDERS.items():
            try:
                value = explain_provider_diff(factory(), None, None, _context())
            except BaseException:  # noqa: BLE001 - the obligation forbids all of them
                offenders.append(label)
                continue
            if len(str(value).encode("utf-8", errors="replace")) > (
                MAX_PROVIDER_ERROR_BYTES
            ):
                offenders.append(label)
        self.assertEqual(offenders, [])

    def test_a_ten_megabyte_name_is_truncated_before_the_fallback(self):
        value = explain_provider_diff(HugeName(), None, None, _context())
        self.assertLessEqual(len(value.encode("utf-8")), MAX_PROVIDER_ERROR_BYTES)
        self.assertTrue(value.endswith(" boundary changed"))

    def test_a_non_string_name_object_degrades_to_the_class_name(self):
        value = explain_provider_diff(MegabyteStrName(), None, None, _context())
        self.assertEqual(value, "MegabyteStrName boundary changed")

    def test_a_name_whose_format_raises_degrades_to_the_class_name(self):
        self.assertEqual(
            explain_provider_diff(UnformattableName(), None, None, _context()),
            "UnformattableName boundary changed",
        )

    def test_a_hostile_str_subclass_is_replaced_by_the_fallback(self):
        value = explain_provider_diff(LyingExplainer(), None, None, _context())
        self.assertIs(type(value), str)
        self.assertEqual(value, "custom.lying boundary changed")


# ---------------------------------------------------------------------------
# `boundver why` with a custom provider
# ---------------------------------------------------------------------------

#: Written to a temporary directory and put on PYTHONPATH, so the CLI
#: subprocess can import it without a file ever entering the repository.
PROVIDER_MODULE_NAME = "chunk07_hostile_providers"

PROVIDER_MODULE_SOURCE = '''
"""Custom providers for tests/test_chunk_07_git_source.py."""

from boundver.providers import ResolvedBoundary


class _Base:
    version = "1"

    def resolve(self, ctx):
        entries = []
        for path in ctx.boundary_cfg.get("paths", []):
            entries.append((path, ctx.read_file(f"{ctx.component_path}/{path}")))
        return ResolvedBoundary(entries=entries)


class QuietProvider(_Base):
    name = "custom.quiet"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return "the contract file changed"


class RaisingProvider(_Base):
    """The caught case, and the premise for every "the hook never ran"
    assertion further down: this message does reach the operator."""

    name = "custom.raising"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        raise RuntimeError("the raising explain hook ran")


class SystemExitProvider(_Base):
    name = "custom.systemexit"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        raise SystemExit(0)


class KeyboardInterruptProvider(_Base):
    name = "custom.keyboardinterrupt"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        raise KeyboardInterrupt


class GeneratorExitProvider(_Base):
    name = "custom.generatorexit"

    def explain_diff(self, old_metadata, new_metadata, ctx):
        raise GeneratorExit


class InstancePoisonProvider(_Base):
    """Rebinds its own instance attribute during resolve, as the RFC threat
    model describes."""

    name = "custom.instancepoison"

    def resolve(self, ctx):
        resolved = _Base.resolve(self, ctx)
        self.name = "P" * (2 * 1024 * 1024)
        return resolved

    def explain_diff(self, old_metadata, new_metadata, ctx):
        return f"explained by an instance carrying {len(self.name)} name bytes"


class ClassPoisonProvider(_Base):
    """Rebinds the class attribute, which a fresh instance would inherit."""

    name = "custom.classpoison"

    def resolve(self, ctx):
        resolved = _Base.resolve(self, ctx)
        ClassPoisonProvider.name = "Q" * (2 * 1024 * 1024)
        return resolved

    def explain_diff(self, old_metadata, new_metadata, ctx):
        raise RuntimeError("the class-poisoned explain hook ran")
'''


class _WhyRun(NamedTuple):
    """One drifted component asked to explain itself, in both renderings."""

    text: subprocess.CompletedProcess
    document: subprocess.CompletedProcess

    def json(self) -> dict:
        return json.loads(self.document.stdout)


#: provider name in the config -> class in the fixture module.
HOSTILE_PROVIDERS = {
    "quiet": ("custom.quiet", "QuietProvider"),
    "raising": ("custom.raising", "RaisingProvider"),
    "systemexit": ("custom.systemexit", "SystemExitProvider"),
    "keyboardinterrupt": ("custom.keyboardinterrupt", "KeyboardInterruptProvider"),
    "generatorexit": ("custom.generatorexit", "GeneratorExitProvider"),
    "instancepoison": ("custom.instancepoison", "InstancePoisonProvider"),
    "classpoison": ("custom.classpoison", "ClassPoisonProvider"),
}

#: Every hostile scenario must retain the already-established drift report.
REPORTING_SCENARIOS = tuple(HOSTILE_PROVIDERS)

#: The two BaseException subclasses exercised together. ``SystemExit`` has a
#: separate assertion because an exit code of zero is the critical case.
OTHER_BASE_EXCEPTIONS = ("keyboardinterrupt", "generatorexit")


class CustomProviderWhyTests(unittest.TestCase):
    """OBL-PROVIDERS-050 and OBL-PROVIDERS-053, through the command a user runs.

    Every scenario costs a repository plus three CLI invocations, so the runs
    happen once for the class and the tests read the recorded output. One of
    them, ``control``, is deliberately *not* drifted: it is the discriminator
    for the SystemExit pin, where the whole finding is an exit code of 0.
    """

    runs: Dict[str, _WhyRun] = {}
    control: Optional[_WhyRun] = None

    @classmethod
    def setUpClass(cls):
        cls._module_directory = tempfile.TemporaryDirectory()
        module_root = Path(cls._module_directory.name)
        (module_root / f"{PROVIDER_MODULE_NAME}.py").write_text(
            PROVIDER_MODULE_SOURCE, encoding="utf-8"
        )
        cls.runs = {
            label: cls._why(module_root, provider_name, class_name)
            for label, (provider_name, class_name) in HOSTILE_PROVIDERS.items()
        }
        cls.control = cls._why(
            module_root, *HOSTILE_PROVIDERS["systemexit"], drift=False
        )

    @classmethod
    def tearDownClass(cls):
        cls.runs = {}
        cls.control = None
        cls._module_directory.cleanup()

    @staticmethod
    def _why(
        module_root: Path,
        provider_name: str,
        class_name: str,
        *,
        drift: bool = True,
    ) -> _WhyRun:
        """Lock a component, drift it, then ask why - twice, one per rendering.

        With ``drift=False`` the second commit is skipped, which produces the
        one run in this class where ``explain_provider_diff`` is not reached:
        the hook is guarded by a boundary change.
        """
        inherited = os.environ.get("PYTHONPATH", "")
        path_value = str(module_root) + (
            os.pathsep + inherited if inherited else ""
        )
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider=provider_name,
                boundary=["contract.txt"],
            )
            scene.file("svc/contract.txt", "one\n")
            scene.config["providers"] = [
                {"module": PROVIDER_MODULE_NAME, "class": class_name}
            ]
            scene.commit()
            with mock.patch.dict(
                os.environ, {"PYTHONPATH": path_value}, clear=False
            ):
                generated = run_cli(
                    scene.root,
                    "generate",
                    "--source",
                    "head",
                    "--allow-custom-providers",
                )
                if generated.returncode != 0:
                    raise AssertionError(
                        f"{class_name}: generate failed with "
                        f"{generated.returncode}\n{generated.stderr}"
                    )
                scene.commit("lock")
                if drift:
                    scene.append_line("svc/contract.txt", "two")
                    scene.commit("drift")
                arguments = ("why", "svc", "--source", "head")
                text = run_cli(
                    scene.root, *arguments, "--allow-custom-providers"
                )
                document = run_cli(
                    scene.root,
                    *arguments,
                    "--format",
                    "json",
                    "--allow-custom-providers",
                )
        return _WhyRun(text, document)

    # -- premises -----------------------------------------------------------

    def test_a_returning_explainer_produces_a_drifted_report_and_exit_one(self):
        """The premise for everything below: this is what a drifted `why` with
        a custom provider looks like when the provider behaves."""
        run = self.runs["quiet"]
        self.assertEqual(run.document.returncode, 1)
        self.assertEqual(run.text.returncode, 1)
        payload = run.json()
        self.assertTrue(payload["drifted"])
        self.assertIn("boundary", payload["changes"])
        self.assertEqual(payload["provider_detail"], "the contract file changed")
        self.assertIn(
            "Provider detail: the contract file changed", run.text.stdout
        )

    def test_every_scenario_that_still_reports_reached_the_explain_branch(self):
        """Every hostile explanation scenario retains the drift report.

        ``explain_provider_diff`` is called only when the boundary facet
        changed, so without this an empty ``provider_detail`` would be
        indistinguishable from a branch that was never entered.
        """
        self.assertEqual(
            set(REPORTING_SCENARIOS),
            set(HOSTILE_PROVIDERS),
            "every recorded hostile provider must retain a report",
        )
        for label in REPORTING_SCENARIOS:
            with self.subTest(provider=label):
                payload = self.runs[label].json()
                self.assertIn("boundary", payload["changes"])
                self.assertTrue(payload["drifted"])

    def test_a_raising_explain_hook_reports_its_message_in_provider_detail(self):
        """The third premise, and the one the class-poison absence rests on.

        A hook that raises a caught exception does not abort the run and does
        not write to stderr: its message comes back as
        ``explain_provider_diff``'s value and is rendered as
        ``provider_detail``. Both renderings are asserted here because both
        are where the class-poison test then looks for it and does not find
        it.
        """
        run = self.runs["raising"]
        self.assertEqual(run.document.returncode, 1)
        self.assertEqual(run.json()["provider_detail"], RAISING_PROVIDER_DETAIL)
        self.assertIn(
            f"Provider detail: {RAISING_PROVIDER_DETAIL}", run.text.stdout
        )
        # The other half of the premise: stderr is not a channel this message
        # can use, so an absence asserted there would prove nothing.
        self.assertEqual(run.document.stderr, "")
        self.assertEqual(run.text.stderr, "")

    def test_an_undrifted_component_with_the_same_provider_still_reports(self):
        """The fourth premise: what exit 0 looks like when nothing is wrong.

        ``SystemExitProvider`` again, on a repository whose drift commit was
        left out. The hook is never called - it is guarded by a boundary
        change - and ``why`` exits 0 after printing a full report that says
        ``drifted`` false. So exit 0 on its own is not a finding, and the
        silent run pinned below differs from a clean one in its stdout.
        """
        self.assertEqual(self.control.document.returncode, 0)
        self.assertEqual(self.control.text.returncode, 0)
        payload = json.loads(self.control.document.stdout)
        self.assertEqual(payload["component"], "svc")
        self.assertFalse(payload["drifted"])
        self.assertEqual(payload["changes"], {})
        self.assertIn(
            "Status: UP TO DATE", self.control.text.stdout
        )

    # -- OBL-PROVIDERS-053 --------------------------------------------------

    def test_a_baseexception_in_explain_diff_leaves_the_verdict_unchanged(self):
        """A non-essential explanation cannot suppress an established drift."""
        baseline = self.runs["quiet"].document
        divergent = []
        for label in ("systemexit", "keyboardinterrupt", "generatorexit"):
            run = self.runs[label].document
            observed = (run.returncode, bool(run.stdout.strip()))
            if observed != (baseline.returncode, True):
                divergent.append(label)
        self.assertEqual(divergent, [])

    def test_system_exit_zero_is_rendered_as_an_unavailable_explanation(self):
        run = self.runs["systemexit"]
        self.assertEqual(run.document.returncode, 1)
        self.assertEqual(run.text.returncode, 1)
        self.assertTrue(run.document.stdout.strip())
        self.assertTrue(run.text.stdout.strip())
        self.assertEqual(run.document.stderr, "")
        self.assertEqual(run.text.stderr, "")
        self.assertIn("provider explanation unavailable", run.document.stdout)

    def test_the_other_two_base_exceptions_are_also_bounded_details(self):
        for label in OTHER_BASE_EXCEPTIONS:
            with self.subTest(provider=label):
                run = self.runs[label]
                self.assertEqual(run.document.returncode, 1)
                self.assertEqual(run.text.returncode, 1)
                self.assertTrue(run.document.stdout.strip())
                self.assertTrue(run.text.stdout.strip())
                self.assertEqual(run.document.stderr, "")
                self.assertEqual(run.text.stderr, "")
                self.assertIn("provider explanation unavailable", run.document.stdout)

    # -- OBL-PROVIDERS-050, through the CLI ---------------------------------

    def test_a_post_registration_name_poison_never_reaches_provider_detail(self):
        """Post-registration poisoning stays bounded or is rejected.

        ``analyze_component_drift`` re-creates the registry and re-loads the
        custom provider before explaining, so the instance that poisoned
        ``self.name`` during ``resolve`` is thrown away, and a poison written
        onto the class is rejected by the 256-byte identity check that runs
        again at load time. In the class case ``load_custom_providers`` returns
        an error, the caller's ``provider is not None and not load_errors``
        short-circuits, and the hook is never called at all - which is why its
        body raises. The raise is not what would fail the run: as the
        ``raising`` premise shows, a hook that raises still finishes the run
        and reports its message in ``provider_detail`` on stdout. So the
        absence of that message from stdout, in both renderings, is the
        evidence that the hook did not fire.
        """
        for label in ("instancepoison", "classpoison"):
            with self.subTest(provider=label):
                run = self.runs[label]
                self.assertEqual(run.document.returncode, 1)
                detail = run.json()["provider_detail"]
                self.assertLessEqual(
                    len(detail.encode("utf-8")), MAX_PROVIDER_ERROR_BYTES
                )
        # 21 is len("custom.instancepoison"), the class-level name. The two
        # megabytes the previous instance wrote over it are simply not there.
        self.assertEqual(
            self.runs["instancepoison"].json()["provider_detail"],
            "explained by an instance carrying 21 name bytes",
        )
        self.assertEqual(self.runs["classpoison"].json()["provider_detail"], "")
        poisoned = self.runs["classpoison"]
        for rendering in ("document", "text"):
            with self.subTest(rendering=rendering):
                completed = getattr(poisoned, rendering)
                # A report was printed, so this is an absence inside output
                # that exists rather than an absence of output.
                self.assertTrue(completed.stdout.strip())
                self.assertNotIn(
                    "the class-poisoned explain hook ran", completed.stdout
                )
                # And nothing of the caught-exception shape at all, which is
                # what the two-megabyte class name would have arrived in.
                self.assertNotIn(
                    "boundary changed (provider explanation", completed.stdout
                )
                self.assertEqual(completed.stderr, "")
        # The text rendering prints "Provider detail:" only for a non-empty
        # explanation, which the `raising` premise shows it does print.
        self.assertNotIn("Provider detail", poisoned.text.stdout)


# ---------------------------------------------------------------------------
# _git.py environment hardening
# ---------------------------------------------------------------------------


def _config_pairs(environment: Dict[str, str]) -> Tuple[dict, dict]:
    """Split a child environment into ``{index: key}`` and ``{index: value}``.

    Reading the mapping back this way is the whole point: the invariant is a
    statement about what Git will see, not about how the dict was built.
    """
    keys, values = {}, {}
    for name, value in environment.items():
        if name.startswith("GIT_CONFIG_KEY_"):
            keys[int(name[len("GIT_CONFIG_KEY_") :])] = value
        elif name.startswith("GIT_CONFIG_VALUE_"):
            values[int(name[len("GIT_CONFIG_VALUE_") :])] = value
    return keys, values


#: Settings ``_ambient_worktree_config_overrides`` is allowed to promote. The
#: property samples from these rather than inventing keys, so a generated shape
#: is one the real query could have produced. That is a claim about ``_git``,
#: so ``test_the_promotable_table_is_the_allowlist_git_enforces`` checks it
#: against the allowlist instead of leaving it asserted only here.
PROMOTABLE_WORKTREE_SETTINGS = (
    ("core.autocrlf", "false"),
    ("core.eol", "lf"),
    ("core.ignorecase", "true"),
    ("core.precomposeunicode", "true"),
    ("core.safecrlf", "warn"),
    ("core.symlinks", "false"),
)

ENVIRONMENT_PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class GitConfigIndexInvariantTests(unittest.TestCase):
    """OBL-GIT-SOURCE-036: the process-local config run must stay gap-free."""

    def setUp(self):
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        (self.root / "nested").mkdir()

    def tearDown(self):
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()
        self._directory.cleanup()

    def _assert_run_is_well_formed(self, environment, expected_pairs, resolved):
        count = int(environment["GIT_CONFIG_COUNT"])
        keys, values = _config_pairs(environment)
        self.assertEqual(sorted(keys), list(range(count)), "key indices")
        self.assertEqual(sorted(values), list(range(count)), "value indices")
        self.assertEqual(count, expected_pairs)
        self.assertIn(
            ("safe.directory", resolved),
            [(keys[index], values[index]) for index in range(count)],
        )

    def test_the_promotable_table_is_the_allowlist_git_enforces(self):
        """The property patches ``_ambient_worktree_config_overrides`` away, so
        the settings it promotes come from this file's table and the real
        allowlist is never touched. Without this the table would be a claim
        about ``_git`` that nothing checks: an eighth promotable setting, or a
        dropped ``core.symlinks``, would leave every other test here green.
        The sampled values are checked too, since the query promotes a setting
        only when its value is one the allowlist names.
        """
        allowlist = git._SAFE_AMBIENT_WORKTREE_CONFIG_VALUES
        self.assertEqual(
            {key for key, _value in PROMOTABLE_WORKTREE_SETTINGS}, set(allowlist)
        )
        for key, value in PROMOTABLE_WORKTREE_SETTINGS:
            with self.subTest(setting=key):
                self.assertIn(value, allowlist[key])

    def test_a_root_less_environment_numbers_exactly_the_five_fixed_settings(self):
        """The base shape, including the absence the register found unasserted:
        no ``GIT_CONFIG_KEY_5`` when there is no repository to name."""
        environment = git._git_subprocess_env(None)
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "5")
        keys, values = _config_pairs(environment)
        self.assertEqual(sorted(keys), [0, 1, 2, 3, 4])
        self.assertEqual(sorted(values), [0, 1, 2, 3, 4])
        self.assertNotIn("GIT_CONFIG_KEY_5", environment)
        self.assertNotIn("GIT_CONFIG_VALUE_5", environment)

    def test_an_inherited_config_index_is_present_until_it_is_stripped(self):
        """The premise for the property's stray-index clause: an ambient
        ``GIT_CONFIG_KEY_99`` really is in the environment boundver copies."""
        poison = {"GIT_CONFIG_KEY_99": "core.editor", "GIT_CONFIG_VALUE_99": "vi"}
        with mock.patch.dict(os.environ, poison, clear=False):
            self.assertEqual(os.environ.copy()["GIT_CONFIG_KEY_99"], "core.editor")
            environment = git._git_subprocess_env(None)
        self.assertNotIn("GIT_CONFIG_KEY_99", environment)
        self.assertNotIn("GIT_CONFIG_VALUE_99", environment)

    @ENVIRONMENT_PROFILE
    @given(
        driver_count=st.integers(min_value=0, max_value=24),
        promoted=st.lists(
            st.sampled_from(PROMOTABLE_WORKTREE_SETTINGS),
            unique_by=lambda pair: pair[0],
            max_size=len(PROMOTABLE_WORKTREE_SETTINGS),
        ),
        stray=st.lists(st.integers(min_value=0, max_value=300), unique=True,
                       max_size=6),
        nested=st.booleans(),
    )
    def test_any_number_of_overrides_leaves_a_gap_free_numbered_run(
        self, driver_count, promoted, stray, nested
    ):
        """The count, the pairing, the run and ``safe.directory``, re-derived
        from the produced mapping for an arbitrary override population."""
        drivers = tuple(
            (f"filter.d{index}.{suffix}", "false" if suffix == "required" else "")
            for index in range(driver_count)
            for suffix in ("clean", "smudge", "process", "required")
        )
        repo_root = self.root / "nested" if nested else self.root
        resolved = str(repo_root.resolve(strict=False))
        poison = {}
        for index in stray:
            poison[f"GIT_CONFIG_KEY_{index}"] = f"poisoned.key.{index}"
            poison[f"GIT_CONFIG_VALUE_{index}"] = f"poisoned-value-{index}"
        with mock.patch.dict(os.environ, poison, clear=False):
            for index in stray:
                self.assertEqual(
                    os.environ[f"GIT_CONFIG_KEY_{index}"], f"poisoned.key.{index}"
                )
            with mock.patch.object(
                git,
                "_ambient_worktree_config_overrides",
                lambda _root, promoted=tuple(promoted): promoted,
            ), mock.patch.object(
                git,
                "_repository_filter_config_overrides",
                lambda _root, drivers=drivers: drivers,
            ):
                environment = git._git_subprocess_env(repo_root)

        self._assert_run_is_well_formed(
            environment,
            5 + 1 + len(promoted) + len(drivers),
            resolved,
        )
        count = int(environment["GIT_CONFIG_COUNT"])
        keys, values = _config_pairs(environment)
        pairs = {(keys[index], values[index]) for index in range(count)}
        for key, value in drivers:
            self.assertIn((key, value), pairs)
        for key, value in promoted:
            self.assertIn((key, value), pairs)
        for index in stray:
            self.assertNotIn(f"poisoned.key.{index}", keys.values())
            self.assertNotIn(f"poisoned-value-{index}", values.values())

    def test_a_real_repository_with_declared_drivers_produces_the_same_run(self):
        """The property patches the two suppliers, so this is the check that
        the shape it generates is the shape a real repository produces."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            scene.git("config", "filter.alpha.clean", "cat")
            scene.git("config", "filter.beta.smudge", "cat")
            git._ambient_worktree_config_overrides.cache_clear()
            git._repository_filter_config_overrides.cache_clear()
            resolved = str(scene.root.resolve())
            promoted = git._ambient_worktree_config_overrides(resolved)
            drivers = git._repository_filter_config_overrides(resolved)
            environment = git._git_subprocess_env(scene.root)
            self.assertEqual(len(drivers), 8)
            self._assert_run_is_well_formed(
                environment, 5 + 1 + len(promoted) + len(drivers), resolved
            )
            count = int(environment["GIT_CONFIG_COUNT"])
            keys, values = _config_pairs(environment)
            pairs = {(keys[index], values[index]) for index in range(count)}
            self.assertIn(("filter.alpha.clean", ""), pairs)
            self.assertIn(("filter.beta.required", "false"), pairs)


#: Ambient names planted in ``os.environ``. Each GIT_ name would redirect the
#: snapshot to the decoy repository or to a foreign object store; SSH_ASKPASS
#: is the only non-prefixed name the strip loop knows, and SSH_AUTH_SOCK is
#: here to fail if the loop ever starts discarding the whole environment.
AMBIENT_NAMES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "SSH_ASKPASS",
)

#: The three names boundver sets itself after stripping, with the value the
#: child must see. Presence alone proves nothing for these: an inherited
#: GIT_CONFIG_COUNT is also present, so the value is the assertion.
REPLACED_CONFIG_NAMES = {
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": os.devnull,
}


class AmbientGitEnvironmentTests(unittest.TestCase):
    """OBL-GIT-SOURCE-037: nothing inherited survives into either environment."""

    def setUp(self):
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()
        self.scene = Scenario()
        self.scene.component("svc", path="svc", provider="leaf")
        self.scene.file("svc/main.py", "x\n")
        self.scene.commit()
        self.decoy = Scenario("decoy")
        self.decoy.component("other", path="other", provider="leaf")
        self.decoy.file("other/main.py", "y\n")
        self.decoy.commit()
        self.poison = {
            "GIT_DIR": str(self.decoy.root / ".git"),
            "GIT_WORK_TREE": str(self.decoy.root),
            "GIT_INDEX_FILE": str(self.decoy.root / ".git" / "index"),
            "GIT_OBJECT_DIRECTORY": str(self.decoy.root / ".git" / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                self.decoy.root / ".git" / "objects"
            ),
            "GIT_CONFIG_COUNT": "1",
            # A key boundver never sets, so slot 0 carrying its own
            # `core.hooksPath` is evidence of replacement rather than a
            # coincidence between the poison and the hardening.
            "GIT_CONFIG_KEY_0": "core.sshCommand",
            "GIT_CONFIG_VALUE_0": str(self.decoy.root / "ssh.sh"),
            "SSH_ASKPASS": str(self.decoy.root / "askpass.sh"),
            "SSH_AUTH_SOCK": str(self.decoy.root / "agent.sock"),
        }

    def tearDown(self):
        git._ambient_worktree_config_overrides.cache_clear()
        git._repository_filter_config_overrides.cache_clear()
        self.decoy.close()
        self.scene.close()

    def _environments(self):
        with mock.patch.dict(os.environ, self.poison, clear=False):
            return (
                git._git_subprocess_env(self.scene.root),
                git._git_config_query_environment(),
            )

    def test_the_planted_environment_really_redirects_an_unhardened_git(self):
        """The premise. Without it every absence below could be an absence of
        effect rather than an absence of the value."""
        with mock.patch.dict(os.environ, self.poison, clear=False):
            naive = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.scene.root,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
        self.assertEqual(naive.returncode, 0, naive.stderr)
        self.assertEqual(
            Path(naive.stdout.strip()).resolve(), self.decoy.root.resolve()
        )

    def test_no_inherited_value_survives_into_either_child_environment(self):
        subprocess_env, query_env = self._environments()
        for name in AMBIENT_NAMES:
            with self.subTest(variable=name):
                planted = self.poison[name]
                self.assertNotIn(name, query_env)
                self.assertNotEqual(subprocess_env.get(name), planted)

    def test_the_names_boundver_does_not_reset_are_removed_outright(self):
        """Everything except the three config slots boundver rewrites itself."""
        subprocess_env, _query = self._environments()
        for name in AMBIENT_NAMES:
            if name in REPLACED_CONFIG_NAMES or name == "GIT_CONFIG_COUNT":
                continue
            with self.subTest(variable=name):
                self.assertNotIn(name, subprocess_env)

    def test_the_config_slots_boundver_reuses_carry_its_own_values(self):
        subprocess_env, _query = self._environments()
        for name, expected in REPLACED_CONFIG_NAMES.items():
            with self.subTest(variable=name):
                self.assertEqual(subprocess_env[name], expected)
        # The inherited count said 1. What the child sees is boundver's own
        # arithmetic, whose size depends on how much the host's global config
        # has to be promoted, so it is derived rather than written down.
        resolved = str(self.scene.root.resolve())
        expected_count = (
            5
            + 1
            + len(git._ambient_worktree_config_overrides(resolved))
            + len(git._repository_filter_config_overrides(resolved))
        )
        self.assertEqual(
            subprocess_env["GIT_CONFIG_COUNT"], str(expected_count)
        )
        self.assertGreaterEqual(expected_count, 6)

    def test_a_name_outside_the_strip_list_is_left_alone(self):
        """SSH_AUTH_SOCK is not GIT_-prefixed and is not in the frozenset, so
        it must survive. Deleting the frozenset would leave SSH_ASKPASS here
        beside it, and deleting the loop would take this one with it."""
        subprocess_env, query_env = self._environments()
        planted = self.poison["SSH_AUTH_SOCK"]
        self.assertEqual(subprocess_env["SSH_AUTH_SOCK"], planted)
        self.assertEqual(query_env["SSH_AUTH_SOCK"], planted)
        self.assertNotIn("SSH_ASKPASS", subprocess_env)
        self.assertNotIn("SSH_ASKPASS", query_env)

    def test_the_hardened_environment_still_resolves_the_nearest_marker(self):
        subprocess_env, _query = self._environments()
        hardened = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.scene.root,
            capture_output=True,
            text=True,
            env=subprocess_env,
        )
        self.assertEqual(hardened.returncode, 0, hardened.stderr)
        self.assertEqual(
            Path(hardened.stdout.strip()).resolve(), self.scene.root.resolve()
        )

    def test_git_root_finds_the_marker_under_an_inherited_git_dir(self):
        """The assertion the register found was only ever made against
        config-based redirection: the same guarantee with GIT_DIR and
        GIT_WORK_TREE naming a different repository in ``os.environ``."""
        previous = Path.cwd()
        try:
            os.chdir(self.scene.root)
            with mock.patch.dict(os.environ, self.poison, clear=False):
                git._ambient_worktree_config_overrides.cache_clear()
                git._repository_filter_config_overrides.cache_clear()
                found = git.git_root()
        finally:
            os.chdir(previous)
            git._ambient_worktree_config_overrides.cache_clear()
            git._repository_filter_config_overrides.cache_clear()
        self.assertEqual(found, self.scene.root.resolve())


# ---------------------------------------------------------------------------
# CRLF/LF equivalence
# ---------------------------------------------------------------------------

#: ``compat`` is absent from both members of every pair below, so comparing it
#: would assert None == None. These three are the digests the obligation names.
COMPARED_FACETS = ("exact", "boundary", "behavior")

#: One line of file content: anything but a control character, which keeps
#: CR, LF and NUL out of the body so the transform is the only difference.
CONTENT_LINE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=12
)

HASHING_PROFILE = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


class LineEndingEquivalenceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-063: CRLF and LF are one file, NUL and a lone CR are not."""

    def _facets(self, content: bytes) -> Dict[str, Dict[str, str]]:
        """Commit *content* as the only boundary file and read every digest.

        The blob and working-tree assertions are the premise for the whole
        class: ``Scenario`` pins ``core.autocrlf=false`` and writes ``* -text``,
        so Git stores what was written. Without checking that, an equality
        below could be Git's own end-of-line conversion agreeing with itself
        rather than boundver's normalization doing anything.
        """
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", provider="path-hash", boundary=["c.txt"]
            )
            scene.config["components"]["svc"]["behavior"] = {"paths": ["c.txt"]}
            target = scene.root / "svc" / "c.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            scene.commit()
            self.assertEqual(scene.blob("svc/c.txt"), content, "stored blob")
            self.assertEqual(target.read_bytes(), content, "working tree")
            return {
                mode: dict(
                    scene.generate(source=mode)["components"]["svc"]["fingerprints"]
                )
                for mode in SOURCE_MODES
            }

    def _assert_agree(self, left, right, agree: bool, subject: str):
        """No ``subTest`` here: Hypothesis disables its reporting inside a
        ``@given`` body, so the failing pair is named in the message instead.

        Both sides are checked for a digest before either comparison. In the
        ``agree=False`` branch that is the difference between an inequality
        and a tautology: ``assertNotEqual(digest, None)`` would pass for the
        wrong reason if the right-hand fixture ever stopped producing one.
        """
        for mode in SOURCE_MODES:
            for facet in COMPARED_FACETS:
                where = f"{subject}: {facet} under --source {mode}"
                self.assertIsNotNone(left[mode][facet], where)
                self.assertIsNotNone(right[mode][facet], where)
                if agree:
                    self.assertEqual(left[mode][facet], right[mode][facet], where)
                else:
                    self.assertNotEqual(
                        left[mode][facet], right[mode][facet], where
                    )

    @HASHING_PROFILE
    @given(lines=st.lists(CONTENT_LINE, min_size=1, max_size=5))
    def test_a_crlf_file_and_its_lf_twin_share_every_facet_digest(self, lines):
        body = LF.join(line.encode("utf-8") for line in lines) + LF
        twin = body.replace(LF, CR + LF)
        self.assertNotEqual(body, twin, "the transform must change the bytes")
        self._assert_agree(
            self._facets(twin), self._facets(body), True, "CRLF against LF"
        )

    def test_one_file_produces_the_same_digest_in_every_source_mode(self):
        """The other half of the obligation's source-mode clause: a clean tree
        gives one answer, so the equality above is not three coincidences."""
        facets = self._facets(b"openapi: 3.1.0" + CR + LF + b"info: {}" + CR + LF)
        for facet in COMPARED_FACETS:
            with self.subTest(facet=facet):
                self.assertEqual(
                    len({facets[mode][facet] for mode in SOURCE_MODES}), 1
                )

    def test_a_nul_bearing_pair_is_not_equivalent(self):
        lf_body = b"a" + NUL + b"b" + LF + b"c" + LF
        crlf_body = lf_body.replace(LF, CR + LF)
        self._assert_agree(
            self._facets(crlf_body),
            self._facets(lf_body),
            False,
            "NUL-bearing content",
        )

    def test_a_lone_carriage_return_is_never_rewritten(self):
        """A CR with no LF after it must reach the digest as a CR, so the file
        must not hash like the same text with the CR turned into a newline."""
        lone = b"first" + CR + b"second" + LF
        converted = b"first" + LF + b"second" + LF
        self._assert_agree(
            self._facets(lone), self._facets(converted), False, "a bare CR"
        )

    def test_a_bare_cr_survives_beside_a_folded_crlf_in_one_file(self):
        """Both rules at once, since ``\\r\\n`` and a lone ``\\r`` in the same
        body is where a naive ``replace(b"\\r", b"")`` would show up."""
        mixed = b"one" + CR + b"two" + CR + LF + b"three" + LF
        folded = b"one" + CR + b"two" + LF + b"three" + LF
        left, right = self._facets(mixed), self._facets(folded)
        for facet in COMPARED_FACETS:
            with self.subTest(facet=facet):
                # Same reason as in ``_assert_agree``: two missing digests
                # would satisfy the equality without meaning anything.
                self.assertIsNotNone(left["head"][facet])
                self.assertIsNotNone(right["head"][facet])
                self.assertEqual(left["head"][facet], right["head"][facet])


if __name__ == "__main__":
    unittest.main()
