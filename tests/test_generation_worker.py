"""Computation equivalence and no-publication worker contract."""

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import zipfile

import pytest

from boundver._generation_protocol import decode_result
from boundver._generation_snapshot import snapshot_to_value
from boundver._generation_worker import REQUEST_SCHEMA, compute_request, encode_request, run_transfer, write_transfer
from boundver._git import _capture_git_source_snapshot
from boundver._lockfile import generate_lockfile
from boundver.core import _capture_operation_snapshot, _get_version
from tests._repo_fixtures import commit_all, init_git_repo


@pytest.fixture
def invocation(tmp_path):
    init_git_repo(tmp_path)
    config = {"project": "worker", "components": {
        name: {"path": name, "boundary": {"provider": "path-hash", "paths": ["schema.json"]}}
        for name in ("api", "client")
    }}
    for name in config["components"]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "schema.json").write_text("{}", encoding="utf-8")
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    return {
        "schema": REQUEST_SCHEMA, "operation_id": "a" * 64,
        "repo_root": str(tmp_path), "config": config, "source": "head",
        "snapshot": snapshot_to_value(_capture_git_source_snapshot(tmp_path, "head")),
        "strict": True, "allow_custom_providers": False, "components": None,
        "existing_lock": None, "running_version": _get_version(),
        "progress": False, "timing": False,
    }


@pytest.mark.parametrize("source", ["head", "index", "working-tree"])
def test_worker_matches_direct_generation_and_does_not_publish(invocation, tmp_path, source):
    snapshot = _capture_operation_snapshot(tmp_path, source)
    invocation["source"] = source
    invocation["snapshot"] = snapshot_to_value(snapshot) if snapshot is not None else None
    direct = generate_lockfile(invocation["config"], tmp_path, source=source, snapshot=snapshot)
    sentinel = tmp_path / "boundary.lock.json"
    sentinel.write_bytes(b"prior lock must not be touched")
    data = encode_request(invocation)
    result = compute_request(data, running_version=_get_version())
    assert decode_result(result, request_digest=hashlib.sha256(data).hexdigest()) == direct
    assert sentinel.read_bytes() == b"prior lock must not be touched"


def test_worker_does_not_recapture_moving_head(invocation, tmp_path):
    data = encode_request(invocation)
    old = compute_request(data, running_version=_get_version())
    (tmp_path / "api" / "schema.json").write_text('{"new":true}', encoding="utf-8")
    commit_all(tmp_path)
    assert compute_request(data, running_version=_get_version()) == old


def test_scoped_worker_requires_and_uses_explicit_base_lock(invocation, tmp_path):
    invocation["existing_lock"] = generate_lockfile(invocation["config"], tmp_path)
    invocation["components"] = ["api"]
    data = encode_request(invocation)
    result = decode_result(compute_request(data, running_version=_get_version()), request_digest=hashlib.sha256(data).hexdigest())
    assert result == invocation["existing_lock"]
    assert not (tmp_path / "boundary.lock.json").exists()


@pytest.mark.parametrize("field,value", [
    ("strict", 1), ("allow_custom_providers", "true"), ("repo_root", "relative"),
    ("components", ["missing"]), ("components", ["api"]),
    ("operation_id", "invalid"), ("destination", "boundary.lock.json"),
    ("source", "index"),
])
def test_invalid_invocation_rejected_before_computation(invocation, field, value):
    changed = copy.deepcopy(invocation)
    changed[field] = value
    with pytest.raises(ValueError):
        encode_request(changed)


def test_runtime_mismatch_fails_before_computation(invocation):
    with pytest.raises(ValueError, match="runtime"):
        compute_request(encode_request(invocation), running_version="different")


def test_operation_identity_changes_result_binding(invocation):
    first = encode_request(invocation)
    invocation["operation_id"] = "b" * 64
    second = encode_request(invocation)
    assert first != second
    with pytest.raises(ValueError, match="request"):
        decode_result(compute_request(second, running_version=_get_version()), request_digest=hashlib.sha256(first).hexdigest())


def test_scoped_worker_still_rejects_drift_in_unselected_components(invocation, tmp_path):
    invocation["existing_lock"] = generate_lockfile(invocation["config"], tmp_path)
    invocation["components"] = ["api"]
    (tmp_path / "client" / "schema.json").write_text('{"changed":true}', encoding="utf-8")
    commit_all(tmp_path)
    invocation["snapshot"] = snapshot_to_value(_capture_git_source_snapshot(tmp_path, "head"))
    with pytest.raises(ValueError):
        compute_request(encode_request(invocation), running_version=_get_version())
    assert not (tmp_path / "boundary.lock.json").exists()


def test_private_transfer_computes_without_touching_repository_output(invocation, tmp_path):
    data = encode_request(invocation)
    with tempfile.TemporaryDirectory(prefix="bv-worker-test-") as name:
        directory = Path(name)
        write_transfer(directory, "request.json", data)
        assert run_transfer(directory, running_version=_get_version()) == 0
        result = (directory / "result.bin").read_bytes()
        assert result == compute_request(data, running_version=_get_version())
        assert not (directory / "error.json").exists()
        with pytest.raises(FileExistsError):
            write_transfer(directory, "result.bin", b"overwrite")
        assert (directory / "result.bin").read_bytes() == result
    assert not (tmp_path / "boundary.lock.json").exists()


@pytest.mark.parametrize("data", [b'{"incomplete":true}', b'{"schema":1,"schema":2}', b"\xff"])
def test_malformed_transfer_has_bounded_error_and_no_result(data):
    with tempfile.TemporaryDirectory(prefix="bv-worker-test-") as name:
        directory = Path(name)
        write_transfer(directory, "request.json", data)
        assert run_transfer(directory, running_version=_get_version()) == 2
        assert not (directory / "result.bin").exists()
        error = (directory / "error.json").read_bytes()
        assert len(error) <= 8192 and isinstance(json.loads(error)["error"], str)


def test_transfer_rejects_arbitrary_paths_and_does_not_overwrite_requests():
    with tempfile.TemporaryDirectory(prefix="bv-worker-test-") as name:
        directory = Path(name)
        with pytest.raises(ValueError):
            write_transfer(directory, "../outside", b"data")
        write_transfer(directory, "request.json", b"first")
        with pytest.raises(FileExistsError):
            write_transfer(directory, "request.json", b"second")
        assert (directory / "request.json").read_bytes() == b"first"


def test_private_transfer_does_not_request_durable_lock_publication(monkeypatch):
    import boundver._generation_worker as worker

    def unexpected_sync(*args):
        pytest.fail("private IPC must not force a durable disk sync")

    monkeypatch.setattr(worker.os, "fsync", unexpected_sync)
    with tempfile.TemporaryDirectory(prefix="bv-worker-test-") as name:
        directory = Path(name)
        write_transfer(directory, "request.json", b"complete transfer")
        assert (directory / "request.json").read_bytes() == b"complete transfer"


@pytest.mark.skipif(os.name != "nt", reason="Windows contained launcher")
def test_contained_interpreter_executes_real_worker_module(invocation, tmp_path):
    from boundver._windows_job import WindowsJob
    data = encode_request(invocation)
    with tempfile.TemporaryDirectory(prefix="bv-worker-test-") as name:
        directory = Path(name)
        write_transfer(directory, "request.json", data)
        with WindowsJob() as job:
            process = job.launch_python(
                ["-I", "-m", "boundver._generation_worker", str(directory)],
                cwd=str(tmp_path), env=dict(os.environ),
            )
            try:
                status = process.wait(20)
                detail = (directory / "error.json").read_text() if (directory / "error.json").exists() else "no worker error"
                assert status == 0, detail
                assert job.active_processes == 0
                result = (directory / "result.bin").read_bytes()
                assert result == compute_request(data, running_version=_get_version())
            finally:
                job.terminate()
                process.wait(5)
                process.close()
    assert not (tmp_path / "boundary.lock.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows contained controller")
def test_controller_validates_result_and_cleans_transfer_before_publication(invocation, tmp_path, monkeypatch):
    from boundver import _generation_runner as runner
    from boundver._generation_control import GenerationControl, GenerationState
    created = []
    original = runner.tempfile.mkdtemp

    def capture_directory(*args, **kwargs):
        name = original(*args, **kwargs)
        created.append(Path(name))
        return name

    monkeypatch.setattr(runner.tempfile, "mkdtemp", capture_directory)
    control = GenerationControl(20)
    result = runner.run_windows_generation(encode_request(invocation), control=control, running_version=_get_version())
    assert result == generate_lockfile(invocation["config"], tmp_path)
    assert control.state is GenerationState.READY
    assert created and not any(path.exists() for path in created)
    assert not (tmp_path / "boundary.lock.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows contained controller")
@pytest.mark.parametrize("stop", ["expire", "cancel"])
def test_controller_reaps_real_worker_when_stopped_immediately_after_launch(invocation, monkeypatch, stop):
    from boundver import _generation_runner as runner
    from boundver._generation_control import GenerationControl, GenerationState
    from boundver._windows_job import WindowsJob, WindowsJobProcess
    from boundver._utils import ConfigError
    now = [0.0]
    control = GenerationControl(1, clock=lambda: now[0])
    launch = WindowsJob.launch_python
    close = WindowsJobProcess.close
    reaped = []

    def stop_after_launch(job, *args, **kwargs):
        process = launch(job, *args, **kwargs)
        if stop == "expire":
            now[0] = 2.0
        else:
            control.cancel()
        return process

    def record_reap(process):
        reaped.append(process.poll() is not None)
        close(process)

    monkeypatch.setattr(WindowsJob, "launch_python", stop_after_launch)
    monkeypatch.setattr(WindowsJobProcess, "close", record_reap)
    with pytest.raises(ConfigError, match="did not publish"):
        runner.run_windows_generation(encode_request(invocation), control=control, running_version=_get_version())
    assert reaped == [True]
    assert control.state is (GenerationState.EXPIRED if stop == "expire" else GenerationState.CANCELLED)


@pytest.mark.skipif(os.name != "nt", reason="Windows contained controller")
def test_expired_controller_does_not_launch(invocation, monkeypatch):
    from boundver import _generation_runner as runner
    from boundver._generation_control import GenerationControl
    from boundver._windows_job import WindowsJob
    from boundver._utils import ConfigError
    now = [0.0]
    control = GenerationControl(1, clock=lambda: now[0])
    now[0] = 1.0
    monkeypatch.setattr(WindowsJob, "launch_python", lambda *a, **k: pytest.fail("expired launch"))
    with pytest.raises(ConfigError, match="expired"):
        runner.run_windows_generation(encode_request(invocation), control=control, running_version=_get_version())


@pytest.mark.skipif(os.name != "posix", reason="POSIX session launcher")
def test_posix_session_runs_real_worker_and_reaps_it(invocation, tmp_path):
    from boundver._posix_worker import PosixWorker
    data = encode_request(invocation)
    with tempfile.TemporaryDirectory(prefix="bv-worker-test-") as name:
        directory = Path(name)
        write_transfer(directory, "request.json", data)
        process = PosixWorker(directory, cwd=tmp_path, env=dict(os.environ))
        try:
            assert process.wait(20) == 0
            assert (directory / "result.bin").read_bytes() == compute_request(data, running_version=_get_version())
        finally:
            process.terminate_group()
            process.wait(5)
        assert not process.group_exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX session launcher")
def test_posix_session_does_not_inherit_controller_session(invocation, tmp_path):
    from boundver._posix_worker import PosixWorker
    # A missing request fails safely; session creation must precede user code.
    with tempfile.TemporaryDirectory(prefix="bv-worker-test-") as name:
        process = PosixWorker(Path(name), cwd=tmp_path, env=dict(os.environ))
        try:
            assert os.getsid(process.pid) == process.pid
            assert os.getpgid(process.pid) == process.pid
        finally:
            process.terminate_group()
            process.wait(5)
        assert not process.group_exists()


def test_platform_controller_matches_direct_generation(invocation, tmp_path):
    from boundver._generation_control import GenerationControl, GenerationState
    from boundver._generation_runner import run_generation
    control = GenerationControl(20)
    result = run_generation(encode_request(invocation), control=control, running_version=_get_version())
    assert result == generate_lockfile(invocation["config"], tmp_path)
    assert control.state is GenerationState.READY
    assert not (tmp_path / "boundary.lock.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX controller")
@pytest.mark.parametrize("stop", ["expire", "cancel"])
def test_posix_controller_reaps_real_worker_after_stop(invocation, monkeypatch, stop):
    from boundver._generation_control import GenerationControl, GenerationState
    from boundver._generation_runner import run_generation
    from boundver._posix_worker import PosixJob, PosixWorker
    from boundver._utils import ConfigError
    now = [0.0]
    control = GenerationControl(1, clock=lambda: now[0])
    launch = PosixJob.launch_python
    close = PosixWorker.close
    reaped = []

    def stop_after_launch(job, *args, **kwargs):
        process = launch(job, *args, **kwargs)
        if stop == "expire":
            now[0] = 2.0
        else:
            control.cancel()
        return process

    def record_reap(process):
        reaped.append(process.poll() is not None)
        close(process)

    monkeypatch.setattr(PosixJob, "launch_python", stop_after_launch)
    monkeypatch.setattr(PosixWorker, "close", record_reap)
    with pytest.raises(ConfigError, match="did not publish"):
        run_generation(encode_request(invocation), control=control, running_version=_get_version())
    assert reaped == [True]
    assert control.state is (GenerationState.EXPIRED if stop == "expire" else GenerationState.CANCELLED)


@pytest.mark.parametrize("source", ["head", "index", "working-tree"])
def test_internal_timed_dispatch_preserves_json_and_dry_run(invocation, tmp_path, capsys, source):
    from boundver import core
    from boundver._cli_parser import build_parser
    args = build_parser(version="test", epilog="").parse_args([
        "--quiet", "generate", "--source", source, "--dry-run", "--format", "json",
    ])
    # Deliberately internal until provider/diagnostic/platform gates are met.
    args.timeout = 20
    sentinel = tmp_path / "boundary.lock.json"
    sentinel.write_bytes(b"prior lock")
    core._cmd_generate(args, tmp_path)
    output = capsys.readouterr()
    expected = generate_lockfile(invocation["config"], tmp_path, source=source)
    assert json.loads(output.out) == expected
    assert sentinel.read_bytes() == b"prior lock"
    assert not output.err


def test_internal_expired_dispatch_preserves_destination(invocation, tmp_path, capsys):
    from boundver import core
    from boundver._cli_parser import build_parser
    args = build_parser(version="test", epilog="").parse_args([
        "--quiet", "generate", "--source", "head", "--timing",
    ])
    args.timeout = 0.000000001
    sentinel = tmp_path / "boundary.lock.json"
    sentinel.write_bytes(b"prior lock")
    with pytest.raises(SystemExit) as error:
        core._cmd_generate(args, tmp_path)
    assert error.value.code == 2
    assert sentinel.read_bytes() == b"prior lock"
    assert "outcome=expired" in capsys.readouterr().err


def test_internal_timed_dispatch_publishes_only_validated_result(invocation, tmp_path, capsys):
    from boundver import core
    from boundver._cli_parser import build_parser
    args = build_parser(version="test", epilog="").parse_args(["--quiet", "generate", "--timing"])
    args.timeout = 20
    expected = generate_lockfile(invocation["config"], tmp_path)
    core._cmd_generate(args, tmp_path)
    assert json.loads((tmp_path / "boundary.lock.json").read_text()) == expected
    assert "publication=written" in capsys.readouterr().err


def test_internal_cancel_after_result_validation_cannot_enter_publication(invocation, tmp_path, monkeypatch, capsys):
    from boundver import core
    from boundver._cli_parser import build_parser
    from boundver._utils import ConfigError
    original = core.run_generation

    def cancel_after_result(*args, **kwargs):
        lock = original(*args, **kwargs)
        assert kwargs["control"].cancel()
        return lock

    monkeypatch.setattr(core, "run_generation", cancel_after_result)
    args = build_parser(version="test", epilog="").parse_args(["--quiet", "generate", "--timing"])
    args.timeout = 20
    sentinel = tmp_path / "boundary.lock.json"
    sentinel.write_bytes(b"prior lock")
    with pytest.raises(ConfigError, match="did not publish: cancelled"):
        core._cmd_generate(args, tmp_path)
    assert sentinel.read_bytes() == b"prior lock"
    stderr = capsys.readouterr().err
    assert "publication=not-attempted" in stderr and "outcome=cancelled" in stderr


def test_internal_timed_dispatch_defers_custom_imports_to_worker(invocation, tmp_path, monkeypatch, capsys):
    from boundver import core, _config
    from boundver._cli_parser import build_parser
    invocation["config"]["providers"] = [{
        "module": "boundver_missing_custom_provider_fixture", "class": "Provider",
        "name": "custom.test",
    }]
    (tmp_path / "boundary.config.json").write_text(json.dumps(invocation["config"]), encoding="utf-8")
    commit_all(tmp_path)

    def parent_import(*args, **kwargs):
        pytest.fail("custom provider loading escaped into the controller")

    monkeypatch.setattr(_config, "load_custom_providers", parent_import)
    args = build_parser(version="test", epilog="").parse_args([
        "--quiet", "generate", "--allow-custom-providers",
    ])
    args.timeout = 20
    with pytest.raises(SystemExit) as error:
        core._cmd_generate(args, tmp_path)
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "Generation worker failed" in stderr
    assert "boundver_missing_custom_provider_fixture" in stderr
    assert not (tmp_path / "boundary.lock.json").exists()


def test_internal_timed_dispatch_forwards_worker_progress_without_corrupting_json(invocation, tmp_path, capsys):
    from boundver import core
    from boundver._cli_parser import build_parser
    args = build_parser(version="test", epilog="").parse_args([
        "--quiet", "generate", "--progress", "--timing", "--dry-run", "--format", "json",
    ])
    args.timeout = 20
    core._cmd_generate(args, tmp_path)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == generate_lockfile(invocation["config"], tmp_path)
    assert "Worker: Generation: component api started" in captured.err
    assert "Worker: Timing component api:" in captured.err
    assert "Worker: Generation diagnostics: outcome=computed" in captured.err
    assert "publication=dry-run" in captured.err
    assert len(captured.err.encode("utf-8")) <= 64 * 1024
    assert not (tmp_path / "boundary.lock.json").exists()


def test_archive_controller_launches_worker_from_same_archive(invocation, tmp_path):
    # This is a real package-in-zip handoff test, not the full reproducible
    # release archive gate (which also checks vendored format dependencies).
    import boundver
    package = Path(boundver.__file__).parent
    archive_path = tmp_path / "runtime boundver.pyz"
    main = (
        "import sys\nfrom pathlib import Path\nfrom boundver import __version__\n"
        "from boundver._generation_control import GenerationControl\n"
        "from boundver._generation_runner import run_generation\n"
        "from boundver._lockfile import dump_lockfile\n"
        "lock=run_generation(Path(sys.argv[1]).read_bytes(),control=GenerationControl(20),running_version=__version__)\n"
        "sys.stdout.write(dump_lockfile(lock))\n"
    )
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in package.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts:
                archive.write(path, "boundver/" + path.relative_to(package).as_posix())
        archive.writestr("__main__.py", main)
        archive.writestr(f"boundver-{_get_version()}.dist-info/METADATA", f"Metadata-Version: 2.4\nName: boundver\nVersion: {_get_version()}\n")
    request_path = tmp_path / "request.json"
    request_path.write_bytes(encode_request(invocation))
    completed = subprocess.run(
        [sys.executable, "-I", str(archive_path), str(request_path)],
        cwd=tmp_path, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert json.loads(completed.stdout) == generate_lockfile(invocation["config"], tmp_path)
    assert not completed.stderr
    assert not (tmp_path / "boundary.lock.json").exists()
