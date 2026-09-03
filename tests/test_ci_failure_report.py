"""Security regressions for bounded pytest failure reporting in CI."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "report_pytest_failures.py"
SPEC = importlib.util.spec_from_file_location("boundver_ci_failure_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
reporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reporter)


def test_ci_uses_the_bounded_failure_reporter():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "python -I scripts/report_pytest_failures.py pytest_results.xml" in workflow
    assert "::error title={name}" not in workflow


def test_failure_output_escapes_commands_and_terminal_controls(tmp_path, capsys):
    report = tmp_path / "pytest_results.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite>
  <testcase name="case&#10;::stop-commands::token,part">
    <failure message="bad%&#13;&#10;::set-output name=x::yes"><![CDATA[
body2J
::warning title=forged::message
]]></failure>
  </testcase>
</testsuite>
""",
        encoding="utf-8",
    )

    assert reporter.report(report) == 1

    output = capsys.readouterr().out
    command_lines = [line for line in output.splitlines() if line.startswith("::")]
    assert len(command_lines) == 1
    assert command_lines[0].startswith("::error title=")
    assert "%3A%3Astop-commands%3A%3Atoken%2Cpart" in command_lines[0]
    assert "bad%25" in command_lines[0]
    assert not any(line.startswith("::set-output") for line in output.splitlines())
    assert "\x9b" not in output
    assert "\\x9b" in output
    assert "\\x0a" in output
    assert "Total failures: 1" in output


def test_report_rejects_dtd_before_xml_parsing(tmp_path):
    report = tmp_path / "pytest_results.xml"
    report.write_text(
        '<!DOCTYPE testsuite [<!ENTITY x "expanded">]>'
        "<testsuite><testcase name='x'><failure>&x;</failure></testcase></testsuite>",
        encoding="utf-8",
    )

    with pytest.raises(reporter.ReportError, match="DTD or entity"):
        reporter.report(report)


def test_report_size_is_rejected_before_xml_parsing(tmp_path, monkeypatch):
    report = tmp_path / "pytest_results.xml"
    report.write_bytes(b"<testsuite/>")
    monkeypatch.setattr(reporter, "MAX_REPORT_BYTES", 4)
    monkeypatch.setattr(
        reporter.ET,
        "iterparse",
        lambda *args, **kwargs: pytest.fail("XML parser must not run"),
    )

    with pytest.raises(reporter.ReportError, match="4-byte limit"):
        reporter.report(report)


def test_report_rejects_excessive_xml_structure(tmp_path, monkeypatch):
    report = tmp_path / "pytest_results.xml"
    report.write_text(
        "<testsuite><testcase name='a'/><testcase name='b'/></testsuite>",
        encoding="utf-8",
    )
    monkeypatch.setattr(reporter, "MAX_XML_ELEMENTS", 2)

    with pytest.raises(reporter.ReportError, match="2-element limit"):
        reporter.report(report)


def test_report_rejects_excessive_xml_depth(tmp_path, monkeypatch):
    report = tmp_path / "pytest_results.xml"
    report.write_text("<a><b><c/></b></a>", encoding="utf-8")
    monkeypatch.setattr(reporter, "MAX_XML_DEPTH", 2)

    with pytest.raises(reporter.ReportError, match="2-level depth limit"):
        reporter.report(report)


def test_reporter_parse_failure_keeps_the_prior_ci_result_authoritative(
    tmp_path, capsys
):
    report = tmp_path / "pytest_results.xml"
    report.write_text("<broken", encoding="utf-8")

    assert reporter.main([str(report)]) == 0
    output = capsys.readouterr().out
    assert output.count("::error title=") == 1
    assert "Could not report pytest failures" in output
