"""Publication race contract; process containment is tested separately."""

import threading

import pytest

from boundver._generation_control import GenerationControl, GenerationState


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), True, "1", None])
def test_invalid_deadlines_are_rejected(seconds):
    with pytest.raises(ValueError):
        GenerationControl(seconds)


@pytest.mark.parametrize("ready", [False, True])
def test_expiry_at_boundary_prevents_late_publication(ready):
    now = [10.0]
    control = GenerationControl(5, clock=lambda: now[0])
    if ready:
        assert control.result_ready()
    now[0] = 15.0
    writes = []
    assert not control.result_ready()
    with pytest.raises(RuntimeError, match="expired"):
        control.publish(lambda: writes.append(True))
    assert not writes
    assert control.state is GenerationState.EXPIRED


@pytest.mark.parametrize("ready", [False, True])
def test_cancellation_cannot_be_reopened_by_late_success(ready):
    control = GenerationControl(60)
    if ready:
        assert control.result_ready()
    assert control.cancel()
    assert control.cancel()
    assert not control.result_ready()
    control.fail()
    with pytest.raises(RuntimeError, match="cancelled"):
        control.publish(lambda: pytest.fail("late publication"))
    assert control.state is GenerationState.CANCELLED


def test_incomplete_and_failed_results_cannot_publish():
    control = GenerationControl(60)
    with pytest.raises(RuntimeError, match="computing"):
        control.publish(lambda: pytest.fail("incomplete result"))
    control.fail()
    assert not control.result_ready()
    assert not control.cancel()
    with pytest.raises(RuntimeError, match="failed"):
        control.publish(lambda: pytest.fail("failed result"))


@pytest.mark.parametrize("ready", [False, True])
def test_explicit_expiry_prevents_publication_before_clock_advances(ready):
    control = GenerationControl(60, clock=lambda: 1000.0)
    signal = threading.Event()
    control.bind_operation_expiry(signal)
    if ready:
        assert control.result_ready()
    signal.set()
    assert control.state is GenerationState.EXPIRED
    assert not control.result_ready()
    with pytest.raises(RuntimeError, match="expired"):
        control.publish(lambda: pytest.fail("expired operation published"))


@pytest.mark.parametrize("terminal", ["cancelled", "failed", "published", "publication-unconfirmed"])
def test_late_watchdog_signal_cannot_reopen_terminal_state(terminal):
    control = GenerationControl(60, clock=lambda: 1000.0)
    signal = threading.Event()
    control.bind_operation_expiry(signal)
    if terminal == "cancelled":
        assert control.cancel()
    elif terminal == "failed":
        control.fail()
    else:
        assert control.result_ready()
        if terminal == "published":
            control.publish(lambda: None)
        else:
            def fail():
                raise OSError("uncertain publication")
            with pytest.raises(OSError):
                control.publish(fail)
    signal.set()
    assert control.state.value == terminal
    assert not control.result_ready()
    with pytest.raises(RuntimeError, match=terminal):
        control.publish(lambda: pytest.fail("terminal operation published again"))


def test_bound_signal_cannot_be_replaced_to_clear_expiry():
    control = GenerationControl(60)
    signal = threading.Event()
    control.bind_operation_expiry(signal)
    signal.set()
    with pytest.raises(RuntimeError, match="only be bound once"):
        control.bind_operation_expiry(threading.Event())
    assert control.state is GenerationState.EXPIRED


def test_commit_admission_wins_cancellation_without_holding_writer_lock():
    now = [0.0]
    control = GenerationControl(5, clock=lambda: now[0])
    signal = threading.Event()
    control.bind_operation_expiry(signal)
    assert control.result_ready()
    admitted = threading.Event()
    finish = threading.Event()
    failures = []

    def writer():
        admitted.set()
        assert finish.wait(5), "test did not release writer"

    def run():
        try:
            control.publish(writer)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert admitted.wait(5)
        now[0] = 10.0
        assert not control.cancel()
        control.fail()
        assert control.state is GenerationState.COMMITTING
        signal.set()
        assert control.state is GenerationState.COMMITTING
        with pytest.raises(RuntimeError, match="committing"):
            control.publish(lambda: pytest.fail("duplicate publication"))
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not failures
    assert control.state is GenerationState.PUBLISHED


@pytest.mark.parametrize("exception", [OSError("fsync"), KeyboardInterrupt()])
def test_writer_exception_keeps_publication_uncertainty(exception):
    control = GenerationControl(60)
    assert control.result_ready()
    writes = []

    def writer():
        writes.append("replacement may have happened")
        raise exception

    with pytest.raises(type(exception)):
        control.publish(writer)
    assert writes
    assert control.state is GenerationState.UNCONFIRMED
    assert not control.cancel()
    assert not control.result_ready()
    with pytest.raises(RuntimeError, match="publication-unconfirmed"):
        control.publish(lambda: pytest.fail("unsafe retry"))


def test_success_cannot_publish_twice():
    control = GenerationControl(60)
    assert control.result_ready()
    assert not control.result_ready()
    writes = []
    control.publish(lambda: writes.append(True))
    with pytest.raises(RuntimeError, match="published"):
        control.publish(lambda: writes.append(True))
    assert writes == [True]
