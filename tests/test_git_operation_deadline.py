"""Operation deadlines also cover Git used before worker launch."""

import threading

import pytest

from boundver import _git
from boundver._utils import GuardrailError


class FakeProcess:
    def __init__(self):
        self.killed = threading.Event()

    def poll(self):
        return None

    def kill(self):
        self.killed.set()


def test_scoped_budget_shortens_and_never_extends_command_timer(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(_git.time, "monotonic", lambda: now)
    with _git.git_operation_deadline(now + 1):
        timer = _git._GitProcessDeadline(FakeProcess(), seconds=300)
        assert 0 <= timer.seconds <= 1
        with _git.git_operation_deadline(now + 100):
            assert _git._GitProcessDeadline(FakeProcess()).seconds <= 1
        with _git.git_operation_deadline(now + 0.25):
            assert _git._GitProcessDeadline(FakeProcess()).seconds == 0.25
        assert _git._GitProcessDeadline(FakeProcess()).seconds == 1
    assert _git._GitProcessDeadline(FakeProcess(), seconds=300).seconds == 300


def test_context_is_restored_after_exception():
    with pytest.raises(RuntimeError):
        with _git.git_operation_deadline(0):
            raise RuntimeError("injected")
    assert _git._OPERATION_GIT_DEADLINE.get() is None


def test_expired_budget_refuses_git_argv_before_any_process(tmp_path):
    with _git.git_operation_deadline(0):
        with pytest.raises(GuardrailError, match="before command launch"):
            _git._offline_git_command(tmp_path, ["status"])


def test_expired_timer_still_kills_process_created_at_deadline_boundary():
    process = FakeProcess()
    with _git.git_operation_deadline(0):
        timer = _git._GitProcessDeadline(process)
        timer.start()
        try:
            assert process.killed.wait(2)
            with pytest.raises(GuardrailError):
                timer.raise_if_expired()
        finally:
            timer.cancel()


def test_deadline_does_not_leak_into_another_thread():
    observed = []
    with _git.git_operation_deadline(0):
        thread = threading.Thread(target=lambda: observed.append(_git._OPERATION_GIT_DEADLINE.get()))
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
    assert observed == [None]


@pytest.mark.parametrize("deadline", [True, "1", None, float("nan"), float("inf")])
def test_invalid_deadlines_are_rejected(deadline):
    with pytest.raises(ValueError):
        with _git.git_operation_deadline(deadline):
            pytest.fail("invalid deadline accepted")
