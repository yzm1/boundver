"""A scenario builder for repository and digest regression tests.

`_repo_fixtures` gives a bare repository. These regressions need more than a
bare repository: a declared component tree,
an executable file, a symlink, a submodule gitlink, CRLF content,
a staged-versus-working-tree divergence — and then a *transform*, because a
metamorphic obligation is a statement about how a digest responds when the input
changes a known way.

This layer keeps property-based and metamorphic tests from rebuilding the same
repository scaffolding.

    with Scenario() as scene:
        scene.component("svc", path="services/svc",
                        provider="path-hash", boundary=["api/*.yaml"])
        scene.file("services/svc/api/v1.yaml", "openapi: 3.1.0\\n")
        scene.commit()
        before = scene.digest("svc", "boundary")
        scene.append_line("services/svc/api/v1.yaml", "# a trailing comment")
        scene.commit("edit")
        assert scene.digest("svc", "boundary") != before
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from boundver._lockfile import generate_lockfile, verify_lockfile

from tests._repo_fixtures import init_git_repo

_TEMP_ROOT = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None

#: Every source mode boundver accepts. Differential obligations that ask
#: whether two modes agree on an unmodified tree iterate this.
SOURCE_MODES = ("head", "index", "working-tree")

#: A stand-in commit id for submodule entries. Any non-null 40-hex works.
PLACEHOLDER_SUBMODULE_OID = "1" * 40

#: A bare line feed, spelled out so nothing here depends on os.linesep.
LF = bytes([10])


def supports_symlinks() -> bool:
    """Windows needs a privilege for symlinks, so tests must be able to skip."""
    with tempfile.TemporaryDirectory(dir=_TEMP_ROOT) as probe:
        target = Path(probe) / "target"
        target.write_text("x", encoding="utf-8")
        try:
            (Path(probe) / "link").symlink_to(target)
        except (OSError, NotImplementedError):
            return False
        return True


requires_symlinks = unittest.skipUnless(
    supports_symlinks(), "symlink creation is unavailable on this host"
)


class Scenario:
    """A disposable Git repository with a declared boundver configuration."""

    def __init__(self, project: str = "scenario", *, autocrlf: str = "false") -> None:
        # conftest patches TemporaryDirectory on Windows to retry transient Git
        # sharing violations during cleanup, so use it rather than mkdtemp.
        self._directory = tempfile.TemporaryDirectory(dir=_TEMP_ROOT)
        self.root = Path(self._directory.name)
        self.config: Dict[str, Any] = {"project": project, "components": {}}
        init_git_repo(self.root)
        self._pin_line_endings(autocrlf)

    def _pin_line_endings(self, autocrlf: str) -> None:
        """Stop Git from rewriting line endings behind a test's back.

        Developer machines commonly set ``core.autocrlf=true`` globally. Under
        that setting Git converts CRLF to LF on the way into the index, so a
        test that writes CRLF content stages LF and asserts nothing, while the
        same test on a Linux runner stages what it wrote. Pinning the value per
        repository, and disabling attribute-driven conversion in the repo-local
        ``info/attributes``, makes line endings mean the same thing everywhere.

        Pass a different *autocrlf* to test boundver under conversion on
        purpose. ``info/attributes`` is untracked, so it never reaches a digest.
        """
        self.git("config", "core.autocrlf", autocrlf)
        self.git("config", "core.eol", "lf")
        attributes = self.root / ".git" / "info" / "attributes"
        attributes.parent.mkdir(parents=True, exist_ok=True)
        if autocrlf == "false":
            attributes.write_bytes(b"* -text" + LF)

    # ---- lifecycle --------------------------------------------------------

    def __enter__(self) -> "Scenario":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._directory.cleanup()
        except OSError:
            # A scenario that exercises boundver's own output writer can be
            # left holding a name Win32 cannot address - a trailing dot or
            # space - and shutil.rmtree then fails on a directory it reports
            # as not empty. The extended-length prefix reaches those names.
            if os.name != "nt":
                raise
            shutil.rmtree("\\\\?\\" + str(self.root), ignore_errors=True)

    def git(self, *args: str) -> str:
        """Run git and return decoded, stripped stdout.

        Text mode translates CRLF to LF on the way in, so this cannot observe
        line endings and must not be used to read file content. Use
        ``git_bytes`` for anything where the exact bytes are the point.
        """
        result = subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def git_bytes(self, *args: str) -> bytes:
        """Run git and return stdout verbatim: no decoding, no stripping."""
        result = subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True
        )
        return result.stdout

    def blob(self, path: str, revision: str = "HEAD") -> bytes:
        """The exact bytes Git stored for *path*, which is the digest's input."""
        return self.git_bytes("cat-file", "-p", f"{revision}:{path}")

    # ---- declaration ------------------------------------------------------

    def component(
        self,
        name: str,
        *,
        path: str,
        provider: str = "path-hash",
        boundary: Optional[Iterable[str]] = None,
        behavior: Optional[Iterable[str]] = None,
        version_source: Optional[Dict[str, str]] = None,
        consumers: Optional[Iterable[str]] = None,
        external_consumers: Optional[Iterable[str]] = None,
        verify_facets: Optional[Iterable[str]] = None,
    ) -> "Scenario":
        """Declare a component. Selector paths are component-relative.

        Every builtin provider takes a path list; ``leaf`` and ``implicit``
        take an empty one, so the default covers them.
        """
        entry: Dict[str, Any] = {
            "path": path,
            "boundary": {"provider": provider, "paths": list(boundary or [])},
        }
        if behavior is not None:
            entry["behavior"] = {"paths": list(behavior)}
        if version_source is not None:
            entry["version_source"] = dict(version_source)
        if consumers is not None:
            entry["consumers"] = list(consumers)
        if external_consumers is not None:
            entry["external_consumers"] = list(external_consumers)
        if verify_facets is not None:
            entry["verify_facets"] = list(verify_facets)
        self.config["components"][name] = entry
        return self

    def defaults(self, **values: Any) -> "Scenario":
        self.config.setdefault("defaults", {}).update(values)
        return self

    def slice(
        self,
        name: str,
        *,
        mode: str = "exact",
        components: Optional[Iterable[str]] = None,
        closure_of: Optional[str] = None,
    ) -> "Scenario":
        """Declare a slice by member list or by consumer closure."""
        definition: Dict[str, Any] = {"mode": mode}
        if closure_of is not None:
            definition["closure_of"] = closure_of
        else:
            definition["components"] = list(components or [])
        self.config.setdefault("slices", {})[name] = definition
        return self

    # ---- content ----------------------------------------------------------

    def file(
        self,
        path: str,
        content: str = "placeholder\n",
        *,
        executable: bool = False,
        crlf: bool = False,
    ) -> "Scenario":
        """Write an owner-only text file, optionally executable or with CRLF."""
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if crlf:
            content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content.encode("utf-8"))
        if executable:
            os.chmod(target, 0o700)
        return self

    def json_file(self, path: str, value: Any, *, indent: int = 2) -> "Scenario":
        return self.file(path, json.dumps(value, indent=indent) + "\n")

    def symlink(self, path: str, target: str) -> "Scenario":
        link = self.root / path
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        return self

    def gitlink(self, path: str, oid: str = PLACEHOLDER_SUBMODULE_OID) -> "Scenario":
        """Add a mode-160000 index entry: an ordinary submodule, without one.

        Git refuses a null OID here, so the default is a fixed non-null one.
        Fixed rather than HEAD, because a digest over the index has to come out
        the same on every run.
        """
        self.git("update-index", "--add", "--cacheinfo", f"160000,{oid},{path}")
        return self

    def submodule(self, path: str, content: bytes = b"submodule content" + LF) -> str:
        """Create a nested repository and register it as a gitlink.

        `gitlink` alone registers an index entry with nothing on disk, which
        Git reports as a deletion, so the tree is not clean and a parity test
        cannot use it. A real nested repository is what a submodule actually
        looks like. Author and committer dates are fixed so the recorded
        commit id is the same on every run, which a pinned digest needs.
        """
        nested = self.root / path
        nested.mkdir(parents=True, exist_ok=True)
        init_git_repo(nested, initial_branch="main")
        (nested / "content.txt").write_bytes(content)
        stamp = "2000-01-01T00:00:00+00:00"
        environment = dict(
            os.environ,
            GIT_AUTHOR_DATE=stamp,
            GIT_COMMITTER_DATE=stamp,
        )
        for arguments in (["add", "--all"], ["commit", "-m", "submodule"]):
            subprocess.run(
                ["git", *arguments],
                cwd=nested,
                check=True,
                capture_output=True,
                env=environment,
            )
        oid = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=nested,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.git("update-index", "--add", "--cacheinfo", f"160000,{oid},{path}")
        return oid

    def remove(self, path: str) -> "Scenario":
        (self.root / path).unlink()
        return self

    # ---- transforms, for metamorphic obligations --------------------------

    def append_line(self, path: str, line: str) -> "Scenario":
        target = self.root / path
        target.write_bytes(target.read_bytes() + line.encode("utf-8") + b"\n")
        return self

    def reindent_json(self, path: str, *, indent: int = 4) -> "Scenario":
        """Reformat without changing the value: a canonical provider must not care."""
        target = self.root / path
        value = json.loads(target.read_text(encoding="utf-8"))
        target.write_text(json.dumps(value, indent=indent) + "\n", encoding="utf-8")
        return self

    def reorder_json_keys(self, path: str) -> "Scenario":
        target = self.root / path
        value = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value = dict(reversed(list(value.items())))
        target.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return self

    def to_crlf(self, path: str) -> "Scenario":
        target = self.root / path
        target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        return self

    def to_lf(self, path: str) -> "Scenario":
        target = self.root / path
        target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n"))
        return self

    def rename(self, old: str, new: str) -> "Scenario":
        destination = self.root / new
        destination.parent.mkdir(parents=True, exist_ok=True)
        (self.root / old).rename(destination)
        return self

    # ---- Git state --------------------------------------------------------

    def write_config(self, name: str = "boundary.config.json") -> "Scenario":
        (self.root / name).write_text(
            json.dumps(self.config, indent=2) + "\n", encoding="utf-8"
        )
        return self

    def stage(self, *paths: str) -> "Scenario":
        """Stage without committing, so index and working tree can diverge."""
        self.git("add", *(paths or ("--all",)))
        return self

    def commit(self, message: str = "scenario") -> "Scenario":
        """Write the config, stage everything, and commit.

        The commit is allowed to be empty. A transform that Git normalizes away
        leaves nothing staged, and whether that happens is often the assertion
        rather than a fixture failure: writing CRLF under ``autocrlf=true``
        stages the same bytes it replaced. An empty commit advances HEAD over an
        identical tree, which is exactly what such a test wants to observe.
        """
        self.write_config()
        self.git("add", "--all")
        self.git("commit", "--allow-empty", "-m", message)
        return self

    def commit_index(self, message: str = "index") -> "Scenario":
        """Commit the index exactly as it stands, staging nothing.

        `commit` runs ``git add --all``, which deletes an index entry whose
        path does not exist on disk. A gitlink added through
        ``update-index --cacheinfo`` is exactly that, so committing it needs a
        path that does not re-stage the working tree first. A test that used
        `commit` here would silently commit a tree without the entry it meant
        to test.
        """
        self.git("commit", "--allow-empty", "-m", message)
        return self

    def branch(self, name: str) -> "Scenario":
        """Create *name* at HEAD and switch to it."""
        self.git("checkout", "-b", name)
        return self

    def checkout(self, name: str) -> "Scenario":
        self.git("checkout", name)
        return self

    def merge(self, name: str, message: str = "merge") -> "Scenario":
        """Merge *name* into the current branch, always creating a commit.

        `--no-ff` matters for every test about first-parent history. A
        fast-forward moves the branch pointer and leaves no merge commit, so
        the mainline would contain the topic commits themselves and the
        distinction the walk depends on would not exist to be tested.
        """
        self.git("merge", "--no-ff", "-m", message, name)
        return self

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    def first_parent_commits(self, revision: str = "HEAD") -> List[str]:
        """The mainline, which is what the diagnostic walk follows."""
        output = self.git("rev-list", "--first-parent", revision)
        return output.splitlines() if output else []

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    # ---- running boundver -------------------------------------------------

    def generate(self, source: str = "head", **kwargs: Any) -> Dict[str, Any]:
        return generate_lockfile(self.config, self.root, source=source, **kwargs)

    def verify(self, lockfile: Dict[str, Any], source: str = "head", **kwargs: Any) -> List[Any]:
        return verify_lockfile(self.config, lockfile, self.root, source=source, **kwargs)

    def digest(self, component: str, facet: str, source: str = "head") -> Optional[str]:
        """The recorded fingerprint for one facet, or None when unavailable."""
        return self.fingerprints(component, source=source)[facet]

    def fingerprints(self, component: str, source: str = "head") -> Dict[str, Any]:
        """All four facet digests for one component."""
        lockfile = self.generate(source=source)
        return dict(lockfile["components"][component]["fingerprints"])

    def slice_digest(self, name: str, source: str = "head") -> Any:
        return self.generate(source=source)["slices"][name]["fingerprint"]


def digest_is_stable_under(scene: Scenario, component: str, facet: str, transform) -> bool:
    """Apply *transform*, re-commit, and report whether the facet digest held."""
    before = scene.digest(component, facet)
    transform(scene)
    scene.commit("transform")
    return scene.digest(component, facet) == before
