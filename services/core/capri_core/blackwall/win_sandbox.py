"""Windows containment for Blackwall child processes.

The generated tool never runs in the daemon. On Windows this module goes one
step further than ``subprocess.run``:

* create the child suspended;
* put it in a Job Object with an active-process limit, memory ceilings and
  kill-on-close;
* give it a restricted token when the account allows one;
* only then resume the primary thread.

The pipe plumbing is deliberately small. ``spawn`` returns a process whose
stdin/stdout/stderr are binary file objects, so the same helper can serve both
the one-shot test runner and a warm worker. Every failure falls back to the
caller's ordinary subprocess path rather than making the daemon unusable.
"""

from __future__ import annotations

import ctypes
import logging
import msvcrt
import os
import subprocess
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger("capri.blackwall.win_sandbox")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
LPCWSTR = wintypes.LPCWSTR
LPWSTR = wintypes.LPWSTR
LPVOID = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
LOGON_WITH_PROFILE = 0x00000001
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
HANDLE_FLAG_INHERIT = 0x00000001
TOKEN_ALL_ACCESS = 0x000F01FF
DISABLE_MAX_PRIVILEGE = 0x00000001
LUA_TOKEN = 0x00000004
SE_PRIVILEGE_ENABLED = 0x00000002
STATUS_DLL_INIT_FAILED = 0xC0000142

# Try strongest token restriction first, stepping down only if the session's
# WindowStation/Desktop rejects restricted-token DLL initialization.
_TOKEN_MODES: tuple[int | None, ...] = (
    DISABLE_MAX_PRIVILEGE | LUA_TOKEN,
    DISABLE_MAX_PRIVILEGE,
    None,
)
_TOKEN_LOCK = threading.Lock()
_TOKEN_INDEX = 0
_TOKEN_VERIFIED = False

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# Keep a generated tool far below the daemon's own footprint. A Python child
# needs room for the interpreter, but not a gigabyte.
PROCESS_MEMORY_LIMIT = 512 * 1024 * 1024
JOB_MEMORY_LIMIT = 768 * 1024 * 1024


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
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", wintypes.LONG)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", DWORD)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", DWORD), ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


class _STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


def _setup() -> None:
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = HANDLE
    kernel32.CreateJobObjectW.argtypes = [LPVOID, LPCWSTR]
    kernel32.CreateJobObjectW.restype = HANDLE
    kernel32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD]
    kernel32.SetInformationJobObject.restype = BOOL
    kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
    kernel32.AssignProcessToJobObject.restype = BOOL
    kernel32.TerminateJobObject.argtypes = [HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = BOOL
    kernel32.TerminateProcess.argtypes = [HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = BOOL
    kernel32.ResumeThread.argtypes = [HANDLE]
    kernel32.ResumeThread.restype = DWORD
    kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    kernel32.WaitForSingleObject.restype = DWORD
    kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    kernel32.GetExitCodeProcess.restype = BOOL
    kernel32.SetHandleInformation.argtypes = [HANDLE, DWORD, DWORD]
    kernel32.SetHandleInformation.restype = BOOL

    advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    advapi32.OpenProcessToken.restype = BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        HANDLE,
        DWORD,
        DWORD,
        LPVOID,
        DWORD,
        LPVOID,
        DWORD,
        LPVOID,
        ctypes.POINTER(HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = BOOL
    advapi32.LookupPrivilegeValueW.argtypes = [LPCWSTR, LPCWSTR, ctypes.POINTER(_LUID)]
    advapi32.LookupPrivilegeValueW.restype = BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        HANDLE,
        BOOL,
        ctypes.POINTER(_TOKEN_PRIVILEGES),
        DWORD,
        LPVOID,
        LPVOID,
    ]
    advapi32.AdjustTokenPrivileges.restype = BOOL

    advapi32.CreateProcessAsUserW.argtypes = [
        HANDLE,
        LPCWSTR,
        LPWSTR,
        LPVOID,
        LPVOID,
        BOOL,
        DWORD,
        LPVOID,
        LPCWSTR,
        ctypes.POINTER(_STARTUPINFO),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessAsUserW.restype = BOOL
    advapi32.CreateProcessWithTokenW.argtypes = [
        HANDLE,
        DWORD,
        LPCWSTR,
        LPWSTR,
        DWORD,
        LPVOID,
        LPCWSTR,
        ctypes.POINTER(_STARTUPINFO),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessWithTokenW.restype = BOOL
    kernel32.CreateProcessW.argtypes = [
        LPCWSTR,
        LPWSTR,
        LPVOID,
        LPVOID,
        BOOL,
        DWORD,
        LPVOID,
        LPCWSTR,
        ctypes.POINTER(_STARTUPINFO),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = BOOL


_setup()


class SandboxError(OSError):
    pass


@dataclass
class SandboxProcess:
    handle: int
    job: int
    pid: int
    stdin: BinaryIO | None
    stdout: BinaryIO
    stderr: BinaryIO
    restricted: bool
    jobbed: bool

    def wait(self, timeout_ms: int) -> tuple[int, bool]:
        timeout = max(1, min(int(timeout_ms), 300_000))
        result = kernel32.WaitForSingleObject(HANDLE(self.handle), timeout)
        timed_out = result == WAIT_TIMEOUT
        if timed_out:
            if self.jobbed and self.job:
                kernel32.TerminateJobObject(HANDLE(self.job), 1)
            else:
                kernel32.TerminateProcess(HANDLE(self.handle), 1)
            kernel32.WaitForSingleObject(HANDLE(self.handle), 5_000)
        code = DWORD()
        if not kernel32.GetExitCodeProcess(HANDLE(self.handle), ctypes.byref(code)):
            code.value = 1
        return int(code.value), timed_out

    def terminate(self) -> None:
        if self.jobbed and self.job:
            kernel32.TerminateJobObject(HANDLE(self.job), 1)
        else:
            kernel32.TerminateProcess(HANDLE(self.handle), 1)

    def close(self) -> None:
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self.stdin = None
        self.stdout = None
        self.stderr = None
        if self.handle:
            kernel32.CloseHandle(HANDLE(self.handle))
            self.handle = 0
        if self.job:
            # Closing a kill-on-close job is the final backstop for any child
            # the generated tool managed to start before the audit stopped it.
            kernel32.CloseHandle(HANDLE(self.job))
            self.job = 0


def _make_job() -> tuple[int, bool]:
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return 0, False
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = PROCESS_MEMORY_LIMIT
    info.JobMemoryLimit = JOB_MEMORY_LIMIT
    if not kernel32.SetInformationJobObject(
        HANDLE(job),
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(HANDLE(job))
        return 0, False
    return int(job), True


def _enable_impersonate(token: HANDLE) -> None:
    """CreateProcessWithTokenW needs SeImpersonatePrivilege.

    It is normally present but disabled on interactive accounts. Enabling it
    for this already-open token is local and reversible; if the account does
    not hold it, the caller still has the Job Object fallback.
    """
    luid = _LUID()
    if not advapi32.LookupPrivilegeValueW(None, "SeImpersonatePrivilege", ctypes.byref(luid)):
        return
    privileges = _TOKEN_PRIVILEGES()
    privileges.PrivilegeCount = 1
    privileges.Privileges[0].Luid = luid
    privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    advapi32.AdjustTokenPrivileges(
        token, False, ctypes.byref(privileges), 0, None, None
    )


def _restricted_token(flags: int | None = None) -> tuple[int, bool]:
    current = HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_ALL_ACCESS, ctypes.byref(current)
    ):
        return 0, False
    try:
        _enable_impersonate(current)
        candidates = (
            (flags,)
            if flags is not None
            else (DISABLE_MAX_PRIVILEGE | LUA_TOKEN, DISABLE_MAX_PRIVILEGE)
        )
        for candidate in candidates:
            restricted = HANDLE()
            if advapi32.CreateRestrictedToken(
                current,
                candidate,
                0,
                None,
                0,
                None,
                0,
                None,
                ctypes.byref(restricted),
            ):
                return int(restricted.value), True
        return 0, False
    finally:
        kernel32.CloseHandle(current)


def _environment(env: dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    entries = [f"{key}={value}\0" for key, value in sorted(env.items())]
    entries.append("\0")
    return ctypes.create_unicode_buffer("".join(entries))


def _pipe() -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, True)
    handle = msvcrt.get_osfhandle(write_fd)
    kernel32.SetHandleInformation(
        HANDLE(handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT
    )
    return read_fd, write_fd


def _file(fd: int, mode: str) -> BinaryIO:
    return os.fdopen(fd, mode, buffering=0)


def _spawn_with_mode(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    want_stdin: bool,
    token_flags: int | None,
) -> SandboxProcess:
    token, has_token = (
        _restricted_token(token_flags) if token_flags is not None else (0, False)
    )
    job, has_job = _make_job()
    in_read = in_write = -1
    out_read = out_write = -1
    err_read = err_write = -1
    nul_fd = -1
    created = False
    used_token = False
    process_info = _PROCESS_INFORMATION()
    try:
        if want_stdin:
            in_read, in_write = os.pipe()
            os.set_inheritable(in_read, True)
            os.set_inheritable(in_write, False)
            kernel32.SetHandleInformation(
                HANDLE(msvcrt.get_osfhandle(in_read)),
                HANDLE_FLAG_INHERIT,
                HANDLE_FLAG_INHERIT,
            )
            stdin_handle = msvcrt.get_osfhandle(in_read)
        else:
            nul_fd = os.open(os.devnull, os.O_RDONLY)
            os.set_inheritable(nul_fd, True)
            kernel32.SetHandleInformation(
                HANDLE(msvcrt.get_osfhandle(nul_fd)),
                HANDLE_FLAG_INHERIT,
                HANDLE_FLAG_INHERIT,
            )
            stdin_handle = msvcrt.get_osfhandle(nul_fd)

        out_read, out_write = _pipe()
        err_read, err_write = _pipe()
        startup = _STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        startup.wShowWindow = SW_HIDE
        startup.hStdInput = HANDLE(stdin_handle)
        startup.hStdOutput = HANDLE(msvcrt.get_osfhandle(out_write))
        startup.hStdError = HANDLE(msvcrt.get_osfhandle(err_write))

        env_block = _environment(env)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        application = command[0] if command and os.path.isabs(command[0]) else sys.executable
        flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT

        if has_token:
            # CreateProcessAsUserW works with the restricted primary token on
            # accounts where CreateProcessWithTokenW refuses for lack of
            # SeImpersonatePrivilege. Try it first; the token path is the point.
            created = bool(
                advapi32.CreateProcessAsUserW(
                    HANDLE(token),
                    application,
                    command_line,
                    None,
                    None,
                    True,
                    flags,
                    ctypes.cast(env_block, LPVOID),
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(process_info),
                )
            )
            if not created:
                created = bool(
                    advapi32.CreateProcessWithTokenW(
                        HANDLE(token),
                        0,
                        application,
                        command_line,
                        flags,
                        ctypes.cast(env_block, LPVOID),
                        str(cwd),
                        ctypes.byref(startup),
                        ctypes.byref(process_info),
                    )
                )
            used_token = created
        if not created:
            # The account may not hold SeImpersonatePrivilege. Still keep the
            # suspended launch and the Job Object; only the token is skipped.
            created = bool(
                kernel32.CreateProcessW(
                    application,
                    command_line,
                    None,
                    None,
                    True,
                    flags,
                    ctypes.cast(env_block, LPVOID),
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(process_info),
                )
            )
        if not created:
            raise SandboxError(
                f"CreateProcess failed (Windows error {ctypes.get_last_error()})"
            )

        # The child owns duplicates now. Close every child-side handle in the
        # parent before waiting on the process.
        for fd in (in_read if want_stdin else nul_fd, out_write, err_write):
            if fd >= 0:
                os.close(fd)
        in_read = out_write = err_write = -1
        nul_fd = -1

        jobbed = bool(
            has_job
            and kernel32.AssignProcessToJobObject(
                HANDLE(job), HANDLE(process_info.hProcess)
            )
        )
        resumed = kernel32.ResumeThread(HANDLE(process_info.hThread))
        if resumed == 0xFFFFFFFF:
            if jobbed:
                kernel32.TerminateJobObject(HANDLE(job), 1)
            else:
                kernel32.TerminateProcess(HANDLE(process_info.hProcess), 1)
            raise SandboxError(
                f"ResumeThread failed (Windows error {ctypes.get_last_error()})"
            )

        stdin = _file(in_write, "wb") if want_stdin else None
        in_write = -1
        stdout = _file(out_read, "rb")
        out_read = -1
        stderr = _file(err_read, "rb")
        err_read = -1
        kernel32.CloseHandle(HANDLE(process_info.hThread))
        process_info.hThread = HANDLE(0)
        process = SandboxProcess(
            handle=int(process_info.hProcess),
            job=job if jobbed else 0,
            pid=int(process_info.dwProcessId),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            restricted=used_token,
            jobbed=jobbed,
        )
        # Ownership moved into the SandboxProcess.
        process_info.hProcess = HANDLE(0)
        job = 0
        return process
    finally:
        if token:
            kernel32.CloseHandle(HANDLE(token))
        if job:
            kernel32.CloseHandle(HANDLE(job))
        for fd in (in_read, in_write, out_read, out_write, err_read, err_write, nul_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if process_info.hProcess:
            kernel32.CloseHandle(process_info.hProcess)
        if process_info.hThread:
            kernel32.CloseHandle(process_info.hThread)


def _probe_dll_init(process: SandboxProcess) -> bool:
    """True when a newly launched child survives past early Windows DLL init."""
    if not process.restricted:
        return True
    result = kernel32.WaitForSingleObject(HANDLE(process.handle), 200)
    if result != WAIT_OBJECT_0:
        return True
    code = DWORD()
    if not kernel32.GetExitCodeProcess(HANDLE(process.handle), ctypes.byref(code)):
        return True
    return int(code.value) != STATUS_DLL_INIT_FAILED


def _advance_token_mode(failed_index: int) -> None:
    global _TOKEN_INDEX, _TOKEN_VERIFIED
    with _TOKEN_LOCK:
        if _TOKEN_INDEX == failed_index and _TOKEN_INDEX + 1 < len(_TOKEN_MODES):
            _TOKEN_INDEX += 1
            _TOKEN_VERIFIED = False


def spawn(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    want_stdin: bool = False,
) -> SandboxProcess:
    """Launch one contained child. Raises SandboxError if Windows refuses."""
    if os.name != "nt":
        raise SandboxError("win_sandbox is Windows-only")

    global _TOKEN_INDEX, _TOKEN_VERIFIED
    with _TOKEN_LOCK:
        index = _TOKEN_INDEX
        verified = _TOKEN_VERIFIED

    if verified:
        return _spawn_with_mode(
            command,
            cwd=cwd,
            env=env,
            want_stdin=want_stdin,
            token_flags=_TOKEN_MODES[index],
        )

    for candidate_index in range(index, len(_TOKEN_MODES)):
        process = _spawn_with_mode(
            command,
            cwd=cwd,
            env=env,
            want_stdin=want_stdin,
            token_flags=_TOKEN_MODES[candidate_index],
        )
        if _probe_dll_init(process):
            with _TOKEN_LOCK:
                if _TOKEN_INDEX <= candidate_index:
                    _TOKEN_INDEX = candidate_index
                    _TOKEN_VERIFIED = True
            return process
        log.warning(
            "restricted sandbox child exited 0x%x during DLL init (mode=%s); stepping down",
            STATUS_DLL_INIT_FAILED,
            _TOKEN_MODES[candidate_index],
        )
        process.close()
        _advance_token_mode(candidate_index)

    return _spawn_with_mode(
        command,
        cwd=cwd,
        env=env,
        want_stdin=want_stdin,
        token_flags=None,
    )


def _run_once(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
) -> tuple[int, str, str, bool, bool, bool]:
    process = spawn(command, cwd=cwd, env=env)
    output = bytearray()
    error = bytearray()

    def drain(stream: BinaryIO, target: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                target.extend(chunk)
                if len(target) > 40_000:
                    del target[: len(target) - 40_000]
        except OSError:
            return

    readers = [
        threading.Thread(target=drain, args=(process.stdout, output), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, error), daemon=True),
    ]
    for reader in readers:
        reader.start()
    code, timed_out = process.wait(int(max(0.1, timeout_s) * 1000))
    for reader in readers:
        reader.join(2.0)
    process.close()
    return (
        code,
        output.decode("utf-8", "replace"),
        error.decode("utf-8", "replace"),
        timed_out,
        process.restricted,
        process.jobbed,
    )


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
) -> tuple[int, str, str, bool, bool, bool]:
    """Run one command to completion. Returns (code, out, err, timed_out, restricted, jobbed)."""
    for _ in range(len(_TOKEN_MODES)):
        with _TOKEN_LOCK:
            index = _TOKEN_INDEX
        result = _run_once(command, cwd=cwd, env=env, timeout_s=timeout_s)
        code, _out, _err, timed_out, restricted, _jobbed = result
        if not timed_out and restricted and code == STATUS_DLL_INIT_FAILED:
            log.warning(
                "restricted sandbox child exited 0x%x; retrying with next containment mode",
                STATUS_DLL_INIT_FAILED,
            )
            _advance_token_mode(index)
            continue
        return result
    return _run_once(command, cwd=cwd, env=env, timeout_s=timeout_s)
