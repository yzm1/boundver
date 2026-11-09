"""Controller-side publication admission; not a worker timeout by itself."""

from enum import Enum
import math
import threading
import time
from typing import Callable, Optional


class GenerationState(Enum):
    COMPUTING = "computing"
    READY = "ready"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    COMMITTING = "committing"
    PUBLISHED = "published"
    UNCONFIRMED = "publication-unconfirmed"


class GenerationControl:
    """Serialize cancellation and admission to a non-interruptible commit.

    READY means the caller has validated the complete worker result and reaped
    its owned execution group. This class does not perform either task, kill
    processes, or asynchronously interrupt Python/filesystem operations.
    """

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("Generation deadline must be a finite positive number")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Generation deadline must be a finite positive number")
        self._clock = clock
        self._deadline = clock() + seconds
        if not math.isfinite(self._deadline):
            raise ValueError("Generation deadline is outside the clock range")
        self._lock = threading.Lock()
        self._state = GenerationState.COMPUTING
        self._operation_expiry: Optional[threading.Event] = None

    def _expire_locked(self) -> None:
        if self._state in (GenerationState.COMPUTING, GenerationState.READY):
            if (
                self._operation_expiry is not None and self._operation_expiry.is_set()
            ) or self._clock() >= self._deadline:
                self._state = GenerationState.EXPIRED

    def bind_operation_expiry(self, signal: threading.Event) -> None:
        """Observe the scoped Git watchdog before admitting computation."""
        with self._lock:
            if self._operation_expiry is not None or self._state is not GenerationState.COMPUTING:
                raise RuntimeError("Generation operation expiry can only be bound once before computation")
            self._operation_expiry = signal
            self._expire_locked()

    @property
    def monotonic_deadline(self) -> float:
        """Immutable deadline for local Git preparation using the same clock."""
        return self._deadline

    @property
    def state(self) -> GenerationState:
        with self._lock:
            self._expire_locked()
            return self._state

    def cancel(self) -> bool:
        """Return whether cancellation won before commit admission."""
        with self._lock:
            self._expire_locked()
            if self._state in (GenerationState.COMPUTING, GenerationState.READY):
                self._state = GenerationState.CANCELLED
                return True
            return self._state is GenerationState.CANCELLED

    def fail(self) -> None:
        with self._lock:
            self._expire_locked()
            if self._state in (GenerationState.COMPUTING, GenerationState.READY):
                self._state = GenerationState.FAILED

    def result_ready(self) -> bool:
        """Caller attests result validation AND owned-process cleanup completed."""
        with self._lock:
            self._expire_locked()
            if self._state is not GenerationState.COMPUTING:
                return False
            self._state = GenerationState.READY
            return True

    def publish(self, writer: Callable[[], None]) -> None:
        """Admit once, then finish the atomic writer without timeout injection."""
        with self._lock:
            self._expire_locked()
            if self._state is not GenerationState.READY:
                raise RuntimeError(f"Generation cannot publish: {self._state.value}")
            self._state = GenerationState.COMMITTING
        try:
            writer()
        except BaseException:
            # Replacement may precede an exception (including fsync failure).
            with self._lock:
                self._state = GenerationState.UNCONFIRMED
            raise
        else:
            with self._lock:
                self._state = GenerationState.PUBLISHED
