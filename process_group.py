"""Tying child processes to the lifetime of this one.

The server can terminate a conversion itself on a clean shutdown, but it cannot
run any code at all when it is force-killed (`taskkill /F`, `kill -9`). Without
help from the OS, a transcode started moments earlier keeps running with no
parent, burning CPU and writing into a temp directory nobody will collect.

Both platforms can be told to do the cleanup for us:

* Windows: put every child in a job object created with
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The job's only handle belongs to this
  process, so when this process dies -- however it dies -- the handle closes,
  the job goes with it, and the kernel kills everything inside.
* POSIX: start children in their own process group so a clean shutdown can
  signal the whole group. A SIGKILL of the parent still cannot be caught, so
  this is a partial measure there.
"""
from __future__ import annotations

import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"

_job_handle = None
_job_failed = False


# --------------------------------------------------------------------- win32

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Pin the signatures: with default ctypes argument conversion a 64-bit
    # HANDLE is passed as a 32-bit int and the call fails with a truncated
    # handle.
    _K32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _K32.CreateJobObjectW.restype = wintypes.HANDLE
    _K32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _K32.SetInformationJobObject.restype = wintypes.BOOL
    _K32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _K32.AssignProcessToJobObject.restype = wintypes.BOOL
    _K32.CloseHandle.argtypes = [wintypes.HANDLE]
    _K32.CloseHandle.restype = wintypes.BOOL

    def _kernel32():
        return _K32


def _ensure_job() -> int | None:
    """The kill-on-close job object, created once. None if unavailable."""
    global _job_handle, _job_failed
    if _job_handle is not None or _job_failed:
        return _job_handle
    try:
        k32 = _kernel32()
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            handle, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            k32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

        _job_handle = handle
        return _job_handle
    except Exception:
        # Nested-job restrictions or a locked-down environment. Losing this is
        # a degradation, not a failure: clean shutdown still kills children.
        _job_failed = True
        return None


def spawn_kwargs() -> dict:
    """Extra Popen keyword arguments for a child that should not outlive us."""
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # A child in its own group does not receive the console's Ctrl+C, so
        # the server decides when it dies instead of the terminal.
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def adopt(proc: subprocess.Popen) -> bool:
    """Bind a freshly spawned child to this process's lifetime.

    Returns True when the OS will now clean the child up for us.
    """
    if not IS_WINDOWS:
        return False
    handle = _ensure_job()
    if handle is None:
        return False
    try:
        k32 = _kernel32()
        # Popen._handle is the process HANDLE on Windows. It is private, but it
        # is the only route to the handle, and it has been stable for years.
        child_handle = int(getattr(proc, "_handle", 0))
        if not child_handle:
            return False
        # Job first, then process -- the reverse order fails with
        # ERROR_INVALID_HANDLE and looks exactly like a permissions problem.
        return bool(k32.AssignProcessToJobObject(
            wintypes.HANDLE(handle), wintypes.HANDLE(child_handle)))
    except Exception:
        return False


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a child and, on POSIX, anything it spawned alongside it."""
    if IS_WINDOWS:
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def status() -> str:
    """One line for the startup log, so the guarantee is visible."""
    if not IS_WINDOWS:
        return "child processes run in their own process group"
    if _ensure_job() is not None:
        return "child processes are killed with the server (win32 job object)"
    return "job object unavailable; children survive a force-kill"
