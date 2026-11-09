"""Stalled diagnostic consumers cannot own the controller's execution thread."""

import io
import os
import threading
import json
import subprocess
import sys

import pytest

from boundver._bounded_output import BoundedDiagnosticOutput, MAX_DIAGNOSTIC_OUTPUT_BYTES


def test_output_drains_without_changing_text_or_closing_original():
    stream = io.StringIO()
    with BoundedDiagnosticOutput(stream) as output:
        assert output.write("first\n") == 6
        output.flush()
        output.write("second\n")
    assert stream.getvalue() == "first\nsecond\n"
    assert not stream.closed


def test_retained_output_has_independent_byte_and_record_budgets():
    stream = io.StringIO()
    with BoundedDiagnosticOutput(stream) as output:
        for _ in range(1000):
            output.write("x" * 1000)
        assert output.accepted_bytes <= MAX_DIAGNOSTIC_OUTPUT_BYTES - 256
        assert output.records.qsize() <= 128
        assert output.omitted_bytes > 0
    assert len(stream.getvalue().encode()) <= MAX_DIAGNOSTIC_OUTPUT_BYTES
    assert "Diagnostic output truncated" in stream.getvalue()


def test_blocked_stream_does_not_block_writes_or_controller_close():
    entered, release = threading.Event(), threading.Event()

    class Blocked:
        def write(self, text):
            entered.set()
            release.wait()

        def flush(self):
            pass

    output = BoundedDiagnosticOutput(Blocked())
    try:
        output.write("blocking record\n")
        assert entered.wait(5)
        output.write("controller continues\n")
        assert output.close() is False
        assert output.abandon.is_set()
    finally:
        release.set()
        output.thread.join(5)
    assert not output.thread.is_alive()


def test_closed_consumer_marks_failure_without_raising_in_controller():
    stream = io.StringIO()
    stream.close()
    output = BoundedDiagnosticOutput(stream)
    output.write("message")
    assert output.close()
    assert output.failed


def test_single_oversized_record_is_omitted_without_partial_text():
    stream = io.StringIO()
    with BoundedDiagnosticOutput(stream) as output:
        output.write("secret-placeholder" * MAX_DIAGNOSTIC_OUTPUT_BYTES)
    assert "secret-placeholder" not in stream.getvalue()
    assert "truncated" in stream.getvalue()


def test_record_budget_applies_even_to_tiny_writes():
    entered, release = threading.Event(), threading.Event()

    class Blocked:
        def write(self, text):
            entered.set()
            release.wait()

        def flush(self):
            pass

    output = BoundedDiagnosticOutput(Blocked())
    try:
        output.write("first")
        assert entered.wait(5)
        for _ in range(1000):
            output.write("x")
        assert output.records.qsize() == 128
        assert output.accepted_bytes == 5 + 128
        assert output.omitted_bytes == 1000 - 128
    finally:
        release.set()
        output.close()
        output.thread.join(5)
    assert not output.thread.is_alive()


def test_unexpected_sink_exception_does_not_trigger_recursive_thread_logging():
    class Failed:
        def write(self, text):
            raise RuntimeError("injected sink failure")

    output = BoundedDiagnosticOutput(Failed())
    output.write("diagnostic")
    assert output.close()
    assert output.failed


def test_terminal_publication_outcome_has_reserved_space_when_progress_overflows():
    entered, release = threading.Event(), threading.Event()
    recorded = io.StringIO()

    class Slow:
        def write(self, text):
            entered.set()
            release.wait()
            recorded.write(text)

        def flush(self):
            pass

    output = BoundedDiagnosticOutput(Slow())
    try:
        output.write("first\n")
        assert entered.wait(5)
        for _ in range(1000):
            output.write("x" * 1024)
        output.write_final("Generation diagnostics: publication=attempted-outcome-unconfirmed\n")
        assert output.final_accepted and output.omitted_bytes > 0
    finally:
        release.set()
        output.close()
        output.thread.join(5)
    assert "publication=attempted-outcome-unconfirmed" in recorded.getvalue()
    assert len(recorded.getvalue().encode()) <= MAX_DIAGNOSTIC_OUTPUT_BYTES


@pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipe setup")
def test_real_full_pipe_does_not_hold_controller_or_python_stderr_lock(monkeypatch):
    import boundver._bounded_output as bounded
    reader, writer = os.pipe()
    os.set_blocking(writer, False)
    try:
        while True:
            os.write(writer, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(writer, True)
    stream = os.fdopen(writer, "w", encoding="utf-8")
    output = BoundedDiagnosticOutput(stream)
    entered = threading.Event()
    original_write = os.write

    def write(descriptor, data):
        if descriptor == output.descriptor:
            entered.set()
        return original_write(descriptor, data)

    monkeypatch.setattr(bounded.os, "write", write)
    try:
        output.write("blocked OS write\n")
        assert entered.wait(5)
        assert output.close() is False
        assert not stream.closed
    finally:
        output.abandon.set()
        os.read(reader, 65536)  # Release only this test's known-full pipe.
        output.thread.join(5)
        stream.close()
        os.close(reader)
    assert not output.thread.is_alive()


def test_cli_process_can_exit_while_real_stderr_pipe_is_not_consumed():
    code = (
        "import json,os,sys\n"
        "from boundver._bounded_output import BoundedDiagnosticOutput\n"
        # POSIX pipe capacity varies, and can exceed the entire bounded output
        # allowance on macOS. Establish a full pipe rather than assuming that
        # repeatedly enqueueing diagnostics will necessarily block the writer.
        "prefilled=False\n"
        "if os.name == 'posix':\n"
        "    descriptor=sys.stderr.fileno()\n"
        "    os.set_blocking(descriptor,False)\n"
        "    try:\n"
        "        for _ in range(2048): os.write(descriptor,b'x'*4096)\n"
        "        raise RuntimeError('stderr pipe exceeds the test fill budget')\n"
        "    except BlockingIOError:\n"
        "        prefilled=True\n"
        "    finally:\n"
        "        os.set_blocking(descriptor,True)\n"
        "output=BoundedDiagnosticOutput(sys.stderr)\n"
        "for _ in range(1000): output.write('x'*1024)\n"
        "drained=output.close()\n"
        "sys.stdout.write(json.dumps({'drained':drained,'failed':output.failed,'prefilled':prefilled}))\n"
    )
    options = {"pipesize": 4096} if sys.platform == "linux" else {}
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", code],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options,
    )
    try:
        # Do not drain stderr until after exit: communicate() during the wait
        # would hide the exact consumer-stall case this test is proving.
        assert process.wait(timeout=10) == 0
        stdout, _ = process.communicate(timeout=5)
        assert json.loads(stdout) == {
            "drained": False, "failed": False, "prefilled": os.name == "posix",
        }
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
