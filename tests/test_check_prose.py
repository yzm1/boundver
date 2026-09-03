"""Tests for the advisory, deterministic documentation prose report."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_checker():
    path = REPO_ROOT / "scripts" / "check_prose.py"
    spec = importlib.util.spec_from_file_location("boundver_prose_checker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def test_report_skips_code_and_flags_only_visible_prose() -> None:
    text = """# Direct heading

This sentence exists in order to demonstrate one avoidable phrase.

~~~text
It is important to note that fenced examples are not prose findings.
~~~

Short and direct.
"""

    findings = checker.inspect_text(text, "guide.md")

    assert [(finding.line, finding.rule) for finding in findings] == [
        (3, "avoidable-phrase")
    ]


def test_report_ignores_badge_markup() -> None:
    badges = " ".join(
        f"[![badge {index}](https://img.example/{index})](https://example/{index})"
        for index in range(20)
    )

    assert checker.inspect_text(badges, "README.md", max_sentence_words=10) == []


def test_long_sentence_threshold_is_explicit() -> None:
    findings = checker.inspect_text(
        "One two three four five six seven eight nine ten eleven.",
        "guide.md",
        max_sentence_words=10,
    )

    assert len(findings) == 1
    assert findings[0].rule == "long-sentence"
    assert "11 words" in findings[0].message


def test_report_points_to_the_source_line_containing_the_finding() -> None:
    findings = checker.inspect_text(
        "Opening sentence.\nWe do this in order to test.\n",
        "guide.md",
    )
    long_sentence = checker.inspect_text(
        "One two three four five\nsix seven eight nine ten eleven.\n",
        "guide.md",
        max_sentence_words=10,
    )

    assert [(finding.line, finding.rule) for finding in findings] == [
        (2, "avoidable-phrase")
    ]
    assert [(finding.line, finding.rule) for finding in long_sentence] == [
        (2, "long-sentence")
    ]


def test_finding_count_has_an_operation_budget() -> None:
    text = "\n\n".join("We do this in order to test." for _ in range(4))

    with pytest.raises(ValueError, match="finding count exceeds"):
        checker.inspect_text(text, "guide.md", max_findings=3)


@pytest.mark.skipif(os.name == "nt", reason="Windows ctime is creation time")
def test_file_snapshot_includes_change_time_on_posix() -> None:
    common = {
        "st_dev": 1,
        "st_ino": 2,
        "st_size": 3,
        "st_mtime_ns": 4,
    }

    assert checker._file_snapshot(
        SimpleNamespace(**common, st_ctime_ns=5)
    ) != checker._file_snapshot(SimpleNamespace(**common, st_ctime_ns=6))


@pytest.mark.skipif(os.name != "nt", reason="Windows stat semantics")
def test_file_snapshot_uses_stable_windows_birth_time() -> None:
    common = {
        "st_dev": 1,
        "st_ino": 2,
        "st_size": 3,
        "st_mtime_ns": 4,
        "st_birthtime_ns": 5,
    }

    assert checker._file_snapshot(
        SimpleNamespace(**common, st_ctime_ns=6)
    ) == checker._file_snapshot(SimpleNamespace(**common, st_ctime_ns=7))


def test_markdown_discovery_stops_at_the_file_budget(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"{index}.md").write_text("Text.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="2-file budget"):
        checker._discover_markdown_paths(tmp_path, max_files=2)


def test_markdown_discovery_stops_at_the_entry_budget(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"{index}.txt").write_text("Text.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="2-entry budget"):
        checker._discover_markdown_paths(
            tmp_path,
            max_files=3,
            max_entries=2,
        )


def test_inspection_bounds_an_iterable_before_materializing_it() -> None:
    def paths():
        for index in range(checker.MAX_FILES + 1):
            yield Path(f"never-read-{index}.md")

    with pytest.raises(ValueError, match="more than 512 files"):
        checker.inspect_paths(paths())


def test_text_report_escapes_terminal_controls(tmp_path: Path, capsys) -> None:
    path = tmp_path / "guide.md"
    control = "\x1b]8;;https://evil.invalid\x07"
    path.write_text(
        f"We do this in order to print {control}unsafe text.\n",
        encoding="utf-8",
    )

    assert checker.main([str(path), "--allow-external-paths"]) == 0
    output = capsys.readouterr().out
    assert control not in output
    assert "\\u001b" in output
    assert "\\u0007" in output


def test_output_size_is_checked_before_printing(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    path = tmp_path / "guide.md"
    path.write_text("We do this in order to test.\n", encoding="utf-8")
    monkeypatch.setattr(checker, "MAX_OUTPUT_BYTES", 16)

    assert checker.main([str(path), "--allow-external-paths"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "output byte budget" in captured.err


def test_cli_is_advisory_unless_failure_is_explicit(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "guide.md"
    path.write_text(
        "We do this in order to produce a deliberately advisory finding.\n",
        encoding="utf-8",
    )

    assert checker.main(
        [str(path), "--allow-external-paths", "--format", "json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "boundver-prose-report/v1"
    assert payload["advisory"] is True
    assert payload["finding_count"] == 1

    assert checker.main(
        [str(path), "--allow-external-paths", "--fail-on-findings"]
    ) == 1


def test_cli_rejects_external_paths_without_explicit_opt_in(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "guide.md"
    path.write_text("Short and direct.\n", encoding="utf-8")

    assert checker.main([str(path)]) == 2
    assert "--allow-external-paths" in capsys.readouterr().err


def test_bounded_read_binds_to_the_validated_file_before_opening(
    tmp_path: Path, monkeypatch
) -> None:
    validated = tmp_path / "validated.md"
    replacement = tmp_path / "replacement.md"
    validated.write_text("Expected.\n", encoding="utf-8")
    replacement.write_text("Secret replacement.\n", encoding="utf-8")

    def fail_open(*_args, **_kwargs):
        raise AssertionError("mismatched input must be rejected before open")

    monkeypatch.setattr(checker, "_open_read_descriptor", fail_open)
    with pytest.raises(ValueError, match="changed before reading"):
        checker._read_bounded(
            replacement,
            "replacement.md",
            validated.lstat(),
        )


def test_bounded_read_checks_the_full_snapshot_after_open(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "guide.md"
    path.write_text("First.\n", encoding="utf-8")
    validated = path.lstat()
    real_open = checker._open_read_descriptor

    def mutate_then_open(candidate: Path) -> int:
        candidate.write_text("Other.\n", encoding="utf-8")
        changed_mtime = validated.st_mtime_ns + 1_000_000_000
        os.utime(
            candidate,
            ns=(validated.st_atime_ns, changed_mtime),
        )
        return real_open(candidate)

    monkeypatch.setattr(checker, "_open_read_descriptor", mutate_then_open)
    with pytest.raises(ValueError, match="changed while opening"):
        checker._read_bounded(path, "guide.md", validated)


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing contract")
def test_windows_reader_rejects_an_existing_writer(tmp_path: Path) -> None:
    path = tmp_path / "guide.md"
    path.write_text("Expected.\n", encoding="utf-8")

    with path.open("r+b"):
        with pytest.raises(OSError, match="excluding concurrent writers"):
            checker._open_read_descriptor(path)


def test_cli_rejects_symlinks_without_reading_the_target(
    tmp_path: Path, capsys
) -> None:
    target = tmp_path / "target.md"
    target.write_text("Secret in order to remain unread.\n", encoding="utf-8")
    link = REPO_ROOT / "docs" / f".prose-test-link-{uuid.uuid4().hex}.md"
    created = False
    try:
        try:
            link.symlink_to(target)
            created = True
        except OSError as exc:
            pytest.skip(f"symlinks are unavailable: {exc}")
        assert checker.main([str(link)]) == 2
        captured = capsys.readouterr()
        assert "symlinks and non-regular files are not read" in captured.err
        assert "Secret" not in captured.out
        assert "Secret" not in captured.err
    finally:
        if created:
            link.unlink(missing_ok=True)


def test_failure_diagnostic_escapes_terminal_controls(
    monkeypatch, capsys
) -> None:
    control = "\x1b]8;;https://evil.invalid\x07"

    def fail(*_args, **_kwargs):
        raise ValueError(f"unsafe {control} diagnostic")

    monkeypatch.setattr(checker, "inspect_paths", fail)
    assert checker.main([]) == 2
    captured = capsys.readouterr()
    assert control not in captured.err
    assert "\\u001b" in captured.err
    assert "\\u0007" in captured.err
