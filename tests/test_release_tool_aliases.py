"""All release-tool selectors reject repository crossings through aliases."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


REPO = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "alias_test_" + name, REPO / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _link(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError:
        if sys.platform == "win32":
            if directory:
                # NTFS junctions exercise reparse-point traversal without the
                # privilege required to create ordinary Windows symlinks.
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                    check=False,
                )
                if result.returncode == 0:
                    return
            pytest.skip("Windows symlink permission unavailable")
        raise


@pytest.mark.parametrize("selector", ["candidate", "publisher", "bash"])
@pytest.mark.parametrize("escape_directory", [False, True])
def test_repository_alias_cannot_hide_an_external_tool(
    tmp_path, selector, escape_directory
):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    external = root / "tools"
    external.mkdir()
    executable = external / "python"
    executable.write_bytes(b"trusted executable")
    alias = root / "alias"
    _link(alias, root, directory=True)
    if escape_directory:
        _link(repository / "bin", external, directory=True)
        selected = alias / "repository" / "bin" / "python"
    else:
        _link(repository / "python", executable)
        selected = alias / "repository" / "python"

    if selector == "candidate":
        module = _load("verify_release_candidate")
        with pytest.raises(module.CandidateVerificationError, match="release repository"):
            module._trusted_tool(str(selected), repository, None)
    elif selector == "publisher":
        module = _load("publish_release")
        with mock.patch.object(module.shutil, "which", return_value=str(selected)):
            with pytest.raises(module.GateError, match="inside the repository"):
                module._trusted_tool("gh", repository)
    else:
        module = _load("_release_platform")
        assert module._trusted_external_file(str(selected), repository) is None


@pytest.mark.parametrize("selector", ["candidate", "publisher", "bash"])
def test_external_relative_file_link_chain_remains_usable(tmp_path, selector):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    tools = root / "tools"
    tools.mkdir()
    executable = root / "external-python"
    executable.write_bytes(b"trusted executable")
    _link(tools / "hop", Path("../external-python"))
    selected = tools / "launcher"
    _link(selected, Path("hop"))
    if selector == "candidate":
        module = _load("verify_release_candidate")
        assert module._trusted_tool(str(selected), repository, None) == str(selected)
    elif selector == "publisher":
        module = _load("publish_release")
        with mock.patch.object(module.shutil, "which", return_value=str(selected)):
            assert Path(module._trusted_tool("gh", repository)).samefile(executable)
    else:
        module = _load("_release_platform")
        resolved = module._trusted_external_file(str(selected), repository)
        assert resolved is not None
        assert Path(resolved).samefile(executable)


@pytest.mark.parametrize("selector", ["candidate", "publisher", "bash"])
@pytest.mark.parametrize("shape", ["cycle", "expanding"])
def test_external_link_work_is_bounded(tmp_path, selector, shape):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    selected = root / "launcher"
    _link(selected, Path("launcher" if shape == "cycle" else "launcher/launcher"))
    if selector == "candidate":
        module = _load("verify_release_candidate")
        with pytest.raises(module.CandidateVerificationError, match="unavailable"):
            module._trusted_tool(str(selected), repository, None)
    elif selector == "publisher":
        module = _load("publish_release")
        with mock.patch.object(module.shutil, "which", return_value=str(selected)):
            with pytest.raises(module.GateError, match="cannot resolve"):
                module._trusted_tool("gh", repository)
    else:
        module = _load("_release_platform")
        assert module._trusted_external_file(str(selected), repository) is None


def test_external_tool_component_budget(tmp_path):
    module = _load("_release_platform")
    repository = tmp_path.resolve() / "repository"
    repository.mkdir()
    selected = tmp_path.resolve().joinpath(*([".."] * 4097))
    with pytest.raises(RuntimeError, match="component budget"):
        module.resolve_external_tool_path(selected, repository)


def test_external_tool_link_target_budget(tmp_path):
    module = _load("_release_platform")
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    selected = root / "launcher"
    _link(selected, Path("launcher"))
    with mock.patch.object(Path, "readlink", return_value=Path("x" * 262145)):
        with pytest.raises(RuntimeError, match="link target budget"):
            module.resolve_external_tool_path(selected, repository)


@pytest.mark.parametrize("selector", ["candidate", "publisher", "bash"])
def test_external_directory_alias_remains_usable(tmp_path, selector):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    tools = root / "tools"
    tools.mkdir()
    executable = tools / "python"
    executable.write_bytes(b"trusted executable")
    alias = root / "alias"
    _link(alias, root, directory=True)
    selected = alias / "tools" / "python"

    if selector == "candidate":
        module = _load("verify_release_candidate")
        assert module._trusted_tool(str(selected), repository, None) == str(selected)
    elif selector == "publisher":
        module = _load("publish_release")
        with mock.patch.object(module.shutil, "which", return_value=str(selected)):
            assert Path(module._trusted_tool("gh", repository)).samefile(executable)
    else:
        module = _load("_release_platform")
        # Windows readlink may retain the valid extended-length '\\?\' prefix.
        # Target identity, not one spelling of its path, is the contract.
        resolved = module._trusted_external_file(str(selected), repository)
        assert resolved is not None
        assert Path(resolved).samefile(executable)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reparse-point launch")
@pytest.mark.parametrize("selector", ["candidate", "publisher", "bash"])
def test_windows_directory_alias_can_launch_the_external_tool(tmp_path, selector):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    executable = Path(sys.executable).resolve()
    alias = root / "tools"
    _link(alias, executable.parent, directory=True)
    selected = alias / executable.name
    if selector == "candidate":
        module = _load("verify_release_candidate")
        command = module._trusted_tool(str(selected), repository, None)
        assert command == str(selected)
    elif selector == "publisher":
        module = _load("publish_release")
        with mock.patch.object(module.shutil, "which", return_value=str(selected)):
            command = module._trusted_tool("python", repository)
    else:
        module = _load("_release_platform")
        command = module._trusted_external_file(str(selected), repository)
    assert command is not None
    assert Path(command).samefile(executable)
    result = subprocess.run(
        [command, "-I", "-c", "print('external-tool-ok')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=True,
    )
    assert result.stdout.strip() == "external-tool-ok"


def test_normalized_candidate_launcher_is_the_target_that_was_validated(tmp_path):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    internal = repository / "python"
    internal.write_bytes(b"repository executable")
    tools = root / "tools"
    tools.mkdir()
    _link(tools / "python", internal)
    external = root / "external"
    external.mkdir()
    (external / "nested").mkdir()
    (external / "python").write_bytes(b"external executable")
    _link(tools / "redirect", external / "nested", directory=True)
    selected = tools / "redirect" / ".." / "python"
    # resolve(selected) is external, but abspath(selected), which the launcher
    # executes, collapses '..' lexically and points at the repository tool.
    module = _load("verify_release_candidate")
    with pytest.raises(module.CandidateVerificationError, match="release repository"):
        module._trusted_tool(str(selected), repository, None)


@pytest.mark.parametrize("selector", ["candidate", "publisher", "bash"])
@pytest.mark.parametrize("target_only", [False, True])
def test_repository_identity_wins_over_a_different_resolved_spelling(
    tmp_path, selector, target_only
):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    external = root / "external-python"
    external.write_bytes(b"external executable")
    alias = root / "alias"
    _link(alias, repository, directory=True)
    if target_only:
        (repository / "python").write_bytes(b"repository executable")
        selected = root / "launcher"
        _link(selected, alias / "python")
    else:
        _link(repository / "python", external)
        selected = alias / "python"
    original_resolve = Path.resolve

    def preserve_spelling(path, *args, **kwargs):
        # A resolver need not canonicalize spelling on a case-insensitive
        # filesystem. The actual directory identities still agree here.
        if path == alias:
            return path
        if target_only and path == selected:
            return alias / "python"
        return original_resolve(path, *args, **kwargs)

    with mock.patch.object(Path, "resolve", preserve_spelling):
        if selector == "candidate":
            module = _load("verify_release_candidate")
            with pytest.raises(module.CandidateVerificationError, match="release repository"):
                module._trusted_tool(str(selected), repository, None)
        elif selector == "publisher":
            module = _load("publish_release")
            with mock.patch.object(module.shutil, "which", return_value=str(selected)):
                with pytest.raises(module.GateError, match="inside the repository"):
                    module._trusted_tool("gh", repository)
        else:
            module = _load("_release_platform")
            assert module._trusted_external_file(str(selected), repository) is None


@pytest.mark.parametrize("selector", ["candidate", "publisher", "bash"])
@pytest.mark.parametrize("relative", [False, True])
def test_repository_owned_intermediate_file_link_is_rejected(tmp_path, selector, relative):
    root = tmp_path.resolve()
    repository = root / "repository"
    repository.mkdir()
    external = root / "external-python"
    external.write_bytes(b"trusted executable")
    _link(repository / "hop", external)
    selected = root / "launcher"
    target = Path("repository/hop") if relative else repository / "hop"
    _link(selected, target)
    if selector == "candidate":
        module = _load("verify_release_candidate")
        with pytest.raises(module.CandidateVerificationError, match="release repository"):
            module._trusted_tool(str(selected), repository, None)
    elif selector == "publisher":
        module = _load("publish_release")
        with mock.patch.object(module.shutil, "which", return_value=str(selected)):
            with pytest.raises(module.GateError, match="inside the repository"):
                module._trusted_tool("gh", repository)
    else:
        module = _load("_release_platform")
        assert module._trusted_external_file(str(selected), repository) is None
