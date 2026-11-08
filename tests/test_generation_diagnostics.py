"""Generation reporting is local, bounded, non-authoritative and stderr-only."""

import io
import json
from contextlib import redirect_stderr

import pytest

from boundver import core
from boundver._cli_parser import build_parser
from boundver._generation_diagnostics import (
    GenerationDiagnostics, MAX_GENERATION_COMPONENT_ROWS,
    MAX_GENERATION_DIAGNOSTIC_BYTES, MAX_GENERATION_PHASE_ROWS,
    MAX_GENERATION_PROGRESS_LINES,
)
from boundver._utils import ConfigError
from tests._parity import run_cli, run_cli_in_process
from tests._repo_fixtures import commit_all, init_git_repo


@pytest.fixture
def repository(tmp_path):
    init_git_repo(tmp_path)
    for name in ("api", "client"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "schema.json").write_text("{}", encoding="utf-8")
    config = {"project": "generation-diagnostics", "components": {
        name: {"path": name, "boundary": {"provider": "path-hash", "paths": ["schema.json"]}}
        for name in ("api", "client")
    }}
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    generated = run_cli(tmp_path, "generate", "--source", "head", "--quiet")
    assert generated.returncode == 0, generated.stderr
    commit_all(tmp_path)
    return tmp_path


@pytest.mark.parametrize("flags", [("--progress",), ("--timing",), ("--progress", "--timing")])
def test_explicit_reporting_leaves_json_and_lock_unchanged(repository, flags):
    before = (repository / "boundary.lock.json").read_bytes()
    plain = run_cli(repository, "generate", "--source", "head", "--dry-run", "--format", "json")
    reported = run_cli(repository, "generate", "--source", "head", "--dry-run", "--format", "json",
                       "--quiet", *flags)
    assert reported.returncode == plain.returncode == 0
    assert reported.stdout == plain.stdout
    assert json.loads(reported.stdout)["schema"] == "boundary-lock/v4"
    assert not plain.stderr
    assert "publication=dry-run" in reported.stderr
    assert "outcome=success" in reported.stderr
    assert ("Generation: capture-source started" in reported.stderr) == ("--progress" in flags)
    assert ("Timing component api:" in reported.stderr) == ("--timing" in flags)
    assert (repository / "boundary.lock.json").read_bytes() == before


def test_component_subset_still_reports_full_integrity_recomputation(repository):
    result = run_cli(repository, "generate", "--source", "head", "--components", "api",
                     "--dry-run", "--timing", "--progress", "--quiet")
    assert result.returncode == 0, result.stderr
    assert "Timing component api:" in result.stderr
    assert "Timing component client:" in result.stderr
    assert "components_attempted=2" in result.stderr


def test_published_generation_reports_written_only_after_success(repository):
    result = run_cli(repository, "generate", "--source", "head", "--timing", "--quiet")
    assert result.returncode == 0, result.stderr
    assert "publication=written" in result.stderr
    assert "Timing phase publish:" in result.stderr


def test_invalid_source_state_does_not_claim_to_publish(repository):
    result = run_cli(repository, "generate", "--config", "missing.json", "--timing", "--quiet")
    assert result.returncode == 2
    assert "outcome=failed" in result.stderr
    assert "publication=not-attempted" in result.stderr


def test_write_failure_is_not_mislabeled_as_definitely_unpublished(repository, monkeypatch):
    before = (repository / "boundary.lock.json").read_bytes()
    def fail(*args):
        raise ConfigError("injected publication failure")
    monkeypatch.setattr(core, "_write_lockfile_atomic", fail)
    result = run_cli_in_process(repository, "generate", "--source", "head", "--timing", "--quiet")
    assert result.returncode == 2
    assert "publication=attempted-outcome-unconfirmed" in result.stderr
    assert "outcome=failed" in result.stderr
    assert (repository / "boundary.lock.json").read_bytes() == before


def test_cancellation_before_publication_preserves_lock_and_reports_cancelled(repository, monkeypatch, capsys):
    before = (repository / "boundary.lock.json").read_bytes()
    def cancel(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(core, "generate_lockfile", cancel)
    args = build_parser(version="test", epilog="").parse_args(["generate", "--timing", "--progress"])
    with pytest.raises(KeyboardInterrupt):
        core._cmd_generate(args, repository)
    stderr = capsys.readouterr().err
    assert "outcome=cancelled" in stderr
    assert "publication=not-attempted" in stderr
    assert (repository / "boundary.lock.json").read_bytes() == before


def test_diagnostics_have_independent_storage_and_output_budgets():
    output = io.StringIO()
    observer = GenerationDiagnostics(progress=True, timing=True)
    with redirect_stderr(output):
        for index in range(1000):
            with observer.component("\U0001f600" * 1000 + str(index)):
                pass
        for index in range(100):
            with observer.phase(str(index)):
                pass
        observer.finish("success")
    assert len(observer.components) == MAX_GENERATION_COMPONENT_ROWS
    assert len(observer.phases) == MAX_GENERATION_PHASE_ROWS
    assert observer.progress_count == MAX_GENERATION_PROGRESS_LINES
    assert len(output.getvalue().encode("utf-8")) <= MAX_GENERATION_DIAGNOSTIC_BYTES
    assert "Generation diagnostics: outcome=success" in output.getvalue()
    assert observer.omitted > 0
    assert observer.omitted_phases == 100 - MAX_GENERATION_PHASE_ROWS
    omitted = observer.component_count - observer.component_rows_shown
    assert f"omitted_component_rows={omitted}" in output.getvalue()


def test_component_labels_do_not_inject_terminal_or_workflow_commands():
    output = io.StringIO()
    with redirect_stderr(output):
        observer = GenerationDiagnostics(progress=True, timing=True)
        with observer.component("::error::injected\n\x1b[31m\u202e"):
            pass
        observer.finish("success")
    value = output.getvalue()
    assert "\x1b" not in value and "\u202e" not in value
    assert not any(line.startswith("::") for line in value.splitlines())
    assert "\\n" in value and "\\x1b" in value


def test_closed_diagnostic_stream_cannot_fail_successful_work():
    class ClosedStream:
        def write(self, value):
            raise BrokenPipeError()
    with redirect_stderr(ClosedStream()):
        observer = GenerationDiagnostics(progress=True, timing=True)
        with observer.phase("work"):
            pass
        observer.finish("success")
    assert observer.stream_failed is True


def test_disabled_observer_is_silent_and_retains_no_rows(monkeypatch):
    import boundver._generation_diagnostics as diagnostics
    def forbidden():
        raise AssertionError("disabled diagnostics should not read the clock")
    monkeypatch.setattr(diagnostics.time, "perf_counter", forbidden)
    observer = GenerationDiagnostics()
    with observer.phase("work"), observer.component("component"):
        pass
    observer.finish("success")
    assert observer.phases == {} and observer.components == []
