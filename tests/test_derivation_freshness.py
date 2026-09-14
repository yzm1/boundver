from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from boundver._config import validate_config
from boundver._lockfile import semantic_config_digest
from tests._repo_fixtures import init_git_repo


ROOT = Path(__file__).resolve().parents[1]
INPUT = "infrastructure/template.yaml"
OUTPUT = "infrastructure/openapi.generated.yaml"
EVIDENCE = "infrastructure/openapi.boundver-derivation.json"


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


def _config() -> dict:
    return {
        "project": "derivation-fixture",
        "derivations": {
            "public-api": {
                "inputs": [INPUT],
                "outputs": [OUTPUT],
                "evidence": EVIDENCE,
                "generator": "sam-openapi-fixture/v1",
            }
        },
        "components": {
            "api": {
                "path": "infrastructure",
                "boundary": {
                    "provider": "openapi",
                    "paths": ["openapi.generated.yaml"],
                },
            }
        },
        "slices": {},
    }


def _write_source(root: Path, config: dict | None = None) -> None:
    (root / "infrastructure").mkdir(parents=True, exist_ok=True)
    (root / INPUT).write_text(
        "Resources:\n  Function:\n    Type: AWS::Serverless::Function\n",
        encoding="utf-8",
    )
    (root / OUTPUT).write_text(
        "openapi: 3.0.0\ninfo:\n  title: fixture\n  version: 1.0.0\npaths: {}\n",
        encoding="utf-8",
    )
    (root / "generator.py").write_text(
        "from pathlib import Path\nPath('GENERATOR-RAN').write_text('bad')\n",
        encoding="utf-8",
    )
    (root / "boundary.config.json").write_text(
        json.dumps(config or _config(), indent=2) + "\n",
        encoding="utf-8",
    )


def _commit(root: Path, message: str) -> None:
    subprocess.run(
        ["git", "add", "--all"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _ready_repository(root: Path) -> None:
    init_git_repo(root, initial_branch="main")
    _write_source(root)
    _commit(root, "add generated API")

    recorded = _run(root, "record-derivation", "public-api", "--source", "head")
    assert recorded.returncode == 0, recorded.stderr
    assert "Recorded derivation 'public-api'" in recorded.stdout
    _commit(root, "record derivation")

    generated = _run(root, "generate", "--source", "head", "--quiet")
    assert generated.returncode == 0, generated.stderr
    _commit(root, "lock boundary")
    verified = _run(root, "verify", "--source", "head", "--quiet")
    assert verified.returncode == 0, verified.stderr
    assert not (root / "GENERATOR-RAN").exists()


def test_why_keeps_unrelated_derivation_owners_in_its_resolution_context(
    tmp_path: Path,
) -> None:
    config = _config()
    config["components"]["worker"] = {
        "path": "worker",
        "boundary": {"provider": "leaf", "paths": []},
    }
    init_git_repo(tmp_path, initial_branch="main")
    _write_source(tmp_path, config)
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "main.py").write_text("pass\n", encoding="utf-8")
    _commit(tmp_path, "add generated API and worker")

    recorded = _run(tmp_path, "record-derivation", "public-api", "--source", "head")
    assert recorded.returncode == 0, recorded.stderr
    _commit(tmp_path, "record derivation")
    generated = _run(tmp_path, "generate", "--source", "head", "--quiet")
    assert generated.returncode == 0, generated.stderr
    _commit(tmp_path, "lock boundaries")

    explained = _run(tmp_path, "why", "worker", "--source", "head")

    assert explained.returncode == 0, explained.stderr
    assert "could not compute current fingerprints" not in explained.stderr
    assert "Status: UP TO DATE" in explained.stdout


def _change(root: Path, kind: str) -> None:
    if kind == "input":
        (root / INPUT).write_text(
            "Resources:\n  Function:\n    Type: AWS::Serverless::Api\n",
            encoding="utf-8",
        )
    else:
        (root / OUTPUT).write_text(
            "openapi: 3.0.0\ninfo:\n  title: changed\n  version: 1.0.0\npaths: {}\n",
            encoding="utf-8",
        )


@pytest.mark.parametrize("source", ["head", "index", "working-tree"])
@pytest.mark.parametrize("kind", ["input", "output"])
def test_stale_derivation_fails_closed_in_every_source_mode(
    tmp_path: Path,
    source: str,
    kind: str,
) -> None:
    _ready_repository(tmp_path)
    _change(tmp_path, kind)
    if source in {"head", "index"}:
        subprocess.run(
            ["git", "add", INPUT if kind == "input" else OUTPUT],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
    if source == "head":
        subprocess.run(
            ["git", "commit", "-m", f"stale {kind}"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    result = _run(tmp_path, "verify", "--source", source)

    assert result.returncode == 2
    expected = "inputs are stale" if kind == "input" else "outputs changed"
    assert expected in result.stdout
    assert "run the trusted generator" in result.stdout
    assert not (tmp_path / "GENERATOR-RAN").exists()


def test_selected_source_controls_freshness_without_moving_state(tmp_path: Path) -> None:
    _ready_repository(tmp_path)
    original = (tmp_path / INPUT).read_text(encoding="utf-8")
    _change(tmp_path, "input")
    subprocess.run(
        ["git", "add", INPUT],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / INPUT).write_text(original, encoding="utf-8")

    head = _run(tmp_path, "verify", "--source", "head", "--quiet")
    index = _run(tmp_path, "verify", "--source", "index")
    working = _run(tmp_path, "verify", "--source", "working-tree", "--quiet")

    assert head.returncode == 0, head.stderr
    assert index.returncode == 2
    assert "inputs are stale for the selected index source" in index.stdout
    assert working.returncode == 0, working.stderr


def test_index_bootstrap_records_then_verifies_without_a_partial_lock(
    tmp_path: Path,
) -> None:
    init_git_repo(tmp_path, initial_branch="main")
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    _commit(tmp_path, "initialize")
    _write_source(tmp_path)
    subprocess.run(
        ["git", "add", "boundary.config.json", INPUT, OUTPUT, "generator.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    recorded = _run(
        tmp_path,
        "record-derivation",
        "public-api",
        "--source",
        "index",
    )
    assert recorded.returncode == 0, recorded.stderr
    assert (tmp_path / EVIDENCE).is_file()
    subprocess.run(
        ["git", "add", EVIDENCE],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    generated = _run(tmp_path, "generate", "--source", "index", "--quiet")
    assert generated.returncode == 0, generated.stderr
    subprocess.run(
        ["git", "add", "boundary.lock.json"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    verified = _run(tmp_path, "verify", "--source", "index", "--quiet")

    assert verified.returncode == 0, verified.stderr
    lock = json.loads((tmp_path / "boundary.lock.json").read_text(encoding="utf-8"))
    assert lock["components"]["api"]["fingerprints"]["boundary"] is not None


@pytest.mark.parametrize(
    ("replacement", "expected"),
    (
        ("{not-json\n", "not valid strict JSON"),
        (
            '{"schema":"boundver-derivation/v1",'
            '"schema":"boundver-derivation/v1"}',
            "not valid strict JSON",
        ),
        (
            json.dumps(
                {
                    "schema": "boundver-derivation/v1",
                    "derivation": "public-api",
                    "generator": "sam-openapi-fixture/v1",
                    "inputs": {"files": 1, "digest": "0" * 64},
                }
            ),
            "root fields do not match",
        ),
    ),
)
def test_missing_or_malformed_evidence_is_actionable(
    tmp_path: Path,
    replacement: str,
    expected: str,
) -> None:
    _ready_repository(tmp_path)
    (tmp_path / EVIDENCE).write_text(replacement, encoding="utf-8")

    validated = _run(tmp_path, "validate-config")
    generated = _run(tmp_path, "generate", "--source", "working-tree")

    assert validated.returncode == 2
    assert expected in validated.stdout
    assert generated.returncode == 2
    assert expected in generated.stderr


def test_missing_evidence_is_rejected_but_recording_can_bootstrap_it(
    tmp_path: Path,
) -> None:
    init_git_repo(tmp_path, initial_branch="main")
    _write_source(tmp_path)
    _commit(tmp_path, "add generated API")

    invalid = _run(tmp_path, "validate-config")
    recorded = _run(tmp_path, "record-derivation", "public-api", "--source", "head")

    assert invalid.returncode == 2
    assert "evidence is missing" in invalid.stdout
    assert recorded.returncode == 0, recorded.stderr


@pytest.mark.parametrize(
    ("mutate", "expected"),
    (
        (
            lambda derivation: derivation.__setitem__("unknown", True),
            "Additional properties are not allowed ('unknown' was unexpected)",
        ),
        (
            lambda derivation: derivation.__setitem__("inputs", []),
            "inputs must be a non-empty array of strings",
        ),
        (
            lambda derivation: derivation.__setitem__("outputs", ["../api.yaml"]),
            "outputs[0] is not a safe repository-relative selector",
        ),
        (
            lambda derivation: derivation.__setitem__(
                "evidence", "infrastructure/evidence.json"
            ),
            "evidence must end with .boundver-derivation.json",
        ),
        (
            lambda derivation: derivation.__setitem__(
                "generator", " sam-openapi-fixture/v1"
            ),
            "generator must not have surrounding whitespace",
        ),
    ),
)
def test_static_derivation_contract_rejects_unsafe_or_ambiguous_fields(
    tmp_path: Path,
    mutate,
    expected: str,
) -> None:
    config = _config()
    mutate(config["derivations"]["public-api"])

    errors = validate_config(config, tmp_path, validate_provider_runtime=False)

    assert any(expected in error for error in errors), errors


def test_duplicate_evidence_paths_are_rejected_statically(tmp_path: Path) -> None:
    config = _config()
    config["derivations"]["duplicate"] = copy.deepcopy(
        config["derivations"]["public-api"]
    )

    errors = validate_config(config, tmp_path, validate_provider_runtime=False)

    assert any("use the same evidence path" in error for error in errors), errors


def test_output_must_be_unambiguous_and_selected_by_a_boundary(tmp_path: Path) -> None:
    init_git_repo(tmp_path, initial_branch="main")
    config = _config()
    config["derivations"]["duplicate"] = {
        **config["derivations"]["public-api"],
        "evidence": "infrastructure/duplicate.boundver-derivation.json",
    }
    _write_source(tmp_path, config)
    _commit(tmp_path, "ambiguous derivations")

    ambiguous = _run(
        tmp_path,
        "record-derivation",
        "public-api",
        "--source",
        "head",
    )
    config["derivations"].pop("duplicate")
    config["derivations"]["public-api"]["outputs"] = ["generator.py"]
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    _commit(tmp_path, "unselected output")
    unselected = _run(
        tmp_path,
        "record-derivation",
        "public-api",
        "--source",
        "head",
    )

    assert ambiguous.returncode == 2
    assert "claimed by both" in ambiguous.stderr
    assert unselected.returncode == 2
    assert "not selected by any component boundary" in unselected.stderr


@pytest.mark.parametrize("field", ["inputs", "outputs"])
def test_every_derivation_selector_must_match_the_selected_source(
    tmp_path: Path,
    field: str,
) -> None:
    init_git_repo(tmp_path, initial_branch="main")
    config = _config()
    config["derivations"]["public-api"][field] = ["missing/**/*.yaml"]
    _write_source(tmp_path, config)
    _commit(tmp_path, "unmatched selector")

    result = _run(
        tmp_path,
        "record-derivation",
        "public-api",
        "--source",
        "head",
    )

    assert result.returncode == 2
    assert f"{field} selector matched no tracked file" in result.stderr


def test_changed_generator_identity_requires_fresh_evidence(tmp_path: Path) -> None:
    _ready_repository(tmp_path)
    config = json.loads(
        (tmp_path / "boundary.config.json").read_text(encoding="utf-8")
    )
    config["derivations"]["public-api"]["generator"] = "sam-openapi-fixture/v2"
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "verify", "--source", "working-tree")

    assert result.returncode == 2
    assert "generator identity changed" in result.stdout
    assert not (tmp_path / "GENERATOR-RAN").exists()


def test_verify_update_does_not_rewrite_lock_when_derivation_is_stale(
    tmp_path: Path,
) -> None:
    _ready_repository(tmp_path)
    original_lock = (tmp_path / "boundary.lock.json").read_bytes()
    _change(tmp_path, "input")

    result = _run(
        tmp_path,
        "verify",
        "--source",
        "working-tree",
        "--update",
    )

    assert result.returncode == 2
    assert "inputs are stale" in result.stdout
    assert (tmp_path / "boundary.lock.json").read_bytes() == original_lock


def test_oversized_evidence_fails_before_parsing(tmp_path: Path) -> None:
    _ready_repository(tmp_path)
    (tmp_path / EVIDENCE).write_bytes(b" " * (64 * 1024 + 1))

    result = _run(tmp_path, "verify", "--source", "working-tree")

    assert result.returncode == 2
    assert "file too large (65537 bytes)" in result.stdout


def test_index_evidence_must_be_a_regular_git_file(tmp_path: Path) -> None:
    _ready_repository(tmp_path)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=tmp_path,
        input="some-target",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"120000,{blob},{EVIDENCE}"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

    result = _run(tmp_path, "verify", "--source", "index")

    assert result.returncode == 2
    assert "evidence must be a regular Git file" in result.stdout


def test_derivation_declaration_is_semantic_and_set_order_is_not() -> None:
    config = _config()
    config["derivations"]["public-api"]["inputs"].append("generator.py")
    reordered = copy.deepcopy(config)
    reordered["derivations"]["public-api"]["inputs"].reverse()
    changed = copy.deepcopy(config)
    changed["derivations"]["public-api"]["generator"] = "sam-openapi-fixture/v2"

    assert semantic_config_digest(config) == semantic_config_digest(reordered)
    assert semantic_config_digest(config) != semantic_config_digest(changed)


def test_recorded_evidence_conforms_to_the_published_schema(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    init_git_repo(tmp_path, initial_branch="main")
    _write_source(tmp_path)
    _commit(tmp_path, "add generated API")
    result = _run(tmp_path, "record-derivation", "public-api", "--source", "head")
    assert result.returncode == 0, result.stderr
    evidence = json.loads((tmp_path / EVIDENCE).read_text(encoding="utf-8"))
    schema = json.loads(
        (ROOT / "spec" / "derivation.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(evidence, schema)
