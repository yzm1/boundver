"""Source-identity diagnostics for lockfile regeneration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from boundver._lockfile import semantic_config_digest
from tests._repo_fixtures import commit_all, init_git_repo


ROOT = Path(__file__).resolve().parents[1]


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "boundver", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
    )


def _write_config(root: Path, marker: str = "base", *, indent: int = 2) -> dict:
    config = {
        "project": "config-source-notice",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {
                    "provider": "path-hash",
                    "paths": ["contract.json"],
                },
                "external_consumers": [marker],
            }
        },
        "slices": {},
    }
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=indent) + "\n",
        encoding="utf-8",
    )
    return config


def _repository(root: Path) -> None:
    init_git_repo(root, initial_branch="main")
    (root / "svc").mkdir()
    (root / "svc" / "contract.json").write_text(
        '{"version": 1}\n',
        encoding="utf-8",
    )
    (root / "svc" / "impl.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_config(root)
    generated = _run(root, "generate", "--source", "working-tree")
    assert generated.returncode == 0, generated.stderr
    commit_all(root, "record initial lock")


def _commit_component_drift(root: Path) -> None:
    (root / "svc" / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit_all(root, "change implementation")


@pytest.mark.parametrize("source", ("head", "index", "working-tree"))
def test_update_reports_only_a_distinct_non_working_tree_config(
    tmp_path: Path,
    source: str,
) -> None:
    _repository(tmp_path)
    if source == "head":
        _commit_component_drift(tmp_path)
        selected_config = json.loads(
            subprocess.run(
                ["git", "show", "HEAD:boundary.config.json"],
                cwd=tmp_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        working_config = _write_config(tmp_path, "working")
    elif source == "index":
        (tmp_path / "svc" / "impl.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        selected_config = _write_config(tmp_path, "index")
        subprocess.run(
            ["git", "add", "svc/impl.py", "boundary.config.json"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        working_config = _write_config(tmp_path, "working")
    else:
        (tmp_path / "svc" / "impl.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        selected_config = _write_config(tmp_path, "working")
        working_config = selected_config

    result = _run(
        tmp_path,
        "verify",
        "--source",
        source,
        "--update",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    if source == "working-tree":
        assert payload["notices"] == []
    else:
        assert len(payload["notices"]) == 1
        notice = payload["notices"][0]
        assert notice["code"] == "working-tree-config-diverges"
        assert notice["source"] == source
        assert notice["working_tree_config"]["status"] == "different"
        assert source.upper() in notice["selected_config"]["identity"]
        assert notice["working_tree_config"]["identity"].startswith(
            "WORKING-TREE:"
        )
        assert notice["selected_config"]["digest"] == semantic_config_digest(
            selected_config
        )
        assert notice["working_tree_config"]["digest"] == semantic_config_digest(
            working_config
        )

    written_lock = json.loads(
        (tmp_path / "boundary.lock.json").read_text(encoding="utf-8")
    )
    assert written_lock["config_digest"] == semantic_config_digest(selected_config)


def test_semantically_identical_working_config_does_not_warn(tmp_path: Path) -> None:
    _repository(tmp_path)
    _commit_component_drift(tmp_path)
    # Formatting is not part of the generated lock contract.
    _write_config(tmp_path, indent=4)

    result = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--update",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["notices"] == []


def test_missing_working_config_is_reported_without_changing_generation_source(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    _commit_component_drift(tmp_path)
    (tmp_path / "boundary.config.json").unlink()

    result = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--update",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    notice = json.loads(result.stdout)["notices"][0]
    assert notice["working_tree_config"]["status"] == "missing"
    assert notice["working_tree_config"]["digest"] is None
    selected_config = json.loads(
        subprocess.run(
            ["git", "show", "HEAD:boundary.config.json"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    written_lock = json.loads(
        (tmp_path / "boundary.lock.json").read_text(encoding="utf-8")
    )
    assert written_lock["config_digest"] == semantic_config_digest(selected_config)


def test_unreadable_working_config_is_reported_without_echoing_its_content(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    _commit_component_drift(tmp_path)
    secret_like_text = "PRIVATE_VALUE_SHOULD_NOT_REACH_OUTPUT"
    (tmp_path / "boundary.config.json").write_text(
        '{"broken": [' + secret_like_text,
        encoding="utf-8",
    )

    result = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--update",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    notice = json.loads(result.stdout)["notices"][0]
    assert notice["working_tree_config"]["status"] == "invalid-or-unreadable"
    assert notice["working_tree_config"]["digest"] is None
    assert secret_like_text not in result.stdout + result.stderr


def test_missing_snapshot_config_fails_without_falling_back_or_writing(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    old_lock = (tmp_path / "boundary.lock.json").read_bytes()
    (tmp_path / "boundary.config.json").unlink()
    (tmp_path / "svc" / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit_all(tmp_path, "remove selected config")
    _write_config(tmp_path, "working")

    result = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--update",
    )

    assert result.returncode == 2
    assert "Config file not found in captured head source" in result.stderr
    assert (tmp_path / "boundary.lock.json").read_bytes() == old_lock


def test_text_notice_is_visible_and_strict_mode_fails_closed(tmp_path: Path) -> None:
    _repository(tmp_path)
    _commit_component_drift(tmp_path)
    _write_config(tmp_path, "working")
    old_lock = (tmp_path / "boundary.lock.json").read_bytes()

    strict = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--update",
        "--strict-config-source",
        "--format",
        "json",
    )

    assert strict.returncode == 2, strict.stderr
    assert strict.stderr == ""
    payload = json.loads(strict.stdout)
    assert payload["ok"] is False
    assert payload["updated"] is False
    assert len(payload["notices"]) == 1
    assert any(
        issue.startswith("CONFIG SOURCE DIVERGENCE:")
        for issue in payload["issues"]
    )
    assert (tmp_path / "boundary.lock.json").read_bytes() == old_lock

    text = _run(
        tmp_path,
        "verify",
        "--source",
        "head",
        "--update",
        "--strict-config-source",
    )
    assert text.returncode == 2
    assert "NOTICE: verify --update is using source=head config HEAD@" in text.stderr
    assert "WORKING-TREE:boundary.config.json is different" in text.stderr
    assert "CONFIG SOURCE DIVERGENCE" in text.stderr
    assert (tmp_path / "boundary.lock.json").read_bytes() == old_lock
