"""Five promises about what an embedder sees, checked against the whole export
surface rather than against one function and one input.

Every obligation answered here quantifies over the functions boundver publishes
for a library caller, and every one of them is answered today by a test that
picks a member and an input and checks that pair. The members such a test
leaves out are the interesting ones. `boundver.__all__` holds fourteen names
and only four of them are the API these obligations describe: the rest are a
CLI entry point that exits by design, two analysis helpers that report failure
by returning a sentinel and printing to stderr, five exception classes and one
enum. A checker that read `__all__` and applied "nothing but a `BoundverError`
may escape" to all fourteen would fail on `main` for the wrong reason and pass
vacuously on three more, so enumerating the surface is only half the work; the
other half is deciding what each member promises without writing that decision
down as a list of names.

The rule used here is `__module__`. `inspect.isclass` separates the types from
the callables, and the module a function reports separates the ones defined in
`src/boundver/__init__.py` - which is exactly `generate`, `verify`, `diff` and
`load_config`, the four every one of these obligations names by hand - from the
ones re-exported out of `cli`, `_output` and `providers`. Each partition gets
one discipline, `DISCIPLINE` is asserted to have a key for every partition that
exists at runtime, and a function exported tomorrow either lands in a partition
that already has a discipline and is driven by the sweep automatically, or
lands in one that does not and turns this file red. Driving is enumerated the
same way: one pool keyed by parameter *name* covers the whole surface, and the
binder refuses to call a function whose required parameters the pool does not
name, so a new export with a new knob fails loudly instead of being skipped.

Four of the five obligations are about something the API must *not* do, which
is the shape that produces confident green over code that never ran. Four
premises guard against that. The pool is captured from a healthy repository
*before* the fault is applied, because a pool rebuilt afterwards feeds
`config=None` to the analysis helpers and manufactures `AttributeError`s that
are the harness's bug and not boundver's. Every fault must be shown to have
changed an outcome, because a fault that changes nothing - patching
`subprocess.run` when the git layer drives `subprocess.Popen`, or truncating
`.git/index` while reading only the committed tree - yields a clean sweep over
an event that never happened. Every function under the strict exception
discipline must be observed both succeeding and failing, because a corpus that
never reaches a function's failure path proves nothing about how that path
fails. And every row records how far into the git file-listing layer its own
call got, counted while the row ran, because the corpus's reach is partial and
saying so is the difference between "boundver did this much work without
printing" and "boundver printed nothing while failing early". As the corpus
stands, thirty of the one hundred and seventy-six API rows get there, all on
the working-tree axis, and six of the fifteen faults never do.

Stdout and stderr are measured at two layers, `sys.stdout` and file descriptor
1, because they are not interchangeable: `print` lands on the first and leaves
the second empty, `os.write(1, ...)` does the reverse, and the one warning
OBL-GIT-SOURCE-141 is about is a `print`, so an fd-only capture would report
silence and prove nothing. Reaching that warning needs a repository that stops
being a repository between `git_root()` answering and the next git command
running, which is the daemon-and-hook scenario the obligation's rationale
describes; it is produced here by subclassing `subprocess.Popen` and patching
no boundver symbol at all, so every line of boundver that runs is the real one.

Covers OBL-GIT-SOURCE-140, OBL-GIT-SOURCE-141, OBL-GIT-SOURCE-142,
OBL-LOCKFILE-062 and OBL-PROVIDERS-060.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import boundver
from boundver import _git, _hashing, _lockfile, _utils
from boundver._config import MAX_PROVIDER_DECLARATIONS

from tests._parity import run_cli
from tests._scenarios import SOURCE_MODES, Scenario

CONFIG = "boundary.config.json"
LOCK = "boundary.lock.json"

#: The warning OBL-GIT-SOURCE-141 names, verbatim from src/boundver/_git.py.
FALLBACK_WARNING = (
    "WARNING: git file listing failed; falling back to filesystem enumeration. "
    "Fingerprints may differ from git-based computation."
)


class UnclassifiedExport(AssertionError):
    """An exported name none of the partition rules can place."""


# ---------------------------------------------------------------------------
# The surface, read at runtime and partitioned by a rule
# ---------------------------------------------------------------------------


def exported_surface() -> List[str]:
    """The authority. `__all__` is a plain list literal at __init__.py:5-20 and
    the module defines no `__getattr__`, so this is the whole published set."""
    return list(boundver.__all__)


def partition_key(name: str) -> str:
    """Which partition an exported name belongs to, derived from the object.

    Nothing here is a name test. `isclass` splits the types from the callables,
    `issubclass(BaseException)` splits the exceptions from the enum, and
    `__module__` says whether a function was defined in the package's own
    `__init__.py` or re-exported from a submodule. That last distinction is the
    load-bearing one: the four functions reporting `boundver` are the four
    every obligation in this file names, and the members that report something
    else are the members that cannot obey the same rules.
    """
    obj = getattr(boundver, name)
    if inspect.isclass(obj):
        return "exception" if issubclass(obj, BaseException) else "type"
    if inspect.isfunction(obj):
        return "function:" + obj.__module__
    raise UnclassifiedExport(f"{name!r} is neither a class nor a function: {obj!r}")


def partitioned() -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = {}
    for name in exported_surface():
        buckets.setdefault(partition_key(name), []).append(name)
    return {key: sorted(names) for key, names in buckets.items()}


#: What each partition promises. Keyed by partition, never by member name, so a
#: fifth function added to `__init__.py` inherits the strict discipline and is
#: driven by the sweep without a row here.
#:
#: It is not thereby free of every list in the file, and saying otherwise would
#: overstate what this keying buys. `test_the_module_rule_selects_the_functions
#: _the_obligations_name` pins each partition's membership as an exact list, so
#: a fifth API function is driven under the right discipline automatically and
#: still has to be named there. That pin is deliberate rather than an
#: oversight: "the strict properties apply to exactly the four functions the
#: obligations name by hand" is a claim about those four names, and it needs
#: them written down once to be checkable at all. What the keying removes is a
#: per-member *discipline* decision, not the roster.
#:
#: This is the module's residue. Removing it would need the package to declare
#: each export's category itself - a marker attribute, or a documented table in
#: the package metadata - and there is none, so the four disciplines are
#: written here and `test_every_partition_of_the_surface_has_a_discipline`
#: keeps the mapping total. Each value is what the sweep asserts:
#:
#:   raises-boundver-error  every escape is a BoundverError, nothing reaches
#:                          stdout, no SystemExit, and a config failure is a
#:                          ConfigError
#:   cli-entry              exits the process by design; excluded from the
#:                          three properties above and driven on its own argv
#:                          axis by CommandLineEntryPointTests
#:   returns-error-sentinel never raises; reports failure through the return
#:                          value and, for one of the two, through stderr
#:   total                  takes no repository input, so no fault can reach it
#:   exception-class        not called; the classes the disciplines refer to
#:   value-type             not called; the SourceMode enum
DISCIPLINE = {
    "function:boundver": "raises-boundver-error",
    "function:boundver.cli": "cli-entry",
    "function:boundver._output": "returns-error-sentinel",
    "function:boundver.providers": "total",
    "exception": "exception-class",
    "type": "value-type",
}

#: The partitions the fault sweep drives. `cli-entry` is absent because `main`
#: reads `sys.argv` rather than its parameters, so a parameter sweep would run
#: it under one invocation no matter which fault was applied.
DRIVEN_PARTITIONS = (
    "function:boundver",
    "function:boundver._output",
    "function:boundver.providers",
)

#: The partition the strict exception, stdout and ConfigError properties apply
#: to - the four functions defined in `src/boundver/__init__.py`.
API_PARTITION = "function:boundver"


def names_in(*partitions: str) -> List[str]:
    buckets = partitioned()
    return sorted(name for key in partitions for name in buckets.get(key, []))


def api_functions() -> List[str]:
    return names_in(API_PARTITION)


def driven_functions() -> List[str]:
    return names_in(*DRIVEN_PARTITIONS)


def bind(function, pool: Dict[str, Any]) -> Dict[str, Any]:
    """Bind *function*'s parameters out of one shared pool, by name.

    The pool is keyed by parameter name rather than by function, so one table
    drives the whole surface. A required parameter the pool does not name is an
    error rather than a skip: that is what makes a newly exported function with
    a new knob fail this file instead of quietly dropping out of the sweep.
    """
    kwargs: Dict[str, Any] = {}
    unnamed: List[str] = []
    for name, parameter in inspect.signature(function).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            unnamed.append(name + " (varargs)")
        elif name in pool:
            kwargs[name] = pool[name]
        elif parameter.default is inspect.Parameter.empty:
            unnamed.append(name)
    if unnamed:
        raise AssertionError(
            f"the argument pool does not name required parameters of "
            f"{function.__name__}: {unnamed}; extend POOL_KEYS"
        )
    return kwargs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def clear_git_caches() -> None:
    """Both `lru_cache`s `_git` installs, each keyed by the resolved repo root.

    A fixture that reused a path would otherwise inherit the previous
    repository's answers, so these are cleared on the way in and on the way out
    of every repository this module enters.
    """
    _git._ambient_worktree_config_overrides.cache_clear()
    _git._repository_filter_config_overrides.cache_clear()


def list_files_binding_sites() -> List[str]:
    """Every module holding the git file-listing function.

    Package modules import `_list_files_for_source` at import time
    and hold the original object, so patching `_git` alone patches a binding
    nobody calls. The sites are enumerated rather than listed, because a
    seventh importer tomorrow would silently drop out of the count below.

    This lives with the fixtures rather than with the stdout obligation
    because `sweep` uses it: every row records how far into this layer its
    call actually got, which is what makes "the corpus never printed" a
    statement about work that happened.
    """
    target = _git._list_files_for_source
    return sorted(
        name
        for name, module in list(sys.modules.items())
        if name.startswith("boundver")
        and getattr(module, "_list_files_for_source", None) is target
    )


class InRepo:
    """Enter a repository. `git_root()` reads `Path.cwd()`, so the process
    working directory is an input to every call here and has to be restored."""

    def __init__(self, path) -> None:
        self.path = str(path)

    def __enter__(self) -> "InRepo":
        self.previous = os.getcwd()
        os.chdir(self.path)
        clear_git_caches()
        return self

    def __exit__(self, *exc: object) -> bool:
        os.chdir(self.previous)
        clear_git_caches()
        return False


def build_repository(scene: Scenario, *, components: int = 1, with_lock: bool = True):
    """A committed repository with a generated, committed lockfile.

    Behavior covers every boundary artifact because `validate_config` rejects a
    component whose behavior paths leave one uncovered, and no slice is
    declared because `generate` passes `require_slice_facets=True` and a
    declared slice without facets fails before any of this reaches the code
    under test.
    """
    for index in range(components):
        name = f"svc{index}"
        scene.component(
            name,
            path=f"services/{name}",
            provider="path-hash",
            boundary=["api/*.yaml"],
            behavior=["api/*.yaml", "impl/*.py"],
        )
        scene.file(f"services/{name}/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file(f"services/{name}/impl/run.py", "VALUE = 1\n")
    scene.defaults(verify_facets=["boundary"])
    scene.commit()
    if not with_lock:
        return None
    with InRepo(scene.root):
        lockfile = boundver.generate(source="head", out_path=LOCK)
    scene.git("add", "--all")
    scene.git("commit", "-m", "lock")
    return lockfile


def drift(scene: Scenario, components: int = 1) -> None:
    """Move a behavior-only file, so the advisory facets differ from the lock
    while the gated facet does not."""
    for index in range(components):
        scene.file(f"services/svc{index}/impl/run.py", "VALUE = 2\n")
    scene.commit("drift")


class Captured:
    """One call's outcome, with both stream layers kept apart."""

    def __init__(self) -> None:
        self.fd_out = ""
        self.fd_err = ""
        self.sys_out = ""
        self.sys_err = ""
        self.exception: Optional[BaseException] = None
        self.result: Any = None

    @property
    def stdout(self) -> str:
        return self.fd_out + self.sys_out

    @property
    def stderr(self) -> str:
        return self.fd_err + self.sys_err

    @property
    def exception_name(self) -> str:
        if self.exception is None:
            return "-"
        cls = type(self.exception)
        return f"{cls.__module__}.{cls.__name__}"

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return (
            f"Captured(exception={self.exception_name}, "
            f"stdout={self.stdout!r}, stderr={self.stderr!r})"
        )


def call_capturing(function, **kwargs) -> Captured:
    """Run one call under both stream layers at once.

    `redirect_stdout` only rebinds `sys.stdout`, so a write straight to file
    descriptor 1 slips past it, and a `dup2` alone misses a write to a stream
    object bound before it ran. `BaseException` rather than `Exception`,
    because `SystemExit` is one of the things this module has to observe.
    """
    captured = Captured()
    sys_out, sys_err = io.StringIO(), io.StringIO()
    fd_out, fd_err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    saved_out, saved_err = os.dup(1), os.dup(2)
    try:
        os.dup2(fd_out.fileno(), 1)
        os.dup2(fd_err.fileno(), 2)
        try:
            with contextlib.redirect_stdout(sys_out), contextlib.redirect_stderr(sys_err):
                try:
                    captured.result = function(**kwargs)
                except BaseException as raised:  # noqa: BLE001 - the taxonomy is the point
                    captured.exception = raised
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
    finally:
        os.close(saved_out)
        os.close(saved_err)
    fd_out.seek(0)
    fd_err.seek(0)
    captured.fd_out = fd_out.read().decode("utf-8", "replace")
    captured.fd_err = fd_err.read().decode("utf-8", "replace")
    fd_out.close()
    fd_err.close()
    captured.sys_out = sys_out.getvalue()
    captured.sys_err = sys_err.getvalue()
    return captured


# ---------------------------------------------------------------------------
# The fault corpus
# ---------------------------------------------------------------------------


def _commit(scene: Scenario, message: str) -> None:
    scene.git("add", "--all")
    scene.git("commit", "--allow-empty", "-m", message)


def _lock_uncommitted(scene: Scenario) -> None:
    """The trap OBL-GIT-SOURCE-140's rationale names: a lock that exists on
    disk but has never been committed, which `verify(source="head")` cannot
    see."""
    (scene.root / LOCK).unlink()
    scene.git("rm", "--cached", LOCK)
    _commit(scene, "drop the lock")


def _config_garbage(scene: Scenario) -> None:
    (scene.root / CONFIG).write_bytes(b"{ not json")
    _commit(scene, "garbage config")


def _lock_garbage(scene: Scenario) -> None:
    (scene.root / LOCK).write_bytes(b"{ not json")
    _commit(scene, "garbage lock")


def _lock_wrong_shape(scene: Scenario) -> None:
    (scene.root / LOCK).write_bytes(b'{"hello": "world"}')
    _commit(scene, "wrong-shape lock")


def _dangling_head(scene: Scenario) -> None:
    (scene.root / ".git" / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")


def _boundary_deleted(scene: Scenario) -> None:
    (scene.root / "services" / "svc0" / "api" / "v1.yaml").unlink()
    _commit(scene, "delete the boundary artifact")


def _truncated_index(scene: Scenario) -> None:
    (scene.root / ".git" / "index").write_bytes(b"DIRCnonsense")


def _over_limit_config(scene: Scenario) -> None:
    """The input OBL-PROVIDERS-060 names: more declarations than
    `MAX_PROVIDER_DECLARATIONS` allows."""
    config = json.loads((scene.root / CONFIG).read_text(encoding="utf-8"))
    config["components"]["svc0"]["boundary"]["paths"] = [
        "p%06d.txt" % index for index in range(MAX_PROVIDER_DECLARATIONS + 1)
    ]
    (scene.root / CONFIG).write_text(json.dumps(config) + "\n", encoding="utf-8")


def _no_git_binary():
    """Make git unavailable the way `_git` actually looks for it.

    Patching `subprocess.run` is inert here - the git layer drives git through
    `subprocess.Popen` - so the injection has to be at `shutil.which`, which is
    what `_git` consults before spawning anything.
    """
    real_which = shutil.which

    def which(command, *args, **kwargs):
        if command == "git":
            return None
        return real_which(command, *args, **kwargs)

    return mock.patch.object(_git.shutil, "which", which)


def _hash_guardrail():
    """The guardrail trip. The real ceiling is 50000 files, which no fixture
    reaches, so the clause is vacuous unless the constant is lowered."""
    return mock.patch.object(_hashing, "MAX_HASH_FILES", 0)


class Case:
    """One member of the corpus: a repository fault, an argument overlay, or
    both, together with the source modes it should be driven under."""

    def __init__(
        self,
        name: str,
        *,
        mutate=None,
        patch=None,
        overrides: Optional[Dict[str, Any]] = None,
        sources: Tuple[str, ...] = SOURCE_MODES,
    ) -> None:
        self.name = name
        self.mutate = mutate
        self.patch = patch
        self.overrides = dict(overrides or {})
        self.sources = sources

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return f"Case({self.name!r})"


CASES: Tuple[Case, ...] = (
    Case("healthy"),
    Case("lock-uncommitted", mutate=_lock_uncommitted),
    Case("config-garbage", mutate=_config_garbage),
    Case("lock-garbage", mutate=_lock_garbage),
    Case("lock-wrong-shape", mutate=_lock_wrong_shape),
    Case("dangling-head", mutate=_dangling_head),
    Case("boundary-deleted", mutate=_boundary_deleted),
    Case("truncated-index", mutate=_truncated_index),
    Case("no-git-binary", patch=_no_git_binary),
    Case("hash-guardrail", patch=_hash_guardrail),
    Case("over-limit-config", mutate=_over_limit_config, sources=("working-tree",)),
    Case("absent-lock", overrides={
        "lock_path": "absent.json",
        "old_path": "absent-old.json",
        "new_path": "absent-new.json",
    }),
    Case("absent-config", overrides={"config_path": "absent.json"}),
    Case("unknown-component", overrides={"component_name": "no-such-component"}),
    Case("parent-traversal", overrides={
        "lock_path": "../escape.json",
        "config_path": "../escape.json",
    }),
    Case("unknown-source-mode", overrides={"source": "not-a-source"}, sources=("head",)),
)

#: Every parameter name the pool must cover for the binder to reach the whole
#: driven surface. Written down so the union check has something to compare
#: against; the values are built per case in `pool_for`.
POOL_KEYS = (
    "allow_custom_providers",
    "base_ref",
    "component_name",
    "components",
    "config",
    "config_path",
    "diagnostic_base_ref",
    "facets",
    "fail_fast",
    "lock_path",
    "lockfile",
    "new_path",
    "observations",
    "old_path",
    "out_path",
    "repo_root",
    "snapshot",
    "source",
    "transitive_consumers",
)


def pool_for(scene: Scenario, config: dict, lockfile: dict) -> Dict[str, Any]:
    """One value per parameter name, all captured from a healthy repository.

    Captured before the fault, not after: a pool rebuilt afterwards binds
    `config=None` and `lockfile=None` whenever `load_config` itself failed, and
    the analysis helpers then raise `AttributeError` on a value their own
    signatures do not admit. That is the harness failing, not boundver, and it
    is the single largest source of false leaks in this shape of sweep.

    `out_path=None` keeps the sweep from writing a lockfile.
    """
    return {
        "config_path": CONFIG,
        "lock_path": LOCK,
        "out_path": None,
        "old_path": str(scene.root / LOCK),
        "new_path": str(scene.root / LOCK),
        "source": "head",
        "allow_custom_providers": False,
        "components": None,
        "facets": None,
        "observations": None,
        "fail_fast": False,
        "transitive_consumers": False,
        "config": config,
        "lockfile": lockfile,
        "repo_root": scene.root,
        "component_name": "svc0",
        "snapshot": None,
        "diagnostic_base_ref": None,
        "base_ref": None,
    }


class Row:
    """One observation: one function, under one case, at one source mode.

    `source` is the axis the sweep drove, which is what the healthy baseline is
    keyed by; `argument` is what the function was actually handed, and the two
    differ only for the case whose overlay poisons the source argument itself.

    `reach` is how many times this one call entered `_list_files_for_source`,
    the layer the stdout obligation's warning lives in. It is recorded per row
    rather than reconstructed later because "no API call printed" over a corpus
    of calls that all failed before reaching a writer is a claim about nothing,
    and the only way to tell the two apart is to count while the row runs.
    """

    def __init__(
        self,
        case: str,
        source: str,
        argument: str,
        function: str,
        captured: Captured,
        reach: int = 0,
    ) -> None:
        self.case = case
        self.source = source
        self.argument = argument
        self.function = function
        self.captured = captured
        self.reach = reach

    @property
    def exception(self) -> Optional[BaseException]:
        return self.captured.exception

    @property
    def exception_name(self) -> str:
        return self.captured.exception_name

    @property
    def raised(self) -> bool:
        return self.captured.exception is not None

    @property
    def is_boundver_error(self) -> bool:
        return isinstance(self.captured.exception, boundver.BoundverError)

    @property
    def outcome(self) -> Tuple[str, bool, bool]:
        """What "this fault changed something" is measured on."""
        return (
            self.exception_name,
            bool(self.captured.stdout),
            bool(self.captured.stderr),
        )

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return (
            f"Row(case={self.case!r}, source={self.source!r}, "
            f"argument={self.argument!r}, function={self.function!r}, "
            f"reach={self.reach}, {self.captured!r})"
        )


_SWEEP: Optional[List[Row]] = None

#: The binding sites the sweep actually instrumented, recorded so the reach
#: premise can check that the count came from every module holding the function
#: rather than from whichever one happened to be patched.
_SWEPT_BINDING_SITES: List[str] = []


def sweep() -> List[Row]:
    """Drive every driven export under every case, at every source mode.

    Computed once for the whole module: five test classes read it and building
    it costs one Git repository per case.

    The whole sweep runs with a counting wrapper installed at every module
    that holds `_list_files_for_source`, and each row records how many times
    its own call entered it. The wrapper delegates to the original, so nothing
    about the run changes; what it buys is that the reach of *this corpus* is
    measured rather than argued for from a separate repository built to make
    the point.
    """
    global _SWEEP
    if _SWEEP is not None:
        return _SWEEP
    rows: List[Row] = []
    functions = driven_functions()
    target = _git._list_files_for_source
    calls: List[Any] = []

    def counting(*args, **kwargs):
        calls.append(args)
        return target(*args, **kwargs)

    sites = list_files_binding_sites()
    _SWEPT_BINDING_SITES[:] = sites
    with contextlib.ExitStack() as instrumented:
        for site in sites:
            instrumented.enter_context(
                mock.patch.object(sys.modules[site], "_list_files_for_source", counting)
            )
        for case in CASES:
            with Scenario("embeddable") as scene:
                build_repository(scene)
                config = json.loads((scene.root / CONFIG).read_text(encoding="utf-8"))
                lockfile = json.loads((scene.root / LOCK).read_text(encoding="utf-8"))
                base_pool = pool_for(scene, config, lockfile)
                if case.mutate is not None:
                    case.mutate(scene)
                patcher = (
                    case.patch() if case.patch is not None else contextlib.nullcontext()
                )
                with InRepo(scene.root), patcher:
                    for source in case.sources:
                        pool = dict(base_pool)
                        pool["source"] = source
                        pool.update(case.overrides)
                        for name in functions:
                            function = getattr(boundver, name)
                            kwargs = bind(function, pool)
                            del calls[:]
                            captured = call_capturing(function, **kwargs)
                            rows.append(
                                Row(case.name, source, pool["source"], name,
                                    captured, len(calls))
                            )
    _SWEEP = rows
    return rows


def rows_for(names) -> List[Row]:
    wanted = set(names)
    return [row for row in sweep() if row.function in wanted]


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


class ExportedSurfaceTests(unittest.TestCase):
    """The enumeration, and the two ways it is made to fail loudly."""

    def test_the_surface_is_read_from_the_runtime_authority(self):
        """`__all__` is the whole published set, and nothing shadows it."""
        names = exported_surface()
        self.assertEqual(len(names), len(set(names)), names)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(hasattr(boundver, name), name)
        # Nothing public reaches an importer that `__all__` does not list,
        # except the typing and importlib re-imports and the submodules - none
        # of which is boundver API.
        extra = sorted(
            name
            for name in dir(boundver)
            if not name.startswith("_") and name not in names
        )
        self.assertEqual(
            extra,
            ["List", "Optional", "PackageNotFoundError", "cli", "core",
             "providers", "version", "versions"],
        )

    def test_every_partition_of_the_surface_has_a_discipline(self):
        """The enumeration bite. A member the rules cannot place, or a
        partition nobody has decided about, fails here rather than dropping out
        of the sweep."""
        buckets = partitioned()
        covered = sorted(name for names in buckets.values() for name in names)
        self.assertEqual(covered, sorted(exported_surface()))
        self.assertEqual(sorted(buckets), sorted(DISCIPLINE))
        for key, names in sorted(buckets.items()):
            with self.subTest(partition=key):
                self.assertTrue(names, f"partition {key!r} is empty")

    def test_the_module_rule_selects_the_functions_the_obligations_name(self):
        """The premise for applying the strict discipline to a derived set.

        Four obligations name `load_config`, `generate`, `verify` and `diff` by
        hand. Nothing below repeats those names: the strict properties are
        applied to whatever `__module__ == "boundver"` selects, and this is the
        one place the derivation is checked against the obligations' own words.
        """
        self.assertEqual(
            api_functions(), ["diff", "generate", "load_config", "verify"]
        )
        for name in api_functions():
            with self.subTest(name=name):
                self.assertEqual(getattr(boundver, name).__module__, "boundver")
        self.assertEqual(names_in("function:boundver.cli"), ["main"])
        self.assertEqual(
            names_in("function:boundver._output"),
            ["analyze_component_drift", "analyze_explain_changes"],
        )
        self.assertEqual(names_in("function:boundver.providers"), ["create_registry"])
        self.assertEqual(
            names_in("exception"),
            ["BoundverError", "ConfigError", "GuardrailError", "LockfileError",
             "ProviderError"],
        )
        self.assertEqual(names_in("type"), ["SourceMode"])

    def test_the_exception_set_is_enumerable_rather_than_listed(self):
        """"A BoundverError subclass" is a claim about the exported classes, so
        it is read off them rather than written down."""
        exceptions = [getattr(boundver, name) for name in names_in("exception")]
        self.assertTrue(exceptions)
        for cls in exceptions:
            with self.subTest(exception=cls.__name__):
                self.assertTrue(issubclass(cls, boundver.BoundverError))
        # The asymmetry OBL-PROVIDERS-060 turns on: the documented handler does
        # not catch three of its four siblings.
        for name in ("GuardrailError", "LockfileError", "ProviderError"):
            with self.subTest(exception=name):
                self.assertFalse(
                    issubclass(getattr(boundver, name), boundver.ConfigError)
                )

    def test_a_newly_exported_function_is_not_silently_skipped(self):
        """The control for the enumeration: appending a name really does grow
        the driven set and really does break the coverage assertion.

        Without this the partition could be complete because the code that
        computes it is broken rather than because the surface is stable.
        """
        def audit_repository(new_knob):  # pragma: no cover - never called
            return new_knob

        with mock.patch.object(boundver, "audit_repository", audit_repository,
                               create=True):
            with mock.patch.object(
                boundver, "__all__", list(boundver.__all__) + ["audit_repository"]
            ):
                # It is not in any driven partition, because its module is this
                # test module rather than one of boundver's - so it fails the
                # coverage assertion instead of being driven under a discipline
                # nobody chose for it.
                self.assertEqual(partition_key("audit_repository"), "function:" + __name__)
                self.assertNotIn("function:" + __name__, DISCIPLINE)
                with self.assertRaises(AssertionError):
                    self.assertEqual(sorted(partitioned()), sorted(DISCIPLINE))
                # And the binder refuses its unrecognised required parameter
                # rather than calling it with a default.
                with self.assertRaises(AssertionError) as raised:
                    bind(audit_repository, {})
                self.assertIn("new_knob", str(raised.exception))
        # And the surface is back to what it was.
        self.assertEqual(sorted(partitioned()), sorted(DISCIPLINE))

    def test_one_pool_covers_every_parameter_of_every_driven_export(self):
        """The second enumeration bite, in its own right: the binder's table
        has to name every parameter the surface actually has."""
        union = sorted(
            {
                parameter
                for name in driven_functions()
                for parameter in inspect.signature(getattr(boundver, name)).parameters
            }
        )
        self.assertEqual(union, sorted(POOL_KEYS))
        with Scenario("pool") as scene:
            lockfile = build_repository(scene)
            config = json.loads((scene.root / CONFIG).read_text(encoding="utf-8"))
            pool = pool_for(scene, config, lockfile)
        self.assertEqual(sorted(pool), sorted(POOL_KEYS))
        for name in driven_functions():
            with self.subTest(function=name):
                bind(getattr(boundver, name), pool)


# ---------------------------------------------------------------------------
# The sweep's own premises
# ---------------------------------------------------------------------------


class SweepPremiseTests(unittest.TestCase):
    """Nothing below this point means anything unless the sweep ran, the faults
    bit, and each function was driven into its failure path."""

    @classmethod
    def setUpClass(cls):
        cls.rows = sweep()

    def test_the_sweep_covers_the_whole_driven_surface(self):
        self.assertEqual(
            {row.function for row in self.rows}, set(driven_functions())
        )
        self.assertEqual({row.case for row in self.rows}, {case.name for case in CASES})
        self.assertEqual(
            {row.source for row in self.rows if row.case == "healthy"},
            set(SOURCE_MODES),
        )
        expected = sum(
            len(case.sources) * len(driven_functions()) for case in CASES
        )
        self.assertEqual(len(self.rows), expected)
        # The one overlay that replaces the source argument itself is driven
        # under the head axis and handed something the axis does not name.
        self.assertEqual(
            {row.argument for row in self.rows if row.case == "unknown-source-mode"},
            {"not-a-source"},
        )

    def test_every_fault_changed_an_outcome(self):
        """A fault whose row matches the healthy row is a broken fault, not a
        passing check.

        This caught two inert injections while this module was being built:
        patching `subprocess.run` to make git unavailable, which the git layer
        never consults, and truncating `.git/index` while only reading `head`.
        """
        healthy = {
            (row.source, row.function): row.outcome
            for row in self.rows
            if row.case == "healthy"
        }
        for case in CASES:
            if case.name == "healthy":
                continue
            changed = sorted(
                {
                    row.function
                    for row in self.rows
                    if row.case == case.name
                    and row.outcome != healthy[(row.source, row.function)]
                }
            )
            with self.subTest(case=case.name):
                self.assertTrue(changed, f"fault {case.name!r} changed nothing")

    def test_the_healthy_case_really_is_healthy(self):
        """The other half: if every row raised, "no leak of the wrong type"
        would be a statement about a repository nothing could read."""
        for row in self.rows:
            if row.case != "healthy":
                continue
            with self.subTest(source=row.source, function=row.function):
                self.assertIsNone(row.exception, row)
        healthy_generate = [
            row for row in self.rows if row.case == "healthy" and row.function == "generate"
        ]
        self.assertTrue(healthy_generate)
        for row in healthy_generate:
            with self.subTest(source=row.source):
                self.assertIn("svc0", row.captured.result["components"])

    def test_each_strictly_disciplined_function_was_driven_into_failure(self):
        """The reachability witness. A clean sweep over a function the corpus
        never made fail is a clean sweep over nothing."""
        for name in api_functions():
            failures = [row for row in rows_for([name]) if row.raised]
            successes = [row for row in rows_for([name]) if not row.raised]
            with self.subTest(function=name):
                self.assertTrue(failures, f"{name} never failed in the corpus")
                self.assertTrue(successes, f"{name} never succeeded in the corpus")

    def test_the_capture_apparatus_sees_each_stream_layer_separately(self):
        """The positive control for every silence assertion in this file.

        `print` reaches only the `sys` layer and `os.write` only the file
        descriptor, so a capture that watched one of them would report silence
        for half the writers in the package.
        """
        seen = call_capturing(lambda: print("to stdout"))
        self.assertEqual(seen.sys_out, "to stdout\n")
        self.assertEqual(seen.fd_out, "")
        seen = call_capturing(lambda: os.write(1, b"to fd 1"))
        self.assertEqual(seen.fd_out, "to fd 1")
        self.assertEqual(seen.sys_out, "")
        seen = call_capturing(lambda: print("to stderr", file=sys.stderr))
        self.assertEqual(seen.sys_err, "to stderr\n")
        self.assertEqual(seen.fd_err, "")
        seen = call_capturing(lambda: os.write(2, b"to fd 2"))
        self.assertEqual(seen.fd_err, "to fd 2")
        self.assertEqual(seen.sys_err, "")

    def test_the_capture_apparatus_reports_an_exception_rather_than_raising(self):
        """The premise for every taxonomy row: a raise has to become data."""
        def explode():
            raise RuntimeError("boom")

        seen = call_capturing(explode)
        self.assertEqual(seen.exception_name, "builtins.RuntimeError")
        self.assertFalse(isinstance(seen.exception, boundver.BoundverError))

        def leave():
            raise SystemExit(7)

        seen = call_capturing(leave)
        self.assertEqual(seen.exception_name, "builtins.SystemExit")
        self.assertEqual(seen.exception.code, 7)


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-140: the exception taxonomy at the boundary
# ---------------------------------------------------------------------------


def raise_lines(function, exception_name: str) -> List[int]:
    """Where *function* raises *exception_name*, read from its own source.

    Line numbers move, so they are derived rather than written down; what the
    scope pin below asserts is that the leak arrives from one of these lines,
    not that the line is 176.
    """
    source_file = Path(inspect.getsourcefile(function))
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function.__name__
    )
    lines = []
    for node in ast.walk(target):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == exception_name:
                lines.append(node.lineno)
    return sorted(lines)


def escapes(handler, function, **kwargs) -> str:
    """Run *function* inside `except handler` and report what came out.

    `"caught"` when the clause held it, the dotted class name when something
    else got past, `"-no-raise-"` when the call returned. This is the
    embedder's own construct rather than an `isinstance` check, and the
    difference matters where the rows under test were themselves selected by
    an `isinstance`: such a set cannot be asked what a handler does to it
    without answering with the filter that built it.
    """
    try:
        function(**kwargs)
    except handler:
        return "caught"
    except BaseException as raised:  # noqa: BLE001 - the taxonomy is the point
        return f"{type(raised).__module__}.{type(raised).__name__}"
    return "-no-raise-"


class ExceptionTaxonomyTests(unittest.TestCase):
    """OBL-GIT-SOURCE-140: a repository-controlled failure is a BoundverError."""

    @classmethod
    def setUpClass(cls):
        cls.rows = rows_for(api_functions())
        cls.leaks = [row for row in cls.rows if row.raised and not row.is_boundver_error]

    def test_no_repository_controlled_failure_escapes_as_a_foreign_exception(self):
        """Every public API failure uses the exported exception taxonomy."""
        for row in self.rows:
            with self.subTest(case=row.case, source=row.source, function=row.function):
                if row.raised:
                    self.assertTrue(row.is_boundver_error, row)

    def test_no_foreign_exception_remains_in_the_fault_corpus(self):
        self.assertEqual(self.leaks, [])
        for row in self.rows:
            if not row.raised:
                continue
            with self.subTest(case=row.case, source=row.source, function=row.function):
                self.assertNotIsInstance(row.exception, OSError)
                self.assertNotIsInstance(row.exception, subprocess.CalledProcessError)

    def test_missing_lock_failures_preserve_the_low_level_cause(self):
        normalized = [
            row
            for row in self.rows
            if isinstance(row.exception, boundver.LockfileError)
            and isinstance(row.exception.__cause__, FileNotFoundError)
        ]
        self.assertTrue(normalized)
        self.assertEqual({row.function for row in normalized}, {"diff", "verify"})
        sites = raise_lines(_lockfile.load_lockfile_file, "FileNotFoundError")
        self.assertEqual(len(sites), 2, sites)
        observed = set()
        for row in normalized:
            frames = row.exception.__cause__.__traceback__
            innermost = None
            while frames is not None:
                innermost = frames
                frames = frames.tb_next
            with self.subTest(case=row.case, source=row.source, function=row.function):
                self.assertEqual(
                    Path(innermost.tb_frame.f_code.co_filename).name, "_lockfile.py"
                )
                self.assertEqual(
                    innermost.tb_frame.f_code.co_name, "load_lockfile_file"
                )
                self.assertIn(innermost.tb_lineno, sites)
            observed.add(innermost.tb_lineno)
        # Both sites remain covered: the snapshot-backed read and the disk read.
        self.assertEqual(sorted(observed), sites)

    def test_the_documented_handlers_catch_a_missing_lock(self):
        handlers = [getattr(boundver, name) for name in names_in("exception")]
        self.assertEqual(len(handlers), 5)
        with Scenario("uncaught") as scene:
            build_repository(scene, with_lock=False)
            with InRepo(scene.root):
                boundver.generate(source="head", out_path=LOCK)
                for handler in handlers:
                    with self.subTest(handler=handler.__name__):
                        outcome = escapes(handler, boundver.verify, source="head")
                        if handler in {boundver.BoundverError, boundver.LockfileError}:
                            self.assertEqual(outcome, "caught")
                        else:
                            self.assertEqual(outcome, "boundver._utils.LockfileError")
                self.assertEqual(
                    escapes(ValueError, boundver.verify, source="head"), "caught"
                )
                self.assertEqual(
                    escapes(OSError, boundver.verify, source="head"),
                    "boundver._utils.LockfileError",
                )
                caught_control = sorted(
                    handler.__name__
                    for handler in handlers
                    if escapes(
                        handler, boundver.verify, source="head",
                        config_path="absent.json",
                    ) == "caught"
                )
                self.assertEqual(caught_control, ["BoundverError", "ConfigError"])
                self.assertEqual(
                    escapes(
                        boundver.GuardrailError, boundver.verify, source="head",
                        config_path="absent.json",
                    ),
                    "boundver._utils.ConfigError",
                )

    def test_the_first_call_an_embedder_makes_raises_lockfile_error(self):
        """A generated but uncommitted lock is absent from the default HEAD."""
        with Scenario("first-call") as scene:
            build_repository(scene, with_lock=False)
            with InRepo(scene.root):
                boundver.generate(source="head", out_path=LOCK)
                self.assertEqual(scene.git("status", "--porcelain"), "?? boundary.lock.json")
                with self.assertRaises(boundver.LockfileError) as raised:
                    boundver.verify(source="head")
                self.assertEqual(
                    str(raised.exception),
                    "Lockfile not found in captured head source: boundary.lock.json",
                )
                # The same repository, one argument different, is fine.
                self.assertEqual(boundver.verify(source="working-tree"), [])

    def test_the_command_line_compensates_where_the_api_does_not(self):
        """The scope pin that shows the leak is invisible from a shell, which
        is why no CLI test would have caught it."""
        with Scenario("cli-compensation") as scene:
            build_repository(scene, with_lock=False)
            with InRepo(scene.root):
                boundver.generate(source="head", out_path=LOCK)
            result = run_cli(scene.root, "verify")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn(
                "ERROR: Lockfile not found in captured head source: boundary.lock.json",
                result.stderr,
            )
            result = run_cli(scene.root, "diff", "missing-a.json", "missing-b.json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr, "ERROR: old lockfile not found: missing-a.json\n"
            )

    def test_the_members_outside_the_api_partition_report_failure_differently(self):
        """The divergence between the obligation's scope and the surface.

        Three of the four other exported callables cannot satisfy the sentence
        the obligation states, so they get their own disciplines rather than an
        exemption: two never raise at all, and one takes no repository input.
        """
        for row in rows_for(names_in("function:boundver._output", "function:boundver.providers")):
            with self.subTest(case=row.case, source=row.source, function=row.function):
                self.assertIsNone(row.exception, row)
        registry = rows_for(["create_registry"])
        self.assertTrue(registry)
        for row in registry:
            with self.subTest(case=row.case, source=row.source):
                self.assertEqual(
                    sorted(row.captured.result), sorted(boundver.create_registry())
                )
        for row in rows_for(["analyze_explain_changes"]):
            if row.case != "unknown-component":
                continue
            with self.subTest(source=row.source):
                self.assertEqual(
                    row.captured.result["error"],
                    "unknown component 'no-such-component'",
                )
        for row in rows_for(["analyze_component_drift"]):
            if row.case != "unknown-component":
                continue
            with self.subTest(source=row.source):
                self.assertIsNone(row.captured.result)
                self.assertIn(
                    "ERROR: unknown component 'no-such-component'", row.captured.stderr
                )


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-141: stdout silence, and the one stderr writer
# ---------------------------------------------------------------------------


def vanishing_repository(root: Path):
    """A repository that stops being one between `git_root()` answering and the
    next git command running.

    No boundver symbol is patched: the only hook is `subprocess.Popen`, armed
    by the `--show-toplevel` probe whose answer boundver has already consumed
    by the time the rename happens. Everything that runs afterwards is real
    code against a directory that genuinely is not a Git repository.
    """
    real_popen = subprocess.Popen
    state = {"armed": False, "moved": False}
    hidden = root.parent / (root.name + ".gitaway")

    class Popen(real_popen):
        def __init__(self, command, *args, **kwargs):
            if state["armed"] and not state["moved"]:
                state["moved"] = True
                os.rename(root / ".git", hidden)
            super().__init__(command, *args, **kwargs)
            if "--show-toplevel" in command:
                state["armed"] = True

    return mock.patch.object(subprocess, "Popen", Popen), state, hidden


class InertFixture(RuntimeError):
    """The vanishing-repository hook did not fire, so nothing was observed."""


def observe_under_a_vanishing_repository(source: str) -> Captured:
    """Run `generate` against a repository that stops being one mid-call, and
    refuse to return an observation the hook did not produce.

    Raising rather than asserting is the point, and so is the fact that the
    caller of this function is `setUpClass`. The divergence this feeds is an
    `unittest.expectedFailure`, and an `expectedFailure` absorbs whatever its
    body raises - `AssertionError` and `RuntimeError` alike, which was probed
    rather than assumed. A premise checked inside that body is therefore not
    checked at all: an inert fixture would leave `generate` running against an
    ordinary healthy repository, the silence assertion would fail for the
    wrong reason, and the decorator would report the same green xfail. Raised
    out of `setUpClass` the same exception is an error on every test in the
    class, which is what a broken premise should be.
    """
    with Scenario("vanishing") as scene:
        build_repository(scene)
        patcher, state, hidden = vanishing_repository(scene.root)
        try:
            with InRepo(scene.root), patcher:
                seen = call_capturing(boundver.generate, source=source, out_path=None)
        finally:
            if hidden.exists():
                os.rename(hidden, scene.root / ".git")
    if not state["moved"]:
        raise InertFixture(
            f"the vanishing-repository hook never moved .git (source={source!r}); "
            f"what follows would be an observation of an ordinary repository"
        )
    return seen


def stream_writer_census() -> Dict[str, Tuple[int, int, int]]:
    """Which package modules can write to a stream at all, counted with `ast`.

    `core.py` rebinds `print = _safe_print` at module scope, so a census built
    by patching `builtins.print` would miss most of its writers; reading the
    call sites finds them whatever the name resolves to.
    """
    package = Path(boundver.__file__).resolve().parent
    census: Dict[str, Tuple[int, int, int]] = {}
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        to_stdout = to_stderr = writes = 0
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                stream = next((kw for kw in node.keywords if kw.arg == "file"), None)
                if (
                    stream is not None
                    and isinstance(stream.value, ast.Attribute)
                    and stream.value.attr == "stderr"
                ):
                    to_stderr += 1
                else:
                    to_stdout += 1
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr in ("stdout", "stderr")
            ):
                writes += 1
        if to_stdout or to_stderr or writes:
            census[path.name] = (to_stdout, to_stderr, writes)
    return census


#: The package modules that can write to a stream, and what a library caller is
#: allowed to see from each. `core.py` and `_output.py` are the CLI's rendering
#: layer; `_discovery.py`'s warning is reached only from two CLI handlers.
#: `_git.py`'s single writer is the one this obligation is about.
DECLARED_WRITERS = {"_discovery.py", "_git.py", "_output.py", "core.py"}


class StreamSilenceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-141: nothing on stdout, and one accounted-for stderr line."""

    @classmethod
    def setUpClass(cls):
        cls.rows = rows_for(api_functions())
        cls.reaching = [row for row in cls.rows if row.reach]
        # Taken here, not in a test: the divergence below is an
        # `expectedFailure`, which absorbs an inert fixture as readily as a
        # real divergence. `observe_under_a_vanishing_repository` says why.
        cls.vanished = {
            source: observe_under_a_vanishing_repository(source)
            for source in ("head", "working-tree")
        }

    def test_the_corpus_reaches_the_layer_the_warning_lives_in(self):
        """The premise, measured on the corpus the silence claims quantify
        over rather than on a repository built to make the point.

        `_load_validated_config_inputs` converts a bad repository into a
        `ConfigError` before any file listing runs, so a corpus can consist
        entirely of calls that never got that far, and asserting silence over
        it asserts that boundver does not print while doing nothing. Every row
        therefore carries its own `reach`, counted inside `sweep` at every
        module that holds the function.

        What the count shows is a partial reach, and the partiality is
        asserted rather than summarised away, because "the corpus reaches it"
        and "the corpus reaches it on one of three axes, from three of the
        four functions, under ten of sixteen cases" are different claims and
        only the second one is true.

        The counts are floors; the memberships are exact. A count pinned by
        equality would make a fifth well-behaved API export red here as well
        as in the roster pin, which would cost the module the property it
        exists to have - a new API function is driven by the sweep without a
        row being written for it - and a new export can only push a count up.
        The memberships are a different kind of statement: which axis and
        which cases reach this layer is a fact about boundver's control flow,
        and a change that moved one of them is worth a red.
        """
        sites = list_files_binding_sites()
        self.assertEqual(
            sites,
            [
                "boundver._config",
                "boundver._coverage",
                "boundver._git",
                "boundver._hashing",
                "boundver._lockfile",
                "boundver._migration_analysis",
                "boundver.core",
            ],
        )
        # The sweep instrumented every one of them, so a row's zero is a zero
        # and not an unpatched binding.
        self.assertEqual(_SWEPT_BINDING_SITES, sites)

        self.assertTrue(
            self.reaching, "no row of the corpus reached the git file listing layer"
        )
        # The corpus is what it says it is, and the reach is a proper part of
        # it rather than all of it: silence over 176 rows of which 30 got to
        # the layer is a different claim from silence over 176 rows of which
        # all did, and this file is only entitled to the first.
        self.assertEqual(
            len(self.rows),
            sum(len(case.sources) * len(api_functions()) for case in CASES),
        )
        self.assertGreaterEqual(len(self.reaching), 30)
        self.assertLess(len(self.reaching), len(self.rows))
        # Only the working-tree axis gets there. Head and index go through
        # `_capture_git_source_snapshot`, which calls `ls-tree` directly.
        self.assertEqual({row.source for row in self.reaching}, {"working-tree"})
        # Three of the four API functions reach it. `diff` takes two lockfile
        # paths and no source, so it never enters the layer at all, and that
        # exclusion is the asymmetry worth recording.
        reaching_functions = {row.function for row in self.reaching}
        self.assertNotIn("diff", reaching_functions)
        self.assertGreaterEqual(len(reaching_functions), 3, sorted(reaching_functions))
        self.assertTrue(reaching_functions <= set(api_functions()))
        for name in sorted(reaching_functions):
            with self.subTest(function=name):
                cases = {row.case for row in self.reaching if row.function == name}
                self.assertGreaterEqual(len(cases), 5, sorted(cases))
        # And the residue, named rather than left implicit: six of the sixteen
        # cases fail before any file listing runs, so for those the silence
        # assertions really are about a call that did nothing.
        silent_cases = sorted(
            {case.name for case in CASES}
            - {row.case for row in self.reaching}
        )
        self.assertEqual(
            silent_cases,
            ["absent-config", "config-garbage", "no-git-binary",
             "over-limit-config", "parent-traversal", "unknown-source-mode"],
        )

    def test_the_reach_counter_discriminates_between_the_source_modes(self):
        """The control for the count above: on a repository this test builds
        itself, the same instrumentation reads zero where the code cannot get
        there and non-zero where it can, so a zero in the corpus is a
        measurement rather than a wrapper that never fired."""
        sites = list_files_binding_sites()
        target = _git._list_files_for_source
        observed = {}
        for source in SOURCE_MODES:
            with Scenario("reach") as scene:
                build_repository(scene)
                calls = []

                def counting(*args, **kwargs):
                    calls.append(args)
                    return target(*args, **kwargs)

                with InRepo(scene.root), contextlib.ExitStack() as stack:
                    for site in sites:
                        stack.enter_context(
                            mock.patch.object(
                                sys.modules[site], "_list_files_for_source", counting
                            )
                        )
                    seen = call_capturing(boundver.generate, source=source, out_path=None)
                self.assertIsNone(seen.exception, seen)
                observed[source] = len(calls)
        # Head and index legitimately record nothing: both go through
        # `_capture_git_source_snapshot`, which calls `ls-tree` directly. A
        # check that demanded a hit on all three would be wrong about the code.
        self.assertEqual(observed["head"], 0)
        self.assertEqual(observed["index"], 0)
        self.assertGreater(observed["working-tree"], 0)

    def test_no_api_call_writes_a_byte_to_stdout(self):
        """The obligation's first half, over the whole corpus and both layers."""
        self.assertTrue(self.rows)
        # The silence is silence over work that happened: some of these rows
        # got as far as the layer the one warning lives in.
        self.assertTrue(self.reaching)
        for row in self.rows:
            with self.subTest(case=row.case, source=row.source, function=row.function):
                self.assertEqual(row.captured.fd_out, "", row)
                self.assertEqual(row.captured.sys_out, "", row)

    def test_no_api_call_writes_to_stderr_over_the_ordinary_corpus(self):
        """The stderr half where it holds: nothing in the fault corpus reaches
        a writer. What does reach one is the fixture below."""
        self.assertTrue(self.rows)
        self.assertTrue(self.reaching)
        for row in self.rows:
            with self.subTest(case=row.case, source=row.source, function=row.function):
                self.assertEqual(row.captured.stderr, "", row)

    def test_the_vanishing_repository_fixture_is_not_inert(self):
        """The control. A hook that changed nothing would make the reach test
        below a silent no-op dressed as evidence."""
        with Scenario("inert") as scene:
            build_repository(scene)

            class Inert(subprocess.Popen):
                pass

            with InRepo(scene.root):
                with mock.patch.object(subprocess, "Popen", Inert):
                    seen = call_capturing(
                        boundver.generate, source="working-tree", out_path=None
                    )
        self.assertIsNone(seen.exception, seen)
        self.assertEqual(seen.stdout, "")
        self.assertEqual(seen.stderr, "")

    def test_an_inert_hook_refuses_to_produce_an_observation(self):
        """The second half of that control, and the reason the premise sits in
        `setUpClass`.

        `test_the_vanishing_repository_fixture_is_not_inert` shows that a
        pass-through hook changes nothing about boundver's behaviour. This
        shows what happens to the *evidence* when the hook stops firing: the
        observation is refused rather than returned, and the refusal is an
        exception, which is the only form of failure `setUpClass` will carry
        to a test decorated `expectedFailure`. Asserting the same premise
        inside that decorated body was measured to report a green xfail with a
        hook that never moved `.git`.
        """
        def inert(root):
            state = {"armed": False, "moved": False}
            return contextlib.nullcontext(), state, root.parent / "never-created"

        with mock.patch.object(sys.modules[__name__], "vanishing_repository", inert):
            with self.assertRaises(InertFixture) as raised:
                observe_under_a_vanishing_repository("working-tree")
        self.assertIn("never moved .git", str(raised.exception))
        self.assertIn("source='working-tree'", str(raised.exception))
        # An InertFixture is not an AssertionError, but that is not what saves
        # it: `expectedFailure` absorbs both. What saves it is where it is
        # raised from, which is what the class-level premise arranges.
        self.assertNotIsInstance(raised.exception, AssertionError)

    def test_what_the_reachable_warning_actually_does_to_a_caller(self):
        """The scope pin for the divergence above, and the premise that the
        capture can see this writer at all.

        Working-tree fails open and narrates; head and index fail closed with a
        `ConfigError`. That asymmetry is the finding: the caller who reads the
        return value is handed a filesystem-approximated lockfile while the
        only signal goes to a stream a library caller may not be reading.
        """
        observed = self.vanished
        self.assertEqual(sorted(observed), ["head", "working-tree"])

        head = observed["head"]
        self.assertEqual(head.stdout, "")
        self.assertEqual(head.stderr, "")
        self.assertIsInstance(head.exception, boundver.ConfigError)
        self.assertIn("Cannot capture head source", str(head.exception))

        tree = observed["working-tree"]
        self.assertEqual(tree.stdout, "")
        self.assertIsNone(tree.exception)
        self.assertIn("svc0", tree.result["components"])
        # The count moves with the shape of the config, so what is pinned is
        # the message and the stream, not how many times it appears.
        self.assertGreaterEqual(tree.stderr.count(FALLBACK_WARNING), 1)
        self.assertEqual(tree.sys_err.count(FALLBACK_WARNING), tree.stderr.count(FALLBACK_WARNING))
        self.assertEqual(tree.fd_err, "")
        self.assertEqual(
            {line for line in tree.stderr.splitlines() if line}, {FALLBACK_WARNING}
        )

    def test_the_reachable_warning_is_documented_for_embedders(self):
        """The public API contract discloses its sole low-level stderr warning."""
        needle = "falling back to filesystem enumeration"
        root = Path(boundver.__file__).resolve().parents[2]
        self.assertTrue((root / "src" / "boundver" / "_git.py").exists(), root)
        searched = []
        hits = []
        for path in sorted(root.rglob("*.md")):
            if ".git" in path.parts or "node_modules" in path.parts:
                continue
            searched.append(path)
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(str(path.relative_to(root)))
        # The premise: the search looked at the documentation it claims to
        # have searched, and can find a string that is there.
        self.assertGreater(len(searched), 20)
        self.assertTrue(any(path.name == "reference.md" for path in searched))
        reference = root / "docs" / "reference.md"
        self.assertIn("boundver.ConfigError", reference.read_text(encoding="utf-8"))
        self.assertIn("docs\\reference.md", hits)

    def test_no_second_uncontrolled_writer_appeared(self):
        """The obligation's last clause, enumerated from the package rather
        than from a memory of which modules print."""
        census = stream_writer_census()
        self.assertEqual(set(census), DECLARED_WRITERS)
        self.assertEqual(census["_git.py"], (0, 1, 0))
        self.assertEqual(census["_discovery.py"], (0, 1, 0))
        # The two rendering modules print freely; that is what they are for.
        self.assertGreater(census["core.py"][0], 50)
        self.assertGreater(census["_output.py"][0], 50)

    def test_the_analysis_exports_write_stderr_by_their_own_design(self):
        """The second writer on the exported surface, recorded rather than
        asserted away.

        `analyze_component_drift` is in `__all__` and prints to stderr on its
        failure paths, returning `None` instead of raising - its own docstring
        says so. It is not on the API path this obligation scopes, which is why
        it is a pin here and not a failure, but a reader of the obligation
        should know the surface has a second writer on it.
        """
        drift_rows = [
            row for row in rows_for(["analyze_component_drift"]) if row.captured.stderr
        ]
        self.assertTrue(drift_rows)
        for row in drift_rows:
            with self.subTest(case=row.case, source=row.source):
                self.assertEqual(row.captured.stdout, "")
                self.assertTrue(row.captured.stderr.startswith("ERROR: "))
                self.assertIsNone(row.captured.result)
        self.assertIn(
            "error printed to stderr", boundver.analyze_component_drift.__doc__
        )
        # Its sibling reports the same failures through the return value only.
        for row in rows_for(["analyze_explain_changes"]):
            with self.subTest(case=row.case, source=row.source):
                self.assertEqual(row.captured.stderr, "")


# ---------------------------------------------------------------------------
# OBL-GIT-SOURCE-142: no SystemExit, and a pure import
# ---------------------------------------------------------------------------

#: Run in a fresh interpreter with `-B`, so no bytecode write pollutes the file
#: traffic. It flips recording on immediately before the import, which is what
#: keeps interpreter startup out of the report.
_IMPORT_PROBE = r'''
import io, json, os, sys
SRC = sys.argv[1]
RECORDING = False
EVENTS = []
PROCESS = {"subprocess.Popen", "os.system", "os.exec", "os.spawn",
           "os.posix_spawn", "os.fork", "socket.connect", "socket.getaddrinfo"}

def hook(event, args):
    if not RECORDING:
        return
    if event == "open":
        EVENTS.append(["open", str(args[0]), str(args[1])])
    elif event in PROCESS:
        EVENTS.append([event, repr(args)[:200], ""])

sys.path.insert(0, SRC)
sys.addaudithook(hook)
out, err = io.StringIO(), io.StringIO()
saved = sys.stdout, sys.stderr
sys.stdout, sys.stderr = out, err
RECORDING = True
error = None
try:
    import boundver
except BaseException as exc:
    error = type(exc).__name__ + ": " + str(exc)
RECORDING = False
sys.stdout, sys.stderr = saved
report = {
    "error": error,
    "stdout": out.getvalue(),
    "stderr": err.getvalue(),
    "all": list(getattr(boundver, "__all__", [])) if error is None else [],
    "missing": [n for n in boundver.__all__ if not hasattr(boundver, n)] if error is None else [],
    "package_dir": os.path.dirname(boundver.__file__) if error is None else "",
    "events": EVENTS,
    "submodules": sorted(m for m in sys.modules if m == "boundver" or m.startswith("boundver.")),
}
print(json.dumps(report))
'''

#: Appended to a copy of the package for the negative control. Each line is one
#: of the three things the obligation forbids at import time.
_IMPORT_POISON = (
    "\n"
    "import os as _probe_os, subprocess as _probe_sp\n"
    "_probe_sp.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True)\n"
    "print('boundver loaded')\n"
    "_probe_outside = _probe_os.path.join(\n"
    "    _probe_os.path.dirname(_probe_os.path.dirname(__file__)), 'side-effect.txt')\n"
    "with open(_probe_outside, 'w') as _probe_handle:\n"
    "    _probe_handle.write('x')\n"
)


def import_report(source_directory: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-B", "-c", _IMPORT_PROBE, source_directory],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - harness failure
        raise AssertionError(f"import probe failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def opens_outside(report: dict) -> List[List[str]]:
    """File opens that are neither inside the package nor inside the running
    interpreter's own tree. Numeric paths are file descriptors, not files."""
    package = os.path.normcase(report["package_dir"])
    allowed = (package, os.path.normcase(sys.prefix), os.path.normcase(sys.base_prefix))
    outside = []
    for event in report["events"]:
        if event[0] != "open":
            continue
        path = event[1]
        if path.isdigit():
            continue
        if os.path.normcase(os.path.abspath(path)).startswith(allowed):
            continue
        outside.append(event)
    return outside


class NoSystemExitTests(unittest.TestCase):
    """OBL-GIT-SOURCE-142, first half: the API never exits the process."""

    @classmethod
    def setUpClass(cls):
        cls.rows = rows_for(driven_functions())

    def test_no_driven_export_raised_system_exit_on_any_input(self):
        self.assertTrue(self.rows)
        for row in self.rows:
            with self.subTest(case=row.case, source=row.source, function=row.function):
                self.assertNotIsInstance(row.exception, SystemExit, row)

    def test_the_helpers_the_api_reuses_do_exit_when_the_cli_calls_them(self):
        """The premise the absence needs: `SystemExit` is not merely unused in
        this module's corpus, it is what the same module does one layer over.

        `core.py` is where the API's helpers live, and it exits freely; what
        the obligation asks is that the API's own path never reaches one of
        those. Observing `main` exit through the identical capture is what
        makes the row above evidence rather than an untested claim.
        """
        with Scenario("exit") as scene:
            build_repository(scene, with_lock=False)
            previous_argv = sys.argv[:]
            try:
                with InRepo(scene.root):
                    sys.argv = ["boundver", "verify"]
                    seen = call_capturing(boundver.main)
            finally:
                sys.argv = previous_argv
        self.assertIsInstance(seen.exception, SystemExit)
        self.assertEqual(seen.exception.code, 2)
        self.assertEqual(seen.stdout, "")
        self.assertIn("ERROR: ", seen.stderr)


class CommandLineEntryPointTests(unittest.TestCase):
    """`main` is exported, so it needs a discipline; it cannot have this one.

    The batch asks for the properties above to be quantified over the exported
    surface, and `main` is on that surface. It exits by design and writes to
    stdout by design, so a uniform sweep would report it as two failures that
    are not failures. Its own axis is `sys.argv`, which the parameter binder
    cannot reach, and this is what it does on that axis.
    """

    def _run(self, scene: Scenario, *argv: str) -> Captured:
        previous_argv = sys.argv[:]
        try:
            with InRepo(scene.root):
                sys.argv = ["boundver", *argv]
                return call_capturing(boundver.main)
        finally:
            sys.argv = previous_argv

    def test_main_exits_rather_than_returning(self):
        with Scenario("cli-axis") as scene:
            build_repository(scene, with_lock=False)
            version = self._run(scene, "--version")
            failure = self._run(scene, "verify")
        self.assertIsInstance(version.exception, SystemExit)
        self.assertEqual(version.exception.code, 0)
        self.assertEqual(version.stdout, f"boundver {boundver.__version__}\n")
        self.assertIsInstance(failure.exception, SystemExit)
        self.assertEqual(failure.exception.code, 2)
        self.assertEqual(failure.stdout, "")
        self.assertTrue(failure.stderr)

    def test_main_takes_no_parameters_so_the_sweep_cannot_vary_it(self):
        """Why it is excluded from `DRIVEN_PARTITIONS` rather than special-cased
        inside the sweep."""
        self.assertEqual(list(inspect.signature(boundver.main).parameters), [])
        self.assertNotIn("function:boundver.cli", DRIVEN_PARTITIONS)
        self.assertEqual(DISCIPLINE["function:boundver.cli"], "cli-entry")


class ImportPurityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-142, second half: `import boundver` is inert."""

    @classmethod
    def setUpClass(cls):
        cls.source = str(Path(boundver.__file__).resolve().parents[1])
        cls.report = import_report(cls.source)

    def test_the_import_succeeds_and_publishes_every_name(self):
        self.assertIsNone(self.report["error"])
        self.assertEqual(self.report["all"], exported_surface())
        self.assertEqual(self.report["missing"], [])
        # The obligation's premise: the CLI graph really is imported eagerly,
        # so this is a claim about a wide import and not a narrow one.
        self.assertIn("boundver.cli", self.report["submodules"])
        self.assertIn("boundver.core", self.report["submodules"])
        self.assertGreater(len(self.report["submodules"]), 20)

    def test_the_import_prints_nothing_and_spawns_nothing(self):
        self.assertEqual(self.report["stdout"], "")
        self.assertEqual(self.report["stderr"], "")
        self.assertEqual([event for event in self.report["events"] if event[0] != "open"], [])

    def test_the_import_reads_nothing_outside_the_installed_package(self):
        self.assertEqual(opens_outside(self.report), [])
        # The premise: the hook recorded file traffic at all, so an empty
        # "outside" list is a filtered list rather than an empty one.
        self.assertTrue([event for event in self.report["events"] if event[0] == "open"])

    def test_the_purity_harness_reports_the_side_effects_it_looks_for(self):
        """The negative control. A copy of the package with a git call, a print
        and a write outside the package appended to `__init__.py` has to make
        each of the three assertions above fail.
        """
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "boundver"
            shutil.copytree(
                Path(boundver.__file__).resolve().parent,
                copied,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            with (copied / "__init__.py").open("a", encoding="utf-8") as handle:
                handle.write(_IMPORT_POISON)
            report = import_report(directory)
        self.assertIsNone(report["error"])
        self.assertEqual(report["stdout"], "boundver loaded\n")
        spawned = [event for event in report["events"] if event[0] != "open"]
        self.assertTrue(spawned)
        self.assertTrue(any("git" in event[1] for event in spawned))
        outside = opens_outside(report)
        self.assertTrue(outside)
        self.assertTrue(any(event[1].endswith("side-effect.txt") for event in outside))


# ---------------------------------------------------------------------------
# OBL-PROVIDERS-060: the documented ConfigError promise
# ---------------------------------------------------------------------------


class ConfigErrorPromiseTests(unittest.TestCase):
    """OBL-PROVIDERS-060: what `docs/reference.md` tells an embedder to catch."""

    @classmethod
    def setUpClass(cls):
        cls.rows = rows_for(api_functions())

    def test_the_documentation_really_makes_this_promise(self):
        """The premise: the obligation quotes the docs, so the docs are read."""
        reference = (
            Path(boundver.__file__).resolve().parents[2] / "docs" / "reference.md"
        ).read_text(encoding="utf-8")
        self.assertIn("except boundver.ConfigError as error", reference)
        self.assertIn("the exported `boundver.ConfigError` (also a `ValueError`)", reference)

    def test_every_failure_reachable_from_load_config_is_a_config_error(self):
        """The obligation's first clause, over the whole corpus."""
        failures = [row for row in rows_for(["load_config"]) if row.raised]
        self.assertTrue(failures, "load_config never failed; the corpus lost its reach")
        for row in failures:
            with self.subTest(case=row.case, source=row.source):
                self.assertIsInstance(row.exception, boundver.ConfigError, row)
        self.assertEqual(
            {row.exception_name for row in failures}, {"boundver._utils.ConfigError"}
        )

    def test_an_over_limit_config_does_not_escape_as_a_guardrail_error(self):
        """The obligation's named counterexample, which does not reproduce.

        `_expand_component_paths` does raise `GuardrailError`, but
        `validate_config` collects it into the error list rather than letting
        it out, so what an embedder sees is the `ConfigError` the docs promise.
        The premise below is what keeps this from being a test of an ordinary
        config: the guardrail message has to be in the text.
        """
        rows = [row for row in self.rows if row.case == "over-limit-config"]
        self.assertTrue(rows)
        raised = [row for row in rows if row.raised]
        self.assertTrue(raised, "the over-limit config was accepted")
        for row in raised:
            with self.subTest(function=row.function):
                self.assertIsInstance(row.exception, boundver.ConfigError, row)
                self.assertNotIsInstance(row.exception, boundver.GuardrailError)
                message = str(row.exception)
                self.assertIn(
                    f"exceeds the {MAX_PROVIDER_DECLARATIONS}-declaration limit", message
                )
                self.assertIn(
                    "Component path expansion guardrail exceeded", message
                )
        self.assertEqual({row.function for row in raised}, set(api_functions()) - {"diff"})

    def test_one_input_produces_one_exception_class_across_the_three(self):
        """The obligation's third clause.

        `diff` is excluded because it takes no config and no source; among the
        three that share an input, the property is that those which raise
        cannot disagree about the class. `generate` legitimately fails where
        `load_config` succeeds, since it does strictly more work, so the check
        is over the classes actually raised rather than over raise-or-not.
        """
        shared = ("load_config", "generate", "verify")
        disagreements = []
        both_raised = 0
        for case in CASES:
            for source in case.sources:
                classes = {
                    row.function: row.exception_name
                    for row in self.rows
                    if row.case == case.name
                    and row.source == source
                    and row.function in shared
                    and row.raised
                }
                if len(classes) > 1:
                    both_raised += 1
                if len(set(classes.values())) > 1:
                    disagreements.append((case.name, source, classes))
        # The premise: at least a few cases made more than one of the three
        # fail, or "they never disagreed" would be a remark about singletons.
        self.assertGreaterEqual(both_raised, 5)
        self.assertEqual(disagreements, [])

    def test_the_promise_rests_on_validate_config_never_raising(self):
        """The residual risk, pinned rather than described.

        `_load_validated_config_inputs` converts everything raised inside its
        `try` to `ConfigError`, and then calls `validate_config` outside it. So
        the clause above holds because `validate_config` returns its errors
        rather than raising them, not because anything catches what it might
        raise. A `GuardrailError` from that call escapes all three functions
        untouched, which is exactly the shape the obligation feared and the
        reason it is worth pinning even though no repository input reaches it.
        """
        with Scenario("outside-the-try") as scene:
            build_repository(scene)
            with InRepo(scene.root):
                def refuse(*args, **kwargs):
                    raise boundver.GuardrailError("Guardrail exceeded: injected")

                with mock.patch("boundver._config.validate_config", refuse):
                    for name in ("load_config", "generate", "verify"):
                        function = getattr(boundver, name)
                        kwargs = {"out_path": None} if name == "generate" else {}
                        seen = call_capturing(function, **kwargs)
                        with self.subTest(function=name):
                            self.assertIsInstance(
                                seen.exception, boundver.GuardrailError
                            )
                            self.assertNotIsInstance(
                                seen.exception, boundver.ConfigError
                            )
                # The control: the same injection with the documented class
                # comes back out as the documented class, so the assertion
                # above is about the wrapper and not about the injection.
                def refuse_properly(*args, **kwargs):
                    raise boundver.ConfigError("injected")

                with mock.patch("boundver._config.validate_config", refuse_properly):
                    seen = call_capturing(boundver.load_config)
                self.assertIsInstance(seen.exception, boundver.ConfigError)


# ---------------------------------------------------------------------------
# OBL-LOCKFILE-062: the observations out-parameter
# ---------------------------------------------------------------------------


def return_lines(function) -> List[int]:
    """Every `return` in *function*'s own body, read from its source.

    Returns inside nested functions are excluded, because they are not exits
    from this function. The set moves with the file, which is the point: a
    refactor that adds a return path adds a line here and the trace below has
    to account for it.
    """
    source_file = Path(inspect.getsourcefile(function))
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function.__name__
    )
    nested = set()
    for node in ast.walk(target):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not target:
            nested.update(
                inner.lineno for inner in ast.walk(node) if isinstance(inner, ast.Return)
            )
    return sorted(
        {
            node.lineno
            for node in ast.walk(target)
            if isinstance(node, ast.Return) and node.lineno not in nested
        }
    )


class WriteBackRecorder(list):
    """The caller's list, instrumented.

    `verify_lockfile` writes back with `observations[:] = ...`, so a slice
    assignment is the mechanism rather than a proxy for it, and a path that
    leaves the list alone is distinguishable from one that writes back the same
    contents it was seeded with.
    """

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.slice_writes = 0

    def __setitem__(self, key, value) -> None:
        if isinstance(key, slice):
            self.slice_writes += 1
        super().__setitem__(key, value)


class ReturnPathTracer:
    """Record the line each watched function returned through."""

    def __init__(self, watched: Dict[Any, str]) -> None:
        self.watched = watched
        self.exits: List[Tuple[str, int]] = []

    def __call__(self, frame, event, arg):
        if event != "call":
            return None
        name = self.watched.get(frame.f_code)
        if name is None:
            return None

        def local(inner_frame, inner_event, inner_arg):
            if inner_event == "return":
                self.exits.append((name, inner_frame.f_lineno))
            return local

        return local


def verify_recording_return_paths(scene: Scenario, seed, **kwargs):
    """Call `boundver.verify` and report where it returned and what it wrote."""
    watched = {
        _lockfile.verify_lockfile.__code__: "verify_lockfile",
        boundver.verify.__code__: "boundver.verify",
    }
    tracer = ReturnPathTracer(watched)
    observations = WriteBackRecorder(seed)
    previous_trace = sys.gettrace()
    with InRepo(scene.root):
        sys.settrace(tracer)
        try:
            seen = call_capturing(boundver.verify, observations=observations, **kwargs)
        finally:
            sys.settrace(previous_trace)
    return seen, observations, tracer.exits


#: The verify invocations the trace is run over. Each pairs API keyword
#: arguments with the argv the CLI needs for the same invocation; the mapping
#: is written out because argparse's flag spellings are not derivable from the
#: Python signature.
INVOCATIONS = (
    ("default", {}, ()),
    ("one component", {"components": ["svc0"]}, ("--components", "svc0")),
    ("explicit facets", {"facets": ["boundary"]}, ("--facets", "boundary")),
    ("fail fast", {"fail_fast": True}, ("--fail-fast",)),
    ("unknown facet", {"facets": ["not-a-facet"]}, ("--facets", "not-a-facet")),
    ("unknown component", {"components": ["no-such"]}, ("--components", "no-such")),
)


class ObservationsOutParameterTests(unittest.TestCase):
    """OBL-LOCKFILE-062: the caller's list, on every return path."""

    @classmethod
    def setUpClass(cls):
        cls.observed = []
        for label, kwargs, _argv in INVOCATIONS:
            with Scenario("observations") as scene:
                build_repository(scene)
                drift(scene)
                seen, observations, exits = verify_recording_return_paths(
                    scene, ["SEED"], **kwargs
                )
                cls.observed.append(
                    (label, seen, list(observations), observations.slice_writes, exits)
                )

    def test_the_tracer_and_the_recorder_can_both_see_what_they_watch(self):
        """The premise for every claim below. The trace has to report an exit
        line from each watched function, and that line has to be a line the
        source really has a `return` on."""
        inner = return_lines(_lockfile.verify_lockfile)
        outer = return_lines(boundver.verify)
        self.assertGreater(len(inner), 10)
        self.assertEqual(len(outer), 2)
        reached = {
            exit
            for _label, seen, _observations, _writes, exits in self.observed
            if seen.exception is None
            for exit in exits
        }
        self.assertTrue(reached)
        for name, line in sorted(reached):
            with self.subTest(function=name, line=line):
                self.assertIn(line, inner if name == "verify_lockfile" else outer)
        self.assertEqual(
            {name for name, _line in reached}, {"verify_lockfile", "boundver.verify"}
        )
        # Every invocation writes back, including early returns.
        writes = {writes for _l, _s, _o, writes, _e in self.observed}
        self.assertNotIn(0, writes)

    def test_public_validation_keeps_unknown_selectors_out_of_the_inner_verifier(self):
        for label, seen, observations, writes, exits in self.observed:
            if not label.startswith("unknown"):
                continue
            with self.subTest(invocation=label):
                self.assertIsInstance(seen.exception, boundver.ConfigError)
                self.assertEqual(observations, [])
                self.assertGreaterEqual(writes, 1)
                self.assertEqual(
                    {name for name, _line in exits}, {"boundver.verify"}
                )

    def test_every_reached_return_path_writes_the_callers_list_back(self):
        """Every exit replaces the out-parameter for the current call."""
        for label, _seen, _observations, writes, exits in self.observed:
            for name, line in exits:
                with self.subTest(invocation=label, function=name, line=line):
                    self.assertGreaterEqual(
                        writes, 1, f"{name}:{line} returned without writing back"
                    )

    def test_every_invocation_writes_back(self):
        writing, silent = {}, {}
        for label, _seen, _observations, writes, exits in self.observed:
            inner = [line for name, line in exits if name == "verify_lockfile"]
            outer = [line for name, line in exits if name == "boundver.verify"]
            key = (tuple(inner), tuple(outer))
            (writing if writes else silent)[label] = key
        self.assertEqual(
            sorted(writing),
            [
                "default",
                "explicit facets",
                "fail fast",
                "one component",
                "unknown component",
                "unknown facet",
            ],
        )
        self.assertEqual(silent, {})

    def test_the_api_preflight_returns_without_reaching_the_writer_at_all(self):
        """The third path, which lives in `__init__.py` rather than in
        `_lockfile.py` and so needs its own repository."""
        with Scenario("preflight") as scene:
            build_repository(scene)
            drift(scene)
            lockfile = json.loads((scene.root / LOCK).read_text(encoding="utf-8"))
            lockfile["components"]["ghost"] = dict(lockfile["components"]["svc0"])
            (scene.root / LOCK).write_text(
                json.dumps(lockfile, indent=2) + "\n", encoding="utf-8"
            )
            scene.git("add", "--all")
            scene.git("commit", "-m", "a component the config does not declare")
            seen, observations, exits = verify_recording_return_paths(scene, ["SEED"])
            fresh, empty, _exits = verify_recording_return_paths(scene, [])
        self.assertIsNone(seen.exception, seen)
        self.assertEqual(
            seen.result,
            ["LOCKFILE component set differs from config: "
             "locked=['ghost', 'svc0'] configured=['svc0']"],
        )
        self.assertEqual([name for name, _line in exits], ["boundver.verify"])
        self.assertGreaterEqual(observations.slice_writes, 1)
        self.assertEqual(list(observations), [])
        self.assertEqual(list(empty), [])
        self.assertIsNone(fresh.exception, fresh)

    def test_the_direct_verifier_clears_before_an_early_return(self):
        observations = WriteBackRecorder(["SEED"])

        result = _lockfile.verify_lockfile(
            {}, {}, Path.cwd(), observations=observations
        )

        self.assertEqual(result, ["Config malformed: components must be a non-empty object"])
        self.assertEqual(list(observations), [])
        self.assertEqual(observations.slice_writes, 1)

    def test_where_both_speak_the_list_agrees_with_the_command_line(self):
        """The obligation's own oracle: the CLI's `observations` for the same
        invocation. It can only judge the paths where the CLI emits a payload,
        which is why the write-back trace above is the checker and this is the
        corroboration."""
        judged = 0
        with Scenario("observations-cli") as scene:
            build_repository(scene, components=3)
            drift(scene, components=3)
            for label, kwargs, argv in INVOCATIONS:
                seen, observations, _exits = verify_recording_return_paths(
                    scene, [], **kwargs
                )
                result = run_cli(scene.root, "verify", "--format", "json", *argv)
                with self.subTest(invocation=label):
                    if not result.stdout.strip():
                        # The CLI refuses an unknown selector before it renders
                        # anything, so there is no payload to compare against.
                        self.assertIsInstance(seen.exception, boundver.ConfigError)
                        self.assertEqual(list(observations), [])
                        self.assertEqual(result.returncode, 2)
                        self.assertIn("ERROR: unknown", result.stderr)
                        continue
                    self.assertIsNone(seen.exception, seen)
                    judged += 1
                    self.assertEqual(
                        json.loads(result.stdout)["observations"], list(observations)
                    )
        self.assertEqual(judged, 4)

    def test_entries_from_an_earlier_call_are_replaced(self):
        for label, _seen, observations, writes, _exits in self.observed:
            with self.subTest(invocation=label):
                self.assertGreaterEqual(writes, 1)
                self.assertNotIn("SEED", observations)

    def test_reusing_one_list_cannot_retain_a_stale_report(self):
        with Scenario("stale") as scene:
            build_repository(scene, components=2)
            scene.file("services/svc0/impl/run.py", "VALUE = 2\n")
            scene.commit("drift in svc0 only")
            observations: List[str] = []
            with InRepo(scene.root):
                self.assertEqual(boundver.verify(components=["svc0"],
                                                 observations=observations), [])
                first = list(observations)
                self.assertEqual(boundver.verify(components=["svc1"],
                                                 observations=observations), [])
                second = list(observations)
                with self.assertRaises(boundver.ConfigError):
                    boundver.verify(
                        components=["no-such"], observations=observations
                    )
                third = list(observations)
            result = run_cli(scene.root, "verify", "--format", "json",
                             "--components", "svc1")
        self.assertTrue(any("svc0" in entry for entry in first))
        self.assertEqual(json.loads(result.stdout)["observations"], [])
        self.assertEqual(second, [])
        self.assertEqual(third, [])

    def test_a_non_list_argument_is_the_callers_error_not_a_silent_skip(self):
        """An immutable sequence is rejected explicitly rather than ignored."""
        with Scenario("observations-type") as scene:
            build_repository(scene)
            drift(scene)
            with InRepo(scene.root):
                with self.assertRaises(TypeError) as raised:
                    boundver.verify(observations=("SEED",))
                self.assertEqual(
                    str(raised.exception), "observations must be a list or None"
                )
                # And the documented default touches nothing.
                self.assertEqual(boundver.verify(observations=None), [])

    def test_preexisting_entries_are_not_coerced_into_current_observations(self):
        with Scenario("observations-bounds") as scene:
            build_repository(scene)
            drift(scene)
            with InRepo(scene.root):
                mixed: List[Any] = [17, {"a": 1}, None]
                boundver.verify(observations=mixed)
                self.assertEqual({type(entry) for entry in mixed}, {str})
                many: List[Any] = [f"e{index}" for index in range(300)]
                boundver.verify(observations=many)
        self.assertNotIn("17", mixed)
        self.assertFalse(any(entry.startswith("e") for entry in many))
        self.assertLess(len(many), _utils.MAX_DIAGNOSTIC_ITEMS)
