"""All release-tool selectors reject repository crossings through aliases."""

from __future__ import annotations

import importlib.util
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
            assert module._trusted_tool("gh", repository) == str(executable)
    else:
        module = _load("_release_platform")
        assert module._trusted_external_file(str(selected), repository) == str(executable)


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
