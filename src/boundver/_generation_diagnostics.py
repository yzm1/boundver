"""Opt-in local generation diagnostics; never part of persisted identities."""

from contextlib import contextmanager
import sys
import time
from typing import Iterator

from ._utils import _safe_display_text

MAX_GENERATION_COMPONENT_ROWS = 128
MAX_GENERATION_PHASE_ROWS = 32
MAX_GENERATION_PROGRESS_LINES = 256
MAX_GENERATION_LABEL_CHARS = 256
MAX_GENERATION_DIAGNOSTIC_BYTES = 64 * 1024


class GenerationDiagnostics:
    """Bound both streaming progress and retained timing rows independently."""

    def __init__(self, *, progress: bool = False, timing: bool = False):
        self.progress = progress
        self.timing = timing
        self.enabled = progress or timing
        self.started = time.perf_counter() if self.enabled else 0.0
        self.phases: dict[str, float] = {}
        self.components: list[tuple[str, float]] = []
        self.component_count = 0
        self.component_rows_shown = 0
        self.omitted_phases = 0
        self.progress_count = 0
        self.omitted = 0
        self.bytes = 0
        self.publication = "not-attempted"
        self.stream_failed = False

    @staticmethod
    def label(value: str) -> str:
        text = value[:MAX_GENERATION_LABEL_CHARS]
        if len(value) > MAX_GENERATION_LABEL_CHARS:
            text += "..."
        return _safe_display_text(text)

    def emit(self, text: str, *, final: bool = False) -> bool:
        if self.stream_failed:
            return False
        body = text + "\n"
        size = len(body.encode("utf-8"))
        ceiling = MAX_GENERATION_DIAGNOSTIC_BYTES - (0 if final else 1024)
        if self.bytes + size > ceiling:
            self.omitted += 1
            return False
        try:
            sys.stderr.write(body)
            sys.stderr.flush()
        except (OSError, ValueError, TypeError):
            # Optional reporting must not turn an already-published success
            # into a failure simply because a diagnostic pipe closed.
            self.stream_failed = True
            return False
        else:
            self.bytes += size
            return True

    def progress_line(self, text: str) -> None:
        if not self.progress:
            return
        if self.progress_count >= MAX_GENERATION_PROGRESS_LINES:
            self.omitted += 1
            return
        self.progress_count += 1
        self.emit("Generation: " + text)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        self.progress_line(self.label(name) + " started")
        started = time.perf_counter()
        try:
            yield
        finally:
            if name in self.phases or len(self.phases) < MAX_GENERATION_PHASE_ROWS:
                self.phases[name] = self.phases.get(name, 0.0) + max(0.0, time.perf_counter() - started)
            else:
                self.omitted_phases += 1

    @contextmanager
    def component(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        label = self.label(name)
        self.progress_line("component " + label + " started")
        started = time.perf_counter()
        try:
            yield
        finally:
            self.component_count += 1
            if self.timing and len(self.components) < MAX_GENERATION_COMPONENT_ROWS:
                self.components.append((label, max(0.0, time.perf_counter() - started)))

    def finish(self, outcome: str) -> None:
        if not self.enabled:
            return
        if self.timing:
            for name, elapsed in self.phases.items():
                self.emit(f"Timing phase {self.label(name)}: {elapsed:.6f}s")
            for name, elapsed in self.components:
                if self.emit(f"Timing component {name}: {elapsed:.6f}s"):
                    self.component_rows_shown += 1
        elapsed = max(0.0, time.perf_counter() - self.started)
        omitted_components = self.component_count - self.component_rows_shown if self.timing else 0
        self.emit(
            f"Generation diagnostics: outcome={outcome}; elapsed={elapsed:.6f}s; "
            f"publication={self.publication}; components_attempted={self.component_count}; "
            f"omitted_component_rows={omitted_components}; "
            f"omitted_phase_timings={self.omitted_phases}; omitted_lines={self.omitted}",
            final=True,
        )
