"""Windows-only, fail-closed independent process launch with a bounded gate.

WMI is only a transport, never proof of independence. The wrapper and its exact
process creation time are inspected before the caller grants execution. The
business child is then created suspended and verified outside every Job before
its primary thread is resumed. No shell interprets caller-supplied arguments.

The random, operation-local handshake owns only one launch authorization. It is
not a business receipt, progress cache, retry policy, or persistent task state.
Callers must retain their own business intent before calling this function and
must not retry a failure whose ``dispatched`` attribute is true automatically.

The request digest protects one grant's exact argv/cwd/environment, owned by
the launcher's in-memory request. Any conflict fails closed without rerunning;
the digest expires with this handshake, not a business version or cache.
"""
from __future__ import annotations

import base64
import ctypes
import functools
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class IndependentLaunchError(RuntimeError):
    def __init__(self, code: str, *, dispatched: bool = False, pid: int | None = None,
                 winerror: int | None = None):
        # Do not attach WMI stderr, argv, credentials, or private paths.
        super().__init__(code)
        self.code = code
        self.dispatched = dispatched
        self.pid = pid
        self.winerror = winerror


_HANDSHAKE_FILES = ("request.json", "ready.json", "permit.json", "started.json", "accepted.json", "error.json")
_SCHEMA = 1
_POLL_SECONDS = 0.04
_WMI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try {
    $inputData = [Console]::In.ReadToEnd()
    $launchData = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($inputData)) | ConvertFrom-Json
    $startup = ([wmiclass]'Win32_ProcessStartup').CreateInstance()
    $startup.CreateFlags = 16777216
    $startup.ShowWindow = 0
    $startup.WinstationDesktop = 'WinSta0\Default'
    $result = ([wmiclass]'Win32_Process').Create([string]$launchData.commandLine, [string]$launchData.cwd, $startup)
    [Console]::Out.Write((@{ returnValue = [int]$result.ReturnValue; pid = [int]$result.ProcessId } | ConvertTo-Json -Compress))
} catch {
    [Console]::Out.Write('{"returnValue":-1,"pid":0}')
    exit 1
}
"""


@functools.lru_cache(maxsize=1)
def _kernel32():
    if os.name != "nt":
        raise IndependentLaunchError("INDEPENDENT_WORKER_WINDOWS_REQUIRED")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    definitions = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    }
    for name, (arguments, result) in definitions.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def _handle_identity(handle, pid: int) -> dict:
    from ctypes import wintypes

    kernel = _kernel32()
    code, in_job = wintypes.DWORD(), wintypes.BOOL()
    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not (kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            and kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                       ctypes.byref(kernel_time), ctypes.byref(user_time))
            and kernel.IsProcessInJob(handle, None, ctypes.byref(in_job))):
        raise IndependentLaunchError("INDEPENDENT_PROCESS_INSPECTION_FAILED", pid=pid)
    # FILETIME, rather than a rounded timestamp, protects against PID reuse.
    creation_time = (created.dwHighDateTime << 32) | created.dwLowDateTime
    wait_result = kernel.WaitForSingleObject(handle, 0)
    if wait_result not in (0, 258):
        raise IndependentLaunchError("INDEPENDENT_PROCESS_INSPECTION_FAILED", pid=pid)
    return {"pid": pid, "creationTime": str(creation_time), "alive": wait_result == 258,
            "inJob": bool(in_job.value)}


def process_identity(pid: int) -> dict:
    """Read exact Windows identity, liveness and *all* Job membership, or fail."""
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise IndependentLaunchError("INDEPENDENT_PROCESS_PID_INVALID")
    kernel = _kernel32()
    handle = kernel.OpenProcess(0x00101000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        raise IndependentLaunchError("INDEPENDENT_PROCESS_OPEN_FAILED", pid=pid, winerror=error)
    try:
        return _handle_identity(handle, pid)
    finally:
        kernel.CloseHandle(handle)


def require_current_independent() -> dict:
    identity = process_identity(os.getpid())
    if not identity["alive"] or identity["inJob"]:
        raise IndependentLaunchError("INDEPENDENT_PROCESS_STILL_IN_JOB", pid=os.getpid())
    return identity


def _check_folder(folder: Path) -> None:
    info = folder.lstat()
    if (not stat.S_ISDIR(info.st_mode) or folder.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400
            or folder.resolve(strict=True) != folder):
        raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_PATH_UNSAFE")


def _write(folder: Path, name: str, value: dict) -> None:
    _check_folder(folder)
    if name not in _HANDSHAKE_FILES:
        raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_FILE_INVALID")
    # Each protocol message is written exactly once; no overwrite/follow-link.
    temporary = folder / (name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        # Windows rename refuses to replace an existing message.
        temporary.rename(folder / name)
    finally:
        temporary.unlink(missing_ok=True)


def _read(folder: Path, name: str) -> dict | None:
    _check_folder(folder)
    path = folder / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 1024 * 1024 or info.st_nlink != 1):
        raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_FILE_UNSAFE")
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_INVALID")
            value[key] = item
        return value
    def invalid_number(_value):
        raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_INVALID")
    with path.open(encoding="utf-8") as handle:
        result = json.load(handle, object_pairs_hook=unique_object, parse_constant=invalid_number)
    if not isinstance(result, dict):
        raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_INVALID")
    return result


def _request_digest(request: dict | None) -> str:
    if not isinstance(request, dict):
        raise IndependentLaunchError("INDEPENDENT_REQUEST_INVALID")
    try:
        raw = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise IndependentLaunchError("INDEPENDENT_REQUEST_INVALID") from None
    return hashlib.sha256(raw).hexdigest()


def _validate_environment(environment: dict) -> None:
    if not isinstance(environment, dict):
        raise IndependentLaunchError("INDEPENDENT_ENVIRONMENT_INVALID")
    if not environment:
        return
    if set(environment) != {"__PYVENV_LAUNCHER__"}:
        raise IndependentLaunchError("INDEPENDENT_ENVIRONMENT_INVALID")
    launcher = environment["__PYVENV_LAUNCHER__"]
    if (not isinstance(launcher, str) or "\0" in launcher or not Path(launcher).is_absolute()
            or not Path(launcher).is_file() or Path(launcher).suffix.lower() != ".exe"):
        raise IndependentLaunchError("INDEPENDENT_ENVIRONMENT_INVALID")


def _cleanup(folder: Path) -> None:
    # No recursive delete: only this launch's known files and then empty folder.
    try:
        _check_folder(folder)
        for name in _HANDSHAKE_FILES:
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()
    except (OSError, IndependentLaunchError):
        pass


def _wait_message(folder: Path, name: str, nonce: str, deadline: float) -> dict:
    while time.monotonic() < deadline:
        error = _read(folder, "error.json")
        if error is not None:
            code = error.get("code", "")
            if (error.get("schema") != _SCHEMA or error.get("nonce") != nonce
                    or not isinstance(code, str) or not re.fullmatch(r"INDEPENDENT_[A-Z_]{1,70}", code)):
                code = "INDEPENDENT_WORKER_REJECTED"
            raise IndependentLaunchError(code)
        message = _read(folder, name)
        if message is not None:
            if message.get("schema") != _SCHEMA or message.get("nonce") != nonce:
                raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_IDENTITY_MISMATCH")
            return message
        time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
    raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_TIMEOUT")


def _wrapper_command(folder: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--independent-worker", str(folder)]
    # Windows venv executables are redirectors that create their own kill Job.
    # The generic wrapper needs only stdlib, so use its actual base interpreter.
    interpreter = Path(getattr(sys, "_base_executable", sys.executable))
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.is_file():
        interpreter = windowless
    return [str(interpreter), str(Path(__file__).resolve()), "--worker", str(folder)]


def _python_venv_launch(command: list[str]) -> tuple[list[str], dict]:
    """Mirror CPython multiprocessing's venv-redirector bypass, if applicable."""
    if (not getattr(sys, "frozen", False) and sys.prefix != sys.base_prefix
            and Path(command[0]).resolve() == Path(sys.executable).resolve()):
        base = Path(getattr(sys, "_base_executable", ""))
        if not base.is_absolute() or not base.is_file():
            raise IndependentLaunchError("INDEPENDENT_BASE_PYTHON_UNAVAILABLE")
        return [str(base), *command[1:]], {"__PYVENV_LAUNCHER__": command[0]}
    return command, {}


def _spawn_wmi(command: list[str], cwd: Path, timeout: float) -> int:
    # A fixed script consumes encoded data over stdin; argv never becomes code.
    script = base64.b64encode(_WMI_SCRIPT.encode("utf-16le")).decode("ascii")
    payload = base64.b64encode(json.dumps({"commandLine": subprocess.list2cmdline(command),
                                         "cwd": str(cwd)}, ensure_ascii=False).encode("utf-8")).decode("ascii")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise IndependentLaunchError("INDEPENDENT_WMI_UNAVAILABLE")
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                                 "-EncodedCommand", script], input=payload, capture_output=True, text=True,
                                timeout=max(0.001, timeout), creationflags=0x08000000, check=False)
        response = json.loads(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise IndependentLaunchError("INDEPENDENT_WMI_START_FAILED") from None
    pid = response.get("pid") if isinstance(response, dict) else None
    if (result.returncode != 0 or not isinstance(response, dict) or response.get("returnValue") != 0
            or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF):
        raise IndependentLaunchError("INDEPENDENT_WMI_START_FAILED")
    return pid


def _validate_command(command: list[str], cwd: Path) -> tuple[list[str], Path]:
    if (not isinstance(command, list) or not command
            or any(not isinstance(argument, str) or "\0" in argument for argument in command)):
        raise IndependentLaunchError("INDEPENDENT_COMMAND_INVALID")
    executable = Path(command[0])
    if not executable.is_absolute() or not executable.is_file() or executable.suffix.lower() not in {".exe", ".com"}:
        raise IndependentLaunchError("INDEPENDENT_EXECUTABLE_REQUIRED")
    try:
        directory = Path(cwd).resolve(strict=True)
        if not directory.is_dir():
            raise OSError()
    except (OSError, TypeError, ValueError):
        raise IndependentLaunchError("INDEPENDENT_CWD_INVALID") from None
    return list(command), directory


def launch_independent(command: list[str], cwd: Path, *, timeout: float = 15) -> dict:
    """Launch a verified independent lifecycle owner; never silently fall back.

    ``pid`` is the wrapper, which waits for ``childPid``. All supplied arguments
    remain local and are never included in errors/results. No parent environment
    is copied: pass required configuration explicitly in the business CLI.
    """
    if os.name != "nt":
        raise IndependentLaunchError("INDEPENDENT_WORKER_WINDOWS_REQUIRED")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise IndependentLaunchError("INDEPENDENT_TIMEOUT_INVALID")
    command, directory = _validate_command(command, cwd)
    command, environment = _python_venv_launch(command)
    deadline = time.monotonic() + timeout
    nonce = secrets.token_hex(32)
    try:
        folder = Path(tempfile.mkdtemp(prefix=".independent-launch-", dir=directory)).absolute()
    except OSError:
        raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_CREATE_FAILED") from None
    dispatched, pid = False, None
    try:
        request = {"schema": _SCHEMA, "nonce": nonce, "command": command, "cwd": str(directory),
                   "environment": environment, "expiresAt": time.time() + timeout}
        request_digest = _request_digest(request)
        _write(folder, "request.json", request)
        pid = _spawn_wmi(_wrapper_command(folder), directory, deadline - time.monotonic())
        ready = _wait_message(folder, "ready.json", nonce, deadline)
        identity = process_identity(pid)
        if (ready.get("pid") != pid or ready.get("creationTime") != identity["creationTime"]
                or not identity["alive"] or identity["inJob"]):
            raise IndependentLaunchError("INDEPENDENT_WORKER_IDENTITY_REJECTED")
        if (ready.get("request_sha256") != request_digest
                or _request_digest(_read(folder, "request.json")) != request_digest):
            raise IndependentLaunchError("INDEPENDENT_REQUEST_DIGEST_MISMATCH")
        if time.monotonic() >= deadline:
            raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_TIMEOUT")
        # The ONLY boundary that authorizes the wrapper to start business code.
        # Set conservatively before writing: a write/ack failure may be unknown.
        dispatched = True
        _write(folder, "permit.json", {"schema": _SCHEMA, "nonce": nonce,
                                      "request_sha256": request_digest, **identity})
        started = _wait_message(folder, "started.json", nonce, deadline)
        current = process_identity(pid)
        child = process_identity(started.get("childPid"))
        if (current["creationTime"] != identity["creationTime"] or not current["alive"] or current["inJob"]
                or started.get("creationTime") != identity["creationTime"]
                or started.get("request_sha256") != request_digest
                or child["creationTime"] != started.get("childCreationTime") or child["inJob"]):
            raise IndependentLaunchError("INDEPENDENT_STARTED_IDENTITY_REJECTED")
        result = {"pid": pid, "creationTime": identity["creationTime"], "childPid": child["pid"],
                  "childCreationTime": child["creationTime"], "independent": True, "transport": "wmi",
                  "dispatched": True}
        _write(folder, "accepted.json", {"schema": _SCHEMA, "nonce": nonce, "request_sha256": request_digest})
        return result
    except Exception as exc:
        code = exc.code if isinstance(exc, IndependentLaunchError) else "INDEPENDENT_LAUNCH_FAILED"
        winerror = exc.winerror if isinstance(exc, IndependentLaunchError) else None
        raise IndependentLaunchError(code, dispatched=dispatched, pid=pid, winerror=winerror) from None
    finally:
        if not dispatched:
            _cleanup(folder)


def _start_verified_child(command: list[str], cwd: Path, environment: dict | None = None) -> tuple[dict, object]:
    """Verify the exact suspended child handle before executing its first code."""
    from ctypes import wintypes

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    kernel = _kernel32()
    create = kernel.CreateProcessW
    create.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                       wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                       ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInformation)]
    create.restype = wintypes.BOOL
    startup, process = StartupInfo(), ProcessInformation()
    startup.cb, startup.dwFlags, startup.wShowWindow = ctypes.sizeof(startup), 1, 0
    command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    environment_block = None
    flags = 0x08000004
    _validate_environment({} if environment is None else environment)
    if environment:
        merged_environment = {**os.environ, **environment}
        block = "\0".join(f"{key}={value}" for key, value in sorted(merged_environment.items(), key=lambda item: item[0].upper())) + "\0\0"
        environment_block = ctypes.create_unicode_buffer(block)
        flags |= 0x400
    if not create(command[0], command_line, None, None, False, flags,
                  environment_block, str(cwd), ctypes.byref(startup), ctypes.byref(process)):
        raise IndependentLaunchError("INDEPENDENT_CHILD_CREATE_FAILED")
    resumed = False
    try:
        identity = _handle_identity(process.hProcess, int(process.dwProcessId))
        if not identity["alive"] or identity["inJob"]:
            raise IndependentLaunchError("INDEPENDENT_CHILD_STILL_IN_JOB")
        if kernel.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise IndependentLaunchError("INDEPENDENT_CHILD_RESUME_FAILED")
        resumed = True
        return identity, process.hProcess
    finally:
        kernel.CloseHandle(process.hThread)
        if not resumed:
            # This exact handle is our newly-created, never-executed child.
            kernel.TerminateProcess(process.hProcess, 1)
            kernel.CloseHandle(process.hProcess)


def worker_main(folder: str | Path) -> int:
    """Entrypoint for source Python or frozen ``--independent-worker PATH``."""
    directory = Path(folder).absolute()
    child_handle = None
    nonce = ""
    owned_folder = False
    try:
        _check_folder(directory)
        if not re.fullmatch(r"\.independent-launch-[A-Za-z0-9_-]{8,}", directory.name):
            raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_PATH_UNSAFE")
        request = _read(directory, "request.json")
        if not request or request.get("schema") != _SCHEMA:
            raise IndependentLaunchError("INDEPENDENT_REQUEST_INVALID")
        nonce = request.get("nonce")
        expires = request.get("expiresAt")
        if (not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce) or not isinstance(expires, (int, float))
                or not math.isfinite(expires)):
            raise IndependentLaunchError("INDEPENDENT_REQUEST_INVALID")
        command, cwd = _validate_command(request.get("command"), Path(request.get("cwd", "")))
        _validate_environment(request.get("environment", {}))
        request_digest = _request_digest(request)
        if directory.parent != cwd or not directory.name.startswith(".independent-launch-"):
            raise IndependentLaunchError("INDEPENDENT_HANDSHAKE_PATH_UNSAFE")
        owned_folder = True
        if not 0 < expires - time.time() <= 60:
            raise IndependentLaunchError("INDEPENDENT_REQUEST_EXPIRED")
        identity = require_current_independent()
        _write(directory, "ready.json", {"schema": _SCHEMA, "nonce": nonce,
                                         "request_sha256": request_digest, **identity})
        deadline = time.monotonic() + min(60, expires - time.time())
        permit = _wait_message(directory, "permit.json", nonce, deadline)
        latest = require_current_independent()
        if (permit.get("pid") != identity["pid"] or permit.get("creationTime") != identity["creationTime"]
                or latest != identity or time.time() >= expires):
            raise IndependentLaunchError("INDEPENDENT_PERMIT_REJECTED")
        if (permit.get("request_sha256") != request_digest
                or _request_digest(_read(directory, "request.json")) != request_digest):
            raise IndependentLaunchError("INDEPENDENT_REQUEST_DIGEST_MISMATCH")
        child, child_handle = _start_verified_child(command, cwd, request.get("environment"))
        # Drop command-bearing payload before publishing the launch ack.
        (directory / "request.json").unlink()
        _write(directory, "started.json", {"schema": _SCHEMA, "nonce": nonce,
                                           "request_sha256": request_digest,
                                           "creationTime": identity["creationTime"], "childPid": child["pid"],
                                           "childCreationTime": child["creationTime"]})
        try:
            accepted = _wait_message(directory, "accepted.json", nonce, deadline)
            if accepted.get("request_sha256") != request_digest:
                raise IndependentLaunchError("INDEPENDENT_REQUEST_DIGEST_MISMATCH")
        except (IndependentLaunchError, OSError, ValueError):
            # Permit already authorized execution. Losing its ack never cancels
            # or retries business work; its own durable receipt owns recovery.
            pass
        _cleanup(directory)
        kernel = _kernel32()
        if kernel.WaitForSingleObject(child_handle, 0xFFFFFFFF) != 0:
            raise IndependentLaunchError("INDEPENDENT_CHILD_WAIT_FAILED")
        from ctypes import wintypes
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(child_handle, ctypes.byref(code)):
            raise IndependentLaunchError("INDEPENDENT_CHILD_EXIT_QUERY_FAILED")
        return int(code.value)
    except Exception as exc:
        try:
            if owned_folder:
                code = exc.code if isinstance(exc, IndependentLaunchError) else "INDEPENDENT_WORKER_REJECTED"
                _write(directory, "error.json", {"schema": _SCHEMA, "nonce": nonce, "code": code})
                # Give the bounded launcher one poll to consume the sanitized error.
                time.sleep(0.15)
        except Exception:
            pass
        if child_handle is not None:
            # Even an ack/filesystem failure must not abandon an authorized
            # child while its lifecycle-owner PID falsely looks finished.
            _kernel32().WaitForSingleObject(child_handle, 0xFFFFFFFF)
        return 1
    finally:
        if child_handle is not None:
            _kernel32().CloseHandle(child_handle)
        if owned_folder:
            _cleanup(directory)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(worker_main(sys.argv[2]))
    raise SystemExit(2)
