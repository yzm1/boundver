"""Best-effort bounded stderr delivery without blocking operation supervision.

Only diagnostics use this queue. Machine-readable stdout must never be dropped
or redirected through it. A stuck consumer can retain one daemon writer and
its descriptor until CLI process exit; it cannot block the controller thread.
"""

import os
import codecs
import queue
import threading

from ._generation_diagnostics import MAX_GENERATION_DIAGNOSTIC_BYTES


MAX_DIAGNOSTIC_OUTPUT_BYTES = MAX_GENERATION_DIAGNOSTIC_BYTES
MAX_DIAGNOSTIC_OUTPUT_RECORDS = 128
MAX_DIAGNOSTIC_DRAIN_SECONDS = 1
MAX_DIAGNOSTIC_NOTICE_BYTES = 512
MAX_DIAGNOSTIC_FINAL_BYTES = 2048


class BoundedDiagnosticOutput:
    def __init__(self, stream):
        self.stream = stream
        self.encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            codecs.lookup(self.encoding)
        except (LookupError, TypeError):
            self.encoding = "utf-8"
        self.records = queue.Queue(maxsize=MAX_DIAGNOSTIC_OUTPUT_RECORDS)
        self.lock = threading.Lock()
        self.closed_event = threading.Event()
        self.abandon = threading.Event()
        self.accepted_bytes = 0
        self.normal_bytes = 0
        self.final_record = None
        self.final_accepted = False
        self.omitted_bytes = 0
        self.failed = False
        self.descriptor = None
        try:
            # A dedicated raw descriptor avoids holding Python's stderr buffer
            # lock in a daemon thread if an OS pipe write blocks at shutdown.
            self.descriptor = os.dup(stream.fileno())
        except (AttributeError, OSError, ValueError):
            pass  # In-memory captures and embedded custom streams have no fd.
        self.thread = threading.Thread(target=self._drain, daemon=True, name="boundver-diagnostics")
        try:
            self.thread.start()
        except BaseException:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            raise

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("Diagnostic output requires text")
        # Bound encoding allocation as well as retained data. Diagnostic text
        # beyond this count is already too large even in single-byte encodings.
        if len(text) > MAX_DIAGNOSTIC_OUTPUT_BYTES:
            with self.lock:
                self.omitted_bytes += len(text)
            return len(text)
        body = text.encode(self.encoding, errors="backslashreplace")
        with self.lock:
            if self.closed_event.is_set() or self.failed or self.normal_bytes + len(body) > MAX_DIAGNOSTIC_OUTPUT_BYTES - MAX_DIAGNOSTIC_NOTICE_BYTES - MAX_DIAGNOSTIC_FINAL_BYTES:
                self.omitted_bytes += len(body)
                return len(text)
            try:
                self.records.put_nowait(body)
            except queue.Full:
                self.omitted_bytes += len(body)
            else:
                self.accepted_bytes += len(body)
                self.normal_bytes += len(body)
        return len(text)

    def write_final(self, text: str) -> int:
        """Reserve one terminal outcome independently of progress backlog."""
        if not isinstance(text, str):
            raise TypeError("Diagnostic output requires text")
        if len(text) > MAX_DIAGNOSTIC_FINAL_BYTES:
            with self.lock:
                self.omitted_bytes += len(text)
            return len(text)
        body = text.encode(self.encoding, errors="backslashreplace")
        with self.lock:
            if self.closed_event.is_set() or self.failed or self.final_accepted or len(body) > MAX_DIAGNOSTIC_FINAL_BYTES:
                self.omitted_bytes += len(body)
            else:
                self.final_record = body
                self.final_accepted = True
                self.accepted_bytes += len(body)
        return len(text)

    def flush(self):
        # Calling flush from a progress emitter must not wait on the consumer.
        return None

    def isatty(self):
        return False

    def _deliver(self, body: bytes):
        if self.descriptor is None:
            self.stream.write(body.decode(self.encoding))
            self.stream.flush()
        else:
            view = memoryview(body)
            while view and not self.abandon.is_set():
                written = os.write(self.descriptor, view)
                if written <= 0:
                    raise OSError("Diagnostic stream made no write progress")
                view = view[written:]

    def _drain(self):
        try:
            while not self.abandon.is_set():
                with self.lock:
                    terminal = self.final_record if self.closed_event.is_set() else None
                    if terminal is not None:
                        self.final_record = None
                if terminal is not None:
                    self._deliver(terminal)
                    continue
                try:
                    body = self.records.get(timeout=0.01)
                except queue.Empty:
                    with self.lock:
                        if not self.closed_event.is_set() or not self.records.empty() or self.final_record is not None:
                            continue
                        omitted = self.omitted_bytes
                    if omitted:
                        notice = f"Diagnostic output truncated; omitted at least {omitted} bytes.\n".encode(self.encoding)
                        if len(notice) <= MAX_DIAGNOSTIC_NOTICE_BYTES:
                            self._deliver(notice)
                    return
                self._deliver(body)
        except BaseException:
            # Do not let a failing diagnostic sink invoke threading's exception
            # printer recursively against the same stderr queue it just broke.
            with self.lock:
                self.failed = True
        finally:
            if self.descriptor is not None:
                try:
                    os.close(self.descriptor)
                except OSError:
                    self.failed = True
                self.descriptor = None

    def close(self) -> bool:
        with self.lock:
            if self.closed_event.is_set():
                return not self.thread.is_alive()
            self.closed_event.set()
        self.thread.join(MAX_DIAGNOSTIC_DRAIN_SECONDS)
        drained = not self.thread.is_alive()
        if not drained:
            self.abandon.set()
        return drained

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
