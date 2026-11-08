"""Owned Windows process job with creation-time interpreter assignment.

This is cancellation containment, not a sandbox. Use launch_python for workers;
the low-level assign helper requires suspension to avoid a spawn race.
"""

import ctypes
from ctypes import wintypes
import os
import math
import subprocess
import sys


class WindowsJob:
    """A non-inheritable, unnamed kill-on-close job with no breakaway flags."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows process jobs are unavailable on this platform")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel = kernel
        self._handle = None

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_longlong),
                ("JobTime", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperations", "WriteOperations", "OtherOperations",
                "ReadBytes", "WriteBytes", "OtherBytes",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("Basic", BasicLimits), ("Io", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t),
                ("PeakJobMemory", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("UserTime", ctypes.c_longlong),
                ("KernelTime", ctypes.c_longlong),
                ("PeriodUserTime", ctypes.c_longlong),
                ("PeriodKernelTime", ctypes.c_longlong),
                ("PageFaults", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TerminatedProcesses", wintypes.DWORD),
            ]

        self._accounting = Accounting
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL,
            ),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(kernel, name)
            function.argtypes = arguments
            function.restype = result
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise OSError("Windows process job is closed")
        return self._handle

    def assign(self, process_handle: int) -> None:
        if not self._kernel.AssignProcessToJobObject(self.handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if not self._kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def launch_python(self, arguments: list[str], *, cwd: str, env: dict[str, str]):
        """Create this interpreter in the job atomically, without a shell.

        The caller supplies trusted worker arguments. No standard handles are
        inherited; results must use a separately designed private transport.
        """
        if not os.path.isabs(sys.executable) or not os.path.isabs(cwd):
            raise ValueError("Interpreter and worker directory must be absolute")
        if any(not isinstance(arg, str) or "\0" in arg for arg in arguments):
            raise ValueError("Invalid worker argument")
        if "\0" in cwd:
            raise ValueError("Invalid worker directory")
        for key, value in env.items():
            if not key or "=" in key or "\0" in key or "\0" in value:
                raise ValueError("Invalid worker environment")

        class Startup(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                ("width", wintypes.DWORD), ("height", wintypes.DWORD),
                ("xChars", wintypes.DWORD), ("yChars", wintypes.DWORD),
                ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("show", wintypes.WORD), ("reservedSize", wintypes.WORD),
                ("reservedBytes", ctypes.c_void_p),
                ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE),
                ("stderr", wintypes.HANDLE),
            ]

        class ExtendedStartup(ctypes.Structure):
            _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [
                ("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD),
            ]

        kernel = self._kernel
        kernel.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
        ]
        kernel.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel.DeleteProcThreadAttributeList.restype = None
        kernel.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.c_void_p, ctypes.POINTER(ProcessInfo),
        ]
        kernel.CreateProcessW.restype = wintypes.BOOL
        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(size.value)
        if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            jobs = (wintypes.HANDLE * 1)(self.handle)
            if not kernel.UpdateProcThreadAttribute(
                attributes, 0, 0x0002000D, jobs, ctypes.sizeof(jobs), None, None,
            ):  # PROC_THREAD_ATTRIBUTE_JOB_LIST
                raise ctypes.WinError(ctypes.get_last_error())
            startup = ExtendedStartup()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
            information = ProcessInfo()
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline([sys.executable, *arguments]))
            environment = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper()))
                + "\0\0"
            )
            if not kernel.CreateProcessW(
                sys.executable, command_line, None, None, False,
                0x00080000 | 0x00000400 | 0x08000000,
                environment, cwd, ctypes.byref(startup), ctypes.byref(information),
            ):  # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(information.thread)
            return WindowsJobProcess(kernel, information.process, information.pid)
        finally:
            kernel.DeleteProcThreadAttributeList(attributes)

    @property
    def active_processes(self) -> int:
        information = self._accounting()
        if not self._kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(information), ctypes.sizeof(information), None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return information.ActiveProcesses

    def close(self) -> None:
        if self._handle is not None:
            handle = self._handle
            if not self._kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class WindowsJobProcess:
    """Controller-owned process handle; its job owns process termination."""

    def __init__(self, kernel, handle, pid: int):
        self._kernel = kernel
        self._handle = handle
        self.pid = pid
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL

    def poll(self):
        if self._handle is None:
            raise OSError("Worker process handle is closed")
        status = self._kernel.WaitForSingleObject(self._handle, 0)
        if status == 258:  # WAIT_TIMEOUT
            return None
        if status != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not self._kernel.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def wait(self, timeout: float):
        if not math.isfinite(timeout) or timeout < 0 or timeout * 1000 >= 0xFFFFFFFF:
            raise ValueError("Worker wait requires a bounded non-negative timeout")
        if self._handle is None:
            raise OSError("Worker process handle is closed")
        status = self._kernel.WaitForSingleObject(self._handle, math.ceil(timeout * 1000))
        if status == 258:
            raise subprocess.TimeoutExpired("boundver worker", timeout)
        if status != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def close(self):
        if self._handle is not None:
            if not self._kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
