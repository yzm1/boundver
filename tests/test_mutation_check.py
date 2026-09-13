"""Integrity checks for the curated release mutation gate."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mutation_check.py"
CATALOG = ROOT / "spec" / "release-mutations.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("boundver_mutation_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _minimal_mutant(identifier: str = "MUT-TEST-001") -> dict:
    return {
        "id": identifier,
        "subsystem": "testing",
        "label": "example fault",
        "file": "src/boundver/__init__.py",
        "tests": ["tests/test_mutation_check.py"],
        "obligations": ["OBL-TEST-001"],
        "search": "before",
        "replace": "after",
    }


def _write_catalog(path: Path, mutants: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "record": "boundver-release-mutation-catalog/v1",
                "mutants": mutants,
            }
        ),
        encoding="utf-8",
    )


def test_public_release_catalog_is_small_killable_and_current() -> None:
    runner = _load_script()
    document = runner.load_catalog(CATALOG)
    mutants = document["mutants"]
    identifiers = [mutant["id"] for mutant in mutants]

    assert document["record"] == runner.RECORD
    assert document["private_catalog_mutants"] == 750
    assert len(mutants) == 12
    assert identifiers == sorted(identifiers)
    assert len(identifiers) == len(set(identifiers))
    assert all(mutant.get("expected", "killed") == "killed" for mutant in mutants)
    for mutant in mutants:
        source = (ROOT / mutant["file"]).read_text(encoding="utf-8")
        assert source.count(mutant["search"]) == 1, mutant["id"]
        assert all(
            (ROOT / test.split("::", 1)[0]).is_file() for test in mutant["tests"]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("file", "../outside.py"),
        ("file", "src/../outside.py"),
        ("file", "tests/wrong.py"),
        ("tests", ["../outside.py"]),
        ("tests", ["src/boundver/core.py"]),
    ],
)
def test_catalog_rejects_paths_outside_the_mutation_contract(
    tmp_path: Path, field: str, value: object
) -> None:
    runner = _load_script()
    mutant = _minimal_mutant()
    mutant[field] = value
    catalog = tmp_path / "catalog.json"
    _write_catalog(catalog, [mutant])

    with pytest.raises(ValueError, match="path|inside src/boundver"):
        runner.load_catalog(catalog)


def test_catalog_rejects_duplicate_mutant_ids(tmp_path: Path) -> None:
    runner = _load_script()
    catalog = tmp_path / "catalog.json"
    _write_catalog(catalog, [_minimal_mutant(), _minimal_mutant()])

    with pytest.raises(ValueError, match="duplicate mutant id"):
        runner.load_catalog(catalog)


def test_catalog_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    runner = _load_script()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        '{"record":"boundver-release-mutation-catalog/v1",'
        '"record":"boundver-release-mutation-catalog/v1","mutants":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        runner.load_catalog(catalog)


def test_checkout_import_accepts_only_this_checkout() -> None:
    runner = _load_script()
    expected = (runner.ROOT / "src" / "boundver" / "__init__.py").resolve()
    accepted = subprocess.CompletedProcess([], 0, f"{expected}\n", "")
    rejected = subprocess.CompletedProcess([], 0, "C:/another/boundver/__init__.py\n", "")

    with mock.patch.object(runner.subprocess, "run", return_value=accepted):
        runner.require_checkout_import()
    with mock.patch.object(runner.subprocess, "run", return_value=rejected), pytest.raises(
        ValueError, match="different checkout"
    ):
        runner.require_checkout_import()


def test_selection_composes_identifier_label_and_subsystem_filters() -> None:
    runner = _load_script()
    first = _minimal_mutant("MUT-TEST-001")
    second = _minimal_mutant("MUT-OTHER-001")
    second["subsystem"] = "other"
    second["label"] = "different fault"
    catalog = {"mutants": [first, second]}

    assert runner.select(catalog, "MUT-TEST-001", None) == [first]
    assert runner.select(catalog, "example", None) == [first]
    assert runner.select(catalog, "different", "other") == [second]
    assert runner.select(catalog, None, "absent") == []
