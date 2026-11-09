"""A generation preview reports changes but cannot publish or accept history."""

import copy
import json
import os
from pathlib import Path

import pytest

from boundver._change_report import lock_change_report
from boundver._lockfile import dump_lockfile, generate_lockfile
from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario
from tests._schema_validation import assert_output_schema


@pytest.fixture
def repository():
    with Scenario("preview") as scene:
        scene.component("svc", path="svc", provider="path-hash", boundary=["api.txt"])
        scene.config["components"]["svc"]["version_source"] = {
            "file": "version.json",
            "field": "version",
        }
        scene.config["slices"] = {"api": {"mode": "boundary", "components": ["svc"]}}
        scene.file("svc/api.txt", "original\n")
        scene.file("svc/version.json", '{"version":"1.0.0"}\n')
        scene.commit()
        previous = generate_lockfile(scene.config, scene.root)
        scene.file("boundary.lock.json", dump_lockfile(previous))
        scene.commit("lock")
        yield scene, previous


def report(previous, current, limit=1000):
    result = lock_change_report(
        previous,
        current,
        source={
            "mode": "head",
            "commit": None,
            "tree": None,
            "lock": "boundary.lock.json",
        },
        limit=limit,
    )
    validate_result(result)
    return result


def validate_result(result):
    assert_output_schema("cli-output.lock-changes.schema.json", result)


def test_equal_contracts_have_explicit_unchanged_version_status(repository):
    scene, previous = repository
    result = report(previous, generate_lockfile(scene.config, scene.root))
    assert result["complete"] and result["publication"] == "not-written"
    assert result["counts"]["unchanged"] == 1
    assert result["components"][0]["version"]["status"] == "unchanged"
    assert result["locked_source"] == {"status": "unavailable", "commit": None}


@pytest.mark.parametrize("limit", [0, 1000])
def test_version_changes_and_facets_are_counted_even_when_rows_are_hidden(
    repository, limit
):
    scene, previous = repository
    scene.file("svc/version.json", '{"version":"1.1.0"}\n')
    scene.commit("version movement")
    result = report(previous, generate_lockfile(scene.config, scene.root), limit=limit)
    assert result["complete"] and result["counts"]["changed"] == 1
    assert result["counts"]["version_changes"] == 1
    if limit:
        row = result["components"][0]
        assert row["version"] == {
            "previous": "1.0.0",
            "current": "1.1.0",
            "status": "changed",
        }
        assert "exact" in row["facets"] and "boundary" not in row["facets"]
    else:
        assert result["components"] == [] and result["presentation"]["truncated"]


def test_changed_declarations_do_not_become_ordinary_contract_changes(repository):
    scene, previous = repository
    scene.config["components"]["svc"]["external_consumers"] = ["outside"]
    scene.commit("declaration change")
    result = report(previous, generate_lockfile(scene.config, scene.root))
    assert result["complete"] and not result["comparison"]["declarations_match"]
    assert result["counts"]["incomparable"] == 1 and result["counts"]["changed"] == 0
    assert result["components"][0]["status"] == "incomparable"


def test_provider_runtime_change_is_incomparable_even_with_equal_declarations(
    repository,
):
    scene, previous = repository
    altered = copy.deepcopy(previous)
    altered["components"]["svc"]["boundary_provider_version"] = "different"
    result = report(altered, generate_lockfile(scene.config, scene.root))
    assert result["counts"]["incomparable"] == 1
    assert result["slices"][0]["status"] == "incomparable"


def test_missing_previous_lock_is_explicit_bootstrap_not_accepted_history(repository):
    scene, _ = repository
    result = report(None, generate_lockfile(scene.config, scene.root))
    assert result["complete"] and not result["comparison"]["previous_present"]
    assert result["counts"]["added"] == 1
    assert result["components"][0]["version"]["status"] == "unavailable"


def test_old_digest_contract_is_not_relabelled_or_accepted(repository):
    scene, previous = repository
    old = copy.deepcopy(previous)
    old["schema"] = "boundary-lock/v3"
    result = report(old, generate_lockfile(scene.config, scene.root))
    assert not result["complete"] and result["exit_code"] == 2
    assert result["counts"] is None and old["schema"] == "boundary-lock/v3"


def test_preview_format_refuses_publication_without_dry_run(repository):
    scene, _ = repository
    before = (scene.root / "boundary.lock.json").read_bytes()
    result = run_cli(scene.root, "generate", "--format", "changes-json")
    assert result.returncode == 2 and "requires --dry-run" in result.stderr
    assert (scene.root / "boundary.lock.json").read_bytes() == before


@pytest.mark.parametrize("timed", [False, True])
def test_cli_preview_uses_committed_old_lock_despite_dirty_disk_and_never_writes(
    repository, timed
):
    scene, _ = repository
    scene.file("svc/version.json", '{"version":"2.0.0"}\n')
    scene.commit("new version without relocking")
    scene.file("boundary.lock.json", "uncommitted other session's file")
    before = (scene.root / "boundary.lock.json").read_bytes()
    extra = ["--timeout", "60"] if timed else []
    result = run_cli(
        scene.root, "generate", "--dry-run", "--format", "changes-json", *extra
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    validate_result(payload)
    assert payload["schema"] == "boundver-lock-changes/v1"
    assert payload["counts"]["version_changes"] == 1
    assert payload["components"][0]["version"]["current"] == "2.0.0"
    assert (scene.root / "boundary.lock.json").read_bytes() == before


def test_scoped_preview_reuses_one_selected_old_lock_read(repository, monkeypatch):
    from boundver import core

    scene, _ = repository
    original = core.load_lockfile_file
    calls = []

    def read(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "load_lockfile_file", read)
    result = run_cli_in_process(
        scene.root,
        "generate",
        "--dry-run",
        "--format",
        "changes-json",
        "--components",
        "svc",
    )
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert json.loads(result.stdout)["counts"]["unchanged"] == 1


def test_bootstrap_preview_does_not_create_output(repository):
    scene, _ = repository
    result = run_cli(
        scene.root,
        "generate",
        "--dry-run",
        "--format",
        "changes-json",
        "--out",
        "new.lock.json",
    )
    assert result.returncode == 0, result.stderr
    assert not (scene.root / "new.lock.json").exists()
    assert json.loads(result.stdout)["counts"]["added"] == 1


@pytest.mark.parametrize("source", ["working-tree", "head", "index"])
@pytest.mark.parametrize("timed", [False, True])
@pytest.mark.parametrize("prefix", ["", "existing/"])
def test_bootstrap_preview_accepts_missing_output_directories_without_creating_them(
    repository, source, timed, prefix,
):
    scene, _ = repository
    if prefix:
        (scene.root / "existing").mkdir()
    previous_bytes = (scene.root / "boundary.lock.json").read_bytes()
    index_bytes = (scene.root / ".git" / "index").read_bytes()
    relative = prefix + "new-dir/nested/new.lock.json"
    arguments = ["generate", "--source", source, "--dry-run", "--format",
                 "changes-json", "--out", relative]
    if timed:
        arguments.extend(["--timeout", "30"])
    result = run_cli(scene.root, *arguments)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    validate_result(payload)
    assert payload["complete"] and payload["publication"] == "not-written"
    assert not payload["comparison"]["previous_present"]
    assert payload["counts"]["added"] == 1
    assert not (scene.root / (prefix + "new-dir")).exists()
    assert (scene.root / "boundary.lock.json").read_bytes() == previous_bytes
    assert (scene.root / ".git" / "index").read_bytes() == index_bytes


def test_existing_json_dry_run_still_returns_a_lock_not_a_report(repository):
    scene, _ = repository
    result = run_cli(scene.root, "generate", "--dry-run", "--format", "json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema"] == "boundary-lock/v4"


@pytest.mark.parametrize("suffix", ["saved.lock.json", "new-dir/nested/new.lock.json"])
def test_working_tree_preview_rejects_symlink_ancestors_before_reading_old_lock(
    repository, tmp_path, monkeypatch, suffix,
):
    from boundver import core

    scene, previous = repository
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "saved.lock.json"
    external.write_text(dump_lockfile(previous), encoding="utf-8")
    try:
        os.symlink(outside, scene.root / "alias", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    calls = []
    original = core.load_lockfile_file

    def read(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "load_lockfile_file", read)
    result = run_cli_in_process(
        scene.root,
        "generate",
        "--source",
        "working-tree",
        "--dry-run",
        "--format",
        "changes-json",
        "--out",
        "alias/" + suffix,
    )
    assert result.returncode == 2
    assert calls == []
    assert external.read_text(encoding="utf-8") == dump_lockfile(previous)
    assert not (outside / "new-dir").exists()


@pytest.mark.parametrize("change", ["prefix-replaced", "missing-ancestor-created"])
def test_bootstrap_preview_refuses_ancestor_changes_without_reading_a_new_lock(
    repository, monkeypatch, change,
):
    from boundver import core

    scene, _ = repository
    existing = scene.root / "existing"
    existing.mkdir()
    original = core._capture_working_tree_ancestors
    reads = []

    def capture(*args, **kwargs):
        identities = original(*args, **kwargs)
        if change == "prefix-replaced":
            existing.rename(scene.root / "preserved-existing")
            existing.mkdir()
        else:
            (existing / "new-dir").mkdir()
        return identities

    def read(*args, **kwargs):
        reads.append(args[0])
        raise AssertionError("Bootstrap must not read through a missing ancestor")

    monkeypatch.setattr(core, "_capture_working_tree_ancestors", capture)
    monkeypatch.setattr(core, "load_lockfile_file", read)
    result = run_cli_in_process(
        scene.root, "generate", "--source", "working-tree", "--dry-run",
        "--format", "changes-json", "--out", "existing/new-dir/nested/new.lock.json",
    )
    assert result.returncode == 2, result.stdout
    assert reads == []
    assert not (existing / "new-dir/nested/new.lock.json").exists()


def test_bootstrap_preview_rejects_reparse_prefix_before_reading_missing_lock(
    repository, monkeypatch,
):
    from boundver import core, _hashing

    scene, _ = repository
    existing = scene.root / "existing"
    existing.mkdir()
    identity = existing.lstat()
    reads = []
    monkeypatch.setattr(
        _hashing, "_is_windows_reparse_point",
        lambda value: (value.st_dev, value.st_ino) == (identity.st_dev, identity.st_ino),
    )

    def read(*args, **kwargs):
        reads.append(args[0])
        raise AssertionError("Reparse prefix must be refused before a lock read")

    monkeypatch.setattr(core, "load_lockfile_file", read)
    result = run_cli_in_process(
        scene.root, "generate", "--source", "working-tree", "--dry-run",
        "--format", "changes-json", "--out", "existing/new-dir/nested/new.lock.json",
    )
    assert result.returncode == 2, result.stdout
    assert reads == []
    assert not (existing / "new-dir").exists()


def test_change_report_budget_matches_pretty_printed_output(repository, monkeypatch):
    from boundver import _change_report

    scene, previous = repository
    current = generate_lockfile(scene.config, scene.root)
    result = report(previous, current)
    compact = len(json.dumps(result).encode("utf-8"))
    pretty = len(json.dumps(result, indent=2, sort_keys=True).encode("utf-8")) + 1
    assert compact < pretty
    monkeypatch.setattr(_change_report, "MAX_CHANGE_REPORT_BYTES", compact + 1)
    result = report(previous, current)
    assert not result["complete"] and result["exit_code"] == 2
    assert result["counts"] is None and result["components"] == []


def test_lock_diff_exposes_the_same_version_facts_without_writing(repository):
    scene, previous = repository
    scene.file("svc/version.json", '{"version":"2.0.0"}\n')
    scene.commit("version")
    current = generate_lockfile(scene.config, scene.root)
    scene.file("old.lock.json", dump_lockfile(previous))
    scene.file("new.lock.json", dump_lockfile(current))
    before = (scene.root / "new.lock.json").read_bytes()
    result = run_cli(
        scene.root, "diff", "old.lock.json", "new.lock.json", "--format", "changes-json"
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    validate_result(payload)
    assert payload["operation"] == "lock-diff"
    assert payload["source"]["previous_lock"] == "old.lock.json"
    assert payload["counts"]["version_changes"] == 1
    assert (scene.root / "new.lock.json").read_bytes() == before


def test_version_status_schema_cannot_diverge_between_reports():
    spec = Path(__file__).resolve().parents[1] / "spec"
    slice_schema = json.loads(
        (spec / "cli-output.slice-explanation.schema.json").read_text()
    )
    changes = json.loads((spec / "cli-output.lock-changes.schema.json").read_text())
    assert (
        slice_schema["properties"]["members"]["items"]["properties"]["version"]
        == changes["properties"]["components"]["items"]["properties"]["version"]
    )


@pytest.mark.parametrize("source", ["index", "working-tree"])
def test_preview_preserves_lock_and_index_entries_for_explicit_sources(
    repository, source
):
    scene, _ = repository
    scene.file("svc/version.json", '{"version":"2.0.0"}\n')
    if source == "index":
        scene.git("add", "svc/version.json")
    before_index = scene.git("ls-files", "--stage", "-z")
    before_lock = (scene.root / "boundary.lock.json").read_bytes()
    result = run_cli(
        scene.root,
        "generate",
        "--source",
        source,
        "--dry-run",
        "--format",
        "changes-json",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    validate_result(payload)
    assert payload["source"]["mode"] == source
    assert payload["components"][0]["version"]["current"] == "2.0.0"
    assert (scene.root / "boundary.lock.json").read_bytes() == before_lock
    assert scene.git("ls-files", "--stage", "-z") == before_index
