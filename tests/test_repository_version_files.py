"""Repository-root version inputs retain source, safety and dependency identity."""

import copy
import json
import os
import subprocess

import pytest

from boundver import _config
from boundver._git import _capture_git_source_snapshot, changed_components_since_ref
from boundver._lockfile import generate_lockfile, semantic_config_digest, verify_lockfile
from boundver._output import analyze_component_drift, analyze_explain_changes
from boundver._utils import ConfigError
from boundver._version_paths import version_file_repo_path
from boundver.versions import extract_version
from tests._parity import run_cli
from tests._repo_fixtures import commit_all, init_git_repo


def _git(root, *args, **kwargs):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **kwargs)


def _repository(root):
    init_git_repo(root)
    for name in ("jobs/task", "jobs/task-2", "client", "ui", "unrelated", "releases", "config"):
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "owned.txt").write_text("owned", encoding="utf-8")
    (root / "releases" / "task.json").write_text('{"image":{"version":"1.2.3"}}', encoding="utf-8")
    config = {
        "project": "repository-versions",
        "components": {
            "task": {"path": "jobs/task", "version_source": {
                "repo_file": "releases/task.json", "field": "image.version"},
                "boundary": {"provider": "leaf", "paths": []}},
            "task-2": {"path": "jobs/task-2", "version_source": {
                "repo_file": "releases/task.json", "field": "image.version"},
                "boundary": {"provider": "leaf", "paths": []}},
            "client": {"path": "client", "version_source": {"component": "task"},
                "boundary": {"provider": "leaf", "paths": []}},
            "ui": {"path": "ui", "version_source": {"component": "client"},
                "boundary": {"provider": "leaf", "paths": []}},
            "unrelated": {"path": "unrelated", "boundary": {"provider": "leaf", "paths": []}},
        },
        "slices": {"versions": {"mode": "compat", "components": ["task", "client", "ui"]}},
    }
    # Non-root config must not change the meaning of repo_file.
    (root / "config" / "boundary.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(root)
    return config


@pytest.mark.parametrize("source", ["head", "index", "working-tree"])
@pytest.mark.parametrize("timed", [False, True])
def test_lock_publication_cannot_replace_a_repository_version_input(tmp_path, source, timed):
    _repository(tmp_path)
    manifest = tmp_path / "releases/task.json"
    before = manifest.read_bytes()
    index = _git(tmp_path, "ls-files", "--stage", "-z").stdout
    options = ("--timeout", "30") if timed else ()
    result = run_cli(
        tmp_path, "generate", "--source", source, "--config", "config/boundary.json",
        "--out", "releases/task.json", "--quiet", *options,
    )
    assert result.returncode == 2, result.stderr
    assert "version input" in result.stderr
    assert manifest.read_bytes() == before
    assert _git(tmp_path, "ls-files", "--stage", "-z").stdout == index


@pytest.mark.parametrize("alias", ["hardlink", "symlink", "parent-symlink"])
def test_lock_output_aliases_cannot_replace_a_repository_version_input(tmp_path, alias):
    _repository(tmp_path)
    manifest = tmp_path / "releases/task.json"
    before = manifest.read_bytes()
    output = tmp_path / "alias.json"
    try:
        if alias == "hardlink":
            os.link(manifest, output)
        elif alias == "symlink":
            output.symlink_to(manifest)
        else:
            directory = tmp_path / "version-alias"
            directory.symlink_to(manifest.parent, target_is_directory=True)
            output = directory / manifest.name
    except OSError as error:
        pytest.skip(f"alias creation unavailable: {error}")
    result = run_cli(
        tmp_path, "generate", "--source", "head", "--config", "config/boundary.json",
        "--out", output.relative_to(tmp_path).as_posix(), "--quiet",
    )
    assert result.returncode == 2, result.stderr
    assert "version input" in result.stderr
    assert manifest.read_bytes() == before
    assert output.read_bytes() == before


def test_public_api_refuses_version_input_output_but_valid_output_still_writes(tmp_path, monkeypatch):
    import boundver

    _repository(tmp_path)
    monkeypatch.chdir(tmp_path)
    manifest = tmp_path / "releases/task.json"
    before = manifest.read_bytes()
    with pytest.raises(ConfigError, match="version input"):
        boundver.generate(config_path="config/boundary.json", out_path="releases/task.json")
    assert manifest.read_bytes() == before
    candidate = boundver.generate(
        config_path="config/boundary.json", out_path="releases/generated.lock.json",
    )
    assert json.loads((tmp_path / "releases/generated.lock.json").read_text()) == candidate
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("arguments", [
    ("generate", "--dry-run", "--out", "releases/task.json"),
    ("generate", "--components", "ui", "--out", "releases/task.json"),
    ("verify", "--update", "--lock", "releases/task.json"),
])
def test_other_publication_modes_do_not_bypass_version_input_protection(tmp_path, arguments):
    _repository(tmp_path)
    manifest = tmp_path / "releases/task.json"
    before = manifest.read_bytes()
    result = run_cli(tmp_path, *arguments, "--source", "head", "--config", "config/boundary.json")
    assert result.returncode == 2, result.stderr
    assert "version input" in result.stderr
    assert manifest.read_bytes() == before


def test_sibling_shared_manifest_resolves_from_repository_not_config_directory(tmp_path):
    config = _repository(tmp_path)
    result = run_cli(tmp_path, "validate-config", "--source", "head", "--config", "config/boundary.json")
    assert result.returncode == 0, result.stdout + result.stderr
    lock = generate_lockfile(config, tmp_path, source="head")
    assert {lock["components"][name]["version"] for name in ("task", "task-2", "client", "ui")} == {"1.2.3"}
    assert verify_lockfile(config, lock, tmp_path, source="head") == []


def test_source_divergence_is_resolved_from_the_same_snapshot(tmp_path):
    config = _repository(tmp_path)
    path = tmp_path / "releases" / "task.json"
    path.write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    _git(tmp_path, "add", "releases/task.json")
    path.write_text('{"image":{"version":"3.0.0"}}', encoding="utf-8")
    for source, expected in [("head", "1.2.3"), ("index", "2.0.0"), ("working-tree", "3.0.0")]:
        assert _config.validate_config(config, tmp_path, source=source) == []
        lock = generate_lockfile(config, tmp_path, source=source)
        assert lock["components"]["task"]["version"] == expected
        assert lock["components"]["ui"]["version"] == expected


def test_external_version_change_moves_compat_not_component_exact(tmp_path):
    config = _repository(tmp_path)
    old = generate_lockfile(config, tmp_path, source="head")
    (tmp_path / "releases" / "task.json").write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    commit_all(tmp_path, "new shared image version")
    new = generate_lockfile(config, tmp_path, source="head")
    for name in ("task", "task-2", "client", "ui"):
        old_fp, new_fp = old["components"][name]["fingerprints"], new["components"][name]["fingerprints"]
        assert old_fp["exact"] == new_fp["exact"]
        assert old_fp["compat"] != new_fp["compat"]
    assert any("compat" in issue for issue in verify_lockfile(config, old, tmp_path, source="head"))


def test_changed_selection_includes_shared_readers_and_transitive_version_dependents(tmp_path):
    config = _repository(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip()
    (tmp_path / "releases" / "task.json").write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    commit_all(tmp_path, "version change outside component roots")
    assert changed_components_since_ref(config, tmp_path, base, source="head") == ["client", "task", "task-2", "ui"]


def test_cli_changed_from_reports_readers_while_still_verifying_full_lock(tmp_path):
    _repository(tmp_path)
    generated = run_cli(tmp_path, "generate", "--source", "head", "--config", "config/boundary.json")
    assert generated.returncode == 0, generated.stderr
    commit_all(tmp_path, "record lock")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip()
    (tmp_path / "releases" / "task.json").write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    commit_all(tmp_path)
    result = run_cli(tmp_path, "verify", "--source", "head", "--config", "config/boundary.json",
                     "--changed-from", base, "--format", "json")
    report = json.loads(result.stdout)
    assert result.returncode == 5, report
    assert report["changed_components"] == ["client", "task", "task-2", "ui"]
    assert report["components_filter"] == []
    assert any("compat" in issue for issue in report["issues"])


def test_selection_includes_input_owner_and_unknown_paths_remain_conservative(tmp_path):
    config = _repository(tmp_path)
    config["components"]["release-owner"] = {"path": "releases", "boundary": {"provider": "leaf", "paths": []}}
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip()
    (tmp_path / "releases" / "task.json").write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    commit_all(tmp_path)
    assert changed_components_since_ref(config, tmp_path, base, source="head") == ["client", "release-owner", "task", "task-2", "ui"]
    (tmp_path / "unknown.txt").write_text("unknown", encoding="utf-8")
    commit_all(tmp_path)
    assert changed_components_since_ref(config, tmp_path, base, source="head") == sorted(config["components"])


def test_legacy_component_file_also_selects_its_version_inheritors(tmp_path):
    config = _repository(tmp_path)
    config["components"]["task"]["version_source"] = {"file": "version.json", "field": "version"}
    path = tmp_path / "jobs" / "task" / "version.json"
    path.write_text('{"version":"1.0.0"}', encoding="utf-8")
    commit_all(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip()
    path.write_text('{"version":"2.0.0"}', encoding="utf-8")
    commit_all(tmp_path)
    assert changed_components_since_ref(config, tmp_path, base, source="head") == ["client", "task", "ui"]


def test_content_equivalent_revert_restores_every_lock_identity(tmp_path):
    config = _repository(tmp_path)
    original = generate_lockfile(config, tmp_path, source="head")
    path = tmp_path / "releases" / "task.json"
    initial = path.read_bytes()
    path.write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    commit_all(tmp_path)
    path.write_bytes(initial)
    commit_all(tmp_path)
    assert generate_lockfile(config, tmp_path, source="head") == original


@pytest.mark.parametrize("invalid", ["../escape.json", "/absolute.json", "C:/drive.json",
    "releases/task.json:ads", "releases\\task.json", "releases/*.json", "", " releases/task.json", None, 7])
def test_unsafe_root_paths_refused_without_schema_or_file_reads(tmp_path, monkeypatch, invalid):
    config = _repository(tmp_path)
    config["components"]["task"]["version_source"]["repo_file"] = invalid
    monkeypatch.setattr(_config, "_schema_engine_errors", lambda *_: [])
    errors = _config.validate_config(config, tmp_path, source="head")
    assert any("version_source.repo_file" in issue for issue in errors), errors
    calls = []
    assert extract_version(tmp_path, "jobs/task", {"repo_file": invalid, "field": "version"},
        read_file_fn=lambda name: calls.append(name)) is None
    assert calls == []


@pytest.mark.parametrize("other", ["file", "component", "constant", "git_tag_prefix"])
def test_repo_file_variant_is_mutually_exclusive(tmp_path, monkeypatch, other):
    config = _repository(tmp_path)
    source = config["components"]["task"]["version_source"]
    source[other] = "1.2.3"
    monkeypatch.setattr(_config, "_schema_engine_errors", lambda *_: [])
    assert any("Unknown field" in issue for issue in _config.validate_config(config, tmp_path, source="head"))


def test_missing_field_and_untracked_version_inputs_fail_closed(tmp_path):
    config = _repository(tmp_path)
    missing = copy.deepcopy(config)
    missing["components"]["task"]["version_source"].pop("field")
    assert any("no 'field'" in issue for issue in _config.validate_config(missing, tmp_path, source="head"))
    config["components"]["task"]["version_source"]["repo_file"] = "releases/untracked.json"
    (tmp_path / "releases" / "untracked.json").write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    assert any("tracked" in issue for issue in _config.validate_config(config, tmp_path, source="working-tree"))
    with pytest.raises(ConfigError):
        generate_lockfile(config, tmp_path, source="working-tree")


@pytest.mark.parametrize("body", ['{"image":{"version":1.2}}', '{"image":{"version":"not-semver"}}', '{broken'])
def test_non_text_malformed_or_invalid_version_cannot_generate_a_lock(tmp_path, body):
    config = _repository(tmp_path)
    (tmp_path / "releases" / "task.json").write_text(body, encoding="utf-8")
    commit_all(tmp_path)
    with pytest.raises(ConfigError, match="version"):
        generate_lockfile(config, tmp_path, source="head")


def test_committed_symlink_cannot_supply_a_version(tmp_path):
    config = _repository(tmp_path)
    oid = _git(tmp_path, "hash-object", "-w", "--stdin", input=b"task.json").stdout.decode().strip()
    _git(tmp_path, "update-index", "--add", "--cacheinfo", f"120000,{oid},releases/link.json")
    config["components"]["task"]["version_source"]["repo_file"] = "releases/link.json"
    snapshot = _capture_git_source_snapshot(tmp_path, "index")
    errors = _config.validate_config(config, tmp_path, source="index", snapshot=snapshot)
    assert any("regular file" in issue for issue in errors)
    with pytest.raises(ConfigError, match="symlink"):
        generate_lockfile(config, tmp_path, source="index", snapshot=snapshot)


def test_legacy_and_repository_variants_read_same_bytes_but_distinct_config_contracts(tmp_path):
    config = _repository(tmp_path)
    root_form = copy.deepcopy(config)
    root_form["components"]["task"]["version_source"]["repo_file"] = "jobs/task/version.json"
    (tmp_path / "jobs" / "task" / "version.json").write_text('{"image":{"version":"1.2.3"}}', encoding="utf-8")
    commit_all(tmp_path)
    legacy = copy.deepcopy(root_form)
    legacy["components"]["task"]["version_source"] = {"file": "version.json", "field": "image.version"}
    assert semantic_config_digest(root_form) != semantic_config_digest(legacy)
    root_lock = generate_lockfile(root_form, tmp_path, source="head")
    legacy_lock = generate_lockfile(legacy, tmp_path, source="head")
    assert root_lock["components"] == legacy_lock["components"]
    assert root_lock["slices"] == legacy_lock["slices"]
    assert version_file_repo_path("jobs/task", {"file": "version.json"}) == "jobs/task/version.json"


@pytest.mark.parametrize(("suffix", "body"), [
    ("json", '{"image":{"version":"4.5.6"}}'),
    ("toml", '[image]\nversion = "4.5.6"\n'),
    ("yaml", 'image:\n  version: "4.5.6"\n'),
])
def test_root_file_uses_existing_strict_format_extractors(tmp_path, suffix, body):
    if suffix == "yaml":
        pytest.importorskip("yaml")
    path = tmp_path / f"version.{suffix}"
    path.write_text(body, encoding="utf-8")
    assert extract_version(tmp_path, "component", {"repo_file": path.name, "field": "image.version"}) == "4.5.6"


def test_deleted_external_input_is_not_read_from_another_source(tmp_path):
    config = _repository(tmp_path)
    (tmp_path / "releases" / "task.json").unlink()
    assert _config.validate_config(config, tmp_path, source="head") == []
    assert any("not found" in issue for issue in _config.validate_config(config, tmp_path, source="working-tree"))
    with pytest.raises((ConfigError, ValueError)):
        generate_lockfile(config, tmp_path, source="working-tree")


def test_repository_version_reads_keep_the_ancestor_reparse_guard(tmp_path, monkeypatch):
    from boundver import _hashing
    from boundver._lockfile import _SourceAccessor

    _repository(tmp_path)
    with _SourceAccessor(tmp_path, "working-tree") as accessor:
        monkeypatch.setattr(_hashing, "_is_windows_reparse_point", lambda _: True)
        with pytest.raises(ValueError, match="ancestor"):
            accessor.version_read_file("releases/task.json")


@pytest.mark.parametrize("source", ["head", "index", "working-tree"])
@pytest.mark.parametrize("version", ["1.3.0", "2.0.0"])
def test_diagnostics_name_external_version_input_for_reader_and_inheritor(tmp_path, source, version):
    config = _repository(tmp_path)
    old = generate_lockfile(config, tmp_path, source="head")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip()
    (tmp_path / "releases" / "task.json").write_text(
        json.dumps({"image": {"version": version}}), encoding="utf-8")
    if source == "index":
        _git(tmp_path, "add", "releases/task.json")
    elif source == "head":
        commit_all(tmp_path)
    for name in ("task", "ui"):
        explanation = analyze_explain_changes(config, tmp_path, name, base_ref=base, source=source)
        assert explanation["error"] is None, explanation
        assert ("M", "releases/task.json") in explanation["changed"]
        drift = analyze_component_drift(config, old, tmp_path, name, source=source,
                                         diagnostic_base_ref=base)
        assert drift["changed_files_status"] == "ok", drift
        assert ("M", "releases/task.json") in drift["changed_files"]


def test_external_version_input_is_not_misclassified_by_recursive_boundary_glob(tmp_path):
    config = _repository(tmp_path)
    (tmp_path / "jobs" / "task" / "contract.json").write_text("{}", encoding="utf-8")
    config["components"]["task"]["boundary"] = {"provider": "path-hash", "paths": ["**/*.json"]}
    commit_all(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip()
    (tmp_path / "releases" / "task.json").write_text('{"image":{"version":"2.0.0"}}', encoding="utf-8")
    commit_all(tmp_path)
    explanation = analyze_explain_changes(config, tmp_path, "task", base_ref=base, source="head")
    assert explanation["error"] is None
    assert ("M", "releases/task.json") in explanation["changed"]
    assert explanation["boundary_changed"] == []


def test_json_schema_itself_rejects_nonliteral_repo_file_variants(tmp_path):
    pytest.importorskip("jsonschema")
    config = _repository(tmp_path)
    for invalid in ["releases/*.json", "releases/task.json:ads", "../version.json"]:
        config["components"]["task"]["version_source"]["repo_file"] = invalid
        assert _config._schema_engine_errors(config, _config._load_config_schema(tmp_path))
