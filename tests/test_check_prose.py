"""Tests for the advisory, deterministic documentation prose report."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

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


def test_finding_count_has_an_operation_budget() -> None:
    text = "\n\n".join("We do this in order to test." for _ in range(4))

    with pytest.raises(ValueError, match="finding count exceeds"):
        checker.inspect_text(text, "guide.md", max_findings=3)


def test_text_report_escapes_terminal_controls(tmp_path: Path, capsys) -> None:
    path = tmp_path / "guide.md"
    control = "\x1b]8;;https://evil.invalid\x07"
    path.write_text(
        f"We do this in order to print {control}unsafe text.\n",
        encoding="utf-8",
    )

    assert checker.main([str(path)]) == 0
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

    assert checker.main([str(path)]) == 2
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

    assert checker.main([str(path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "boundver-prose-report/v1"
    assert payload["advisory"] is True
    assert payload["finding_count"] == 1

    assert checker.main([str(path), "--fail-on-findings"]) == 1
