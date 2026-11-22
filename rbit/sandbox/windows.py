"""Windows backend: an AppContainer plus a Job Object, driven through ctypes.

* **AppContainer** (the isolation used by Edge and UWP apps): the process runs with a
  low-box token. It has no access to anything whose ACL does not name its container SID
  (or "all application packages"): not the user's profile, documents, SSH keys, other
  drives. The workspace gets an explicit modify ACE for the container; interpreters and
  tools outside the system directories get a read/execute ACE. With no network
  capability declared the process has no network access at all (also not loopback).
* **Job Object**: the whole process tree dies with the job (`kill_tree`), is capped in
  number of processes, and has no access to the clipboard, desktop settings or other
  windows' handles.
* **Handle list**: only the three standard handles are inherited, nothing else leaks in.

Grants are recorded in `~/.rbit/sandbox-grants.json`; `revoke_all()` removes every ACE and
the container profile again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policy import Policy, sandbox_env

logger = logging.getLogger(__name__)

PROFILE_NAME = "rbit.sandbox.v1"
GRANTS_FILE = Path.home() / ".rbit" / "sandbox-grants.json"

IS_WINDOWS = sys.platform == "win32"


class WindowsSandboxError(RuntimeError):
    pass


def available() -> bool:
    return IS_WINDOWS and _load_api() is not None


# ---------------------------------------------------------------------- Win32 API

_API: Any = None
_API_FAILED = False


def _load_api() -> Any:
    """Declares the Win32 structures and functions once; None when they are not usable."""
    global _API, _API_FAILED
    if _API is not None or _API_FAILED or not IS_WINDOWS:
        return _API
    try:
        _API = _build_api()
    except (OSError, AttributeError):
        _API_FAILED = True
    return _API


def _build_api() -> Any:
    import ctypes
    from ctypes import wintypes

    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv.CreateAppContainerProfile  # noqa: B018 - raises AttributeError on systems without AppContainers

    PSID = ctypes.c_void_p

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", PSID), ("Capabilities", ctypes.c_void_p), ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", PSID), ("Attributes", wintypes.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(PSID)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(PSID)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    userenv.DeleteAppContainerProfile.restype = ctypes.c_long
    advapi32.ConvertSidToStringSidW.argtypes = [PSID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(PSID)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION)]
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

    ns = type("Win32", (), {})()
    for name, value in list(locals().items()):
        if name not in ("ns", "name", "value") and not name.startswith("__"):
            setattr(ns, name, value)
    return ns


# -------------------------------------------------------------- container profile


_sid_cache: dict[str, tuple[Any, str]] = {}
_sid_lock = threading.Lock()


def profile_name(key: str = "") -> str:
    return PROFILE_NAME + (f".{key}" if key else "")


def container_sid(key: str = "") -> tuple[Any, str]:
    """(PSID, 'S-1-15-2-...') of the rbit AppContainer for this widening key, created on first use.

    Every distinct set of extra folders gets its OWN container identity. With one shared
    identity a folder opened for one MCP server stayed open for every other program, because
    the permission lives on the folder, not on the program.
    """
    api = _load_api()
    if api is None:
        raise WindowsSandboxError("AppContainers are not available on this system")
    with _sid_lock:
        if key in _sid_cache:
            return _sid_cache[key]
        import ctypes
        from ctypes import wintypes

        name = profile_name(key)
        sid = ctypes.c_void_p()
        hr = api.userenv.CreateAppContainerProfile(name, "rbit sandbox", "Isolated programs run by rbit", None, 0, ctypes.byref(sid))
        if (hr & 0xFFFFFFFF) == 0x800700B7:  # already exists
            hr = api.userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
        if hr != 0:
            raise WindowsSandboxError(f"cannot create the AppContainer profile (0x{hr & 0xFFFFFFFF:08x})")
        text = wintypes.LPWSTR()
        if not api.advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise WindowsSandboxError("cannot format the AppContainer SID")
        value = text.value or ""
        api.kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        _sid_cache[key] = (sid, value)
        return _sid_cache[key]


# ----------------------------------------------------------------------- desktop

_desktops: dict[str, tuple[int, str]] = {}


def _private_desktop(sid_text: str) -> str | None:
    """A desktop only this container (and the user) can open, so a sandboxed program cannot
    take screenshots of, or send input to, the user's real desktop. None when it cannot be made
    (the program then starts on the default desktop; everything else about the sandbox holds)."""
    if sid_text in _desktops:
        return _desktops[sid_text][1]
    api = _load_api()
    import ctypes

    descriptor = ctypes.c_void_p()
    sddl = f"D:(A;;GA;;;AU)(A;;GA;;;{sid_text})"
    if not api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        logger.warning("sandbox: no private desktop (security descriptor): the default desktop is used")
        return None
    attributes = api.SECURITY_ATTRIBUTES(ctypes.sizeof(api.SECURITY_ATTRIBUTES), descriptor, False)
    name = "rbit-sandbox-" + sid_text.rsplit("-", 1)[-1]
    handle = api.user32.CreateDesktopW(name, None, None, 0, 0x10000000, ctypes.byref(attributes))
    api.kernel32.LocalFree(descriptor)
    if not handle:
        logger.warning("sandbox: no private desktop (error %s): the default desktop is used", ctypes.get_last_error())
        return None
    _desktops[sid_text] = (int(handle), "WinSta0\\" + name)  # the handle stays open: it keeps the desktop alive
    return _desktops[sid_text][1]


# ------------------------------------------------------------------------ grants

_grants_lock = threading.Lock()
_granted_this_process: set[tuple[str, str, str]] = set()


def _system_dirs() -> tuple[Path, ...]:
    values = [os.environ.get(k) for k in ("SystemRoot", "ProgramFiles", "ProgramFiles(X86)", "ProgramW6432", "ProgramData")]
    return tuple(Path(v).resolve() for v in values if v)


def _is_under(path: Path, parents: tuple[Path, ...]) -> bool:
    for parent in parents:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            continue
    return False


def _grantable(path: Path) -> None:
    """Refuses grants that would hand the whole machine or the whole profile to the sandbox."""
    resolved = path.resolve()
    home = Path.home().resolve()
    if resolved.parent == resolved:
        raise WindowsSandboxError(f"refusing to grant a drive root: {resolved}")
    if resolved == home or resolved in home.parents:
        raise WindowsSandboxError(
            f"refusing to open {resolved} to the sandbox: it contains the whole user profile. "
            "Use a project folder as the workspace."
        )


def _icacls(path: Path, *args: str) -> None:
    proc = subprocess.run(["icacls", str(path), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).decode("oem", errors="replace").strip()
        raise WindowsSandboxError(f"icacls {' '.join(args)} failed for {path}: {detail}")


def _identity(path: Path) -> str:
    """Volume and file index of `path`: a deleted and re-created folder is a different object."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_dev}:{st.st_ino}"


def _load_ledger() -> dict[str, Any]:
    try:
        data = json.loads(GRANTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record(entry: dict[str, str]) -> None:
    with _grants_lock:
        data = _load_ledger()
        grants = data.get("grants") if isinstance(data.get("grants"), list) else []
        grants = [g for g in grants if isinstance(g, dict) and Path(str(g.get("path", ""))).exists()]
        if entry not in grants:
            grants.append(entry)
        profiles = sorted({str(g.get("profile", "")) for g in grants if g.get("profile") is not None})
        GRANTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        GRANTS_FILE.write_text(json.dumps({"grants": grants, "profiles": profiles}, indent=2), encoding="utf-8")


def _recorded(path: Path, permission: str, sid_text: str) -> bool:
    """True when this grant was made for THIS container identity on THIS very folder."""
    wanted = str(path)
    identity = _identity(path)
    return any(
        isinstance(e, dict)
        and e.get("path") == wanted
        and e.get("permission") == permission
        and e.get("sid") == sid_text
        and e.get("id") == identity
        for e in (_load_ledger().get("grants") or [])
    )


#: How many directory entries are looked at when searching for hard links (a huge tree is
#: not walked to the end: a warning is logged instead).
_SCAN_LIMIT = 400_000


def _multi_linked_files(root: Path) -> tuple[list[Path], bool]:
    """Files under `root` that have more than one hard link, and whether the scan was complete.

    The walk never follows symbolic links or junctions. A hard link is the same file object
    as its other names, so a permission granted on the workspace would also be granted on the
    copy of the file that lives OUTSIDE it.
    """
    found: list[Path] = []
    stack = [root]
    seen = 0
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > _SCAN_LIMIT:
                        return found, False
                    try:
                        if entry.is_symlink() or entry.is_junction():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif os.stat(entry.path, follow_symlinks=False).st_nlink > 1:  # (DirEntry.stat leaves st_nlink 0)
                            found.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return found, True


def grant(path: Path, permission: str, key: str = "") -> None:
    """Gives the container `permission` ("M" modify, "RX" read+execute) on `path`, inherited below it."""
    _sid, sid_text = container_sid(key)
    memo = (str(path), permission, sid_text)
    if memo in _granted_this_process:
        return
    _grantable(path)
    if permission == "RX" and (_is_under(path.resolve(), _system_dirs())):
        return  # already readable by "all application packages"
    if not _recorded(path, permission, sid_text):
        linked, complete = _multi_linked_files(path) if path.is_dir() else ([], True)
        # A grant is inherited by every file below the folder, and a hard-linked file is ONE object
        # with several names, some of them outside the folder. Stop such files from inheriting
        # BEFORE granting (an inherited entry cannot be removed afterwards): they stay closed to
        # the sandbox under every name.
        for victim in linked:
            try:
                _icacls(victim, "/inheritance:d")
            except WindowsSandboxError:
                pass
        _icacls(path, "/grant", f"*{sid_text}:(OI)(CI){permission}")
        _record({"sid": sid_text, "profile": profile_name(key), "path": str(path), "permission": permission, "id": _identity(path)})
        if linked:
            logger.warning(
                "sandbox: %d hard-linked file(s) in %s were kept closed to the sandbox (first: %s)",
                len(linked), path, linked[0],
            )
        if not complete:
            logger.warning("sandbox: %s is too large to search for hard links; some may have been opened", path)
    _granted_this_process.add(memo)


def protect(path: Path, key: str = "", *, read_only: bool = False) -> None:
    """Takes the container's access to `path` away again, or leaves it read-only.

    Used for the project's own `.rbit` state (no access) and for `.git/hooks` and `.git/config`
    (read-only: a hook written from inside would later run OUTSIDE the sandbox).

    A deny entry is not enough: for an AppContainer, deny entries naming the container are not
    honoured here, only the absence of an allow entry is. So the folder stops inheriting the
    workspace's grant and the container's entry is removed.
    """
    _sid, sid_text = container_sid(key)
    label = "read-only" if read_only else "no-access"
    memo = (str(path), label, sid_text)
    if memo in _granted_this_process:
        return
    if not _recorded(path, label, sid_text):
        _icacls(path, "/inheritance:d")  # keep the other entries, but as explicit ones
        _icacls(path, "/remove", f"*{sid_text}")
        if read_only:
            inherit = "(OI)(CI)" if path.is_dir() else ""
            _icacls(path, "/grant", f"*{sid_text}:{inherit}R")
        _record({"sid": sid_text, "profile": profile_name(key), "path": str(path), "permission": label, "id": _identity(path)})
    _granted_this_process.add(memo)


def revoke_all() -> int:
    """Removes every ACE rbit added and deletes every container profile. Returns the number removed."""
    data = _load_ledger()
    removed = 0
    for entry in data.get("grants", []) if isinstance(data.get("grants"), list) else []:
        path, sid = Path(entry.get("path", "")), entry.get("sid", "")
        if path.exists() and sid:
            try:
                _icacls(path, "/remove", f"*{sid}")
                removed += 1
            except WindowsSandboxError:
                pass
    api = _load_api()
    if api is not None:
        names = {PROFILE_NAME, *(str(n) for n in (data.get("profiles") or []) if n)}
        for name in names:
            api.userenv.DeleteAppContainerProfile(name)
    _granted_this_process.clear()
    _sid_cache.clear()
    try:
        GRANTS_FILE.unlink()
    except OSError:
        pass
    return removed


def prepare(policy: Policy, executable: str | None) -> None:
    """Makes sure the container can reach what the policy lists (once per path and process)."""
    key = policy.extras_key
    workspace = policy.workspace
    grant(workspace, "M", key)
    rbit_dir = workspace / ".rbit"
    rbit_dir.mkdir(exist_ok=True)
    protect(rbit_dir, key)  # sandboxed commands must not rewrite rbit's config, audit log or sessions
    for name in ("hooks", "config"):
        target = workspace / ".git" / name
        if target.exists():
            protect(target, key, read_only=True)
    for path in policy.write_paths:
        if path != workspace:
            path.mkdir(parents=True, exist_ok=True)
            grant(path, "M", key)
    for path in policy.read_paths:
        if path.exists():
            grant(path, "RX", key)
    if executable:
        exe_dir = Path(executable).resolve().parent
        if not _is_under(exe_dir, _system_dirs()):
            grant(exe_dir, "RX", key)


# ----------------------------------------------------------------------- process


@dataclass
class _Handles:
    process: int
    thread: int
    job: int


class WinProcess:
    """Looks like `asyncio.subprocess.Process`: pid, returncode, stdin/stdout/stderr, wait, kill."""

    def __init__(self, pid: int, handles: _Handles, stdin: Any, stdout: Any, stderr: Any, transports: list[Any] | None = None) -> None:
        self.pid = pid
        self._transports = transports or []
        self._h = handles
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        if self._returncode is None:
            api = _load_api()
            import ctypes
            from ctypes import wintypes

            code = wintypes.DWORD()
            if api.kernel32.GetExitCodeProcess(self._h.process, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
                self._returncode = int(code.value)
        return self._returncode

    async def wait(self) -> int:
        api = _load_api()
        while self.returncode is None:
            await asyncio.to_thread(api.kernel32.WaitForSingleObject, self._h.process, 200)
        self._close_transports()
        self._close_handles()
        return int(self._returncode or 0)

    def kill(self) -> None:
        """Ends the whole job: the process and everything it started."""
        api = _load_api()
        if self._h.job:
            api.kernel32.TerminateJobObject(self._h.job, 1)
        else:
            api.kernel32.TerminateProcess(self._h.process, 1)

    terminate = kill
    kill_tree = kill

    def _close_transports(self) -> None:
        for transport in self._transports:
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
        self._transports = []

    def _close_handles(self) -> None:
        api = _load_api()
        for name in ("process", "thread", "job"):
            handle = getattr(self._h, name)
            if handle:
                api.kernel32.CloseHandle(handle)
                setattr(self._h, name, 0)

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002 - mirrors asyncio
        if input and self.stdin is not None:
            self.stdin.write(input)
            await self.stdin.drain()
        if self.stdin is not None:
            self.stdin.close()
        out, err = await asyncio.gather(
            self.stdout.read() if self.stdout is not None else _empty(),
            self.stderr.read() if self.stderr is not None else _empty(),
        )
        await self.wait()
        return out, err


async def _empty() -> bytes:
    return b""


def _resolve_executable(argv0: str, env: dict[str, str]) -> str | None:
    if os.path.isabs(argv0) and os.path.isfile(argv0):
        return argv0
    return shutil.which(argv0, path=env.get("PATH")) or shutil.which(argv0)


def _inheritable_nul() -> int:
    api = _load_api()
    import ctypes

    sa = ctypes.create_string_buffer(24)
    ctypes.memmove(sa, ctypes.byref(ctypes.c_uint32(24)), 4)
    ctypes.memmove(ctypes.byref(sa, 16), ctypes.byref(ctypes.c_int32(1)), 4)  # bInheritHandle = TRUE
    handle = api.kernel32.CreateFileW("\\\\.\\NUL", 0xC0000000, 3, sa, 3, 0, None)
    return int(handle)


def _make_job(memory_mb: int = 0) -> int:
    api = _load_api()
    import ctypes

    job = api.kernel32.CreateJobObjectW(None, None)
    if not job:
        raise WindowsSandboxError("CreateJobObject failed")
    info = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    # kill the tree when the job closes, cap the number of processes, die on unhandled exceptions
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x8 | 0x400
    info.BasicLimitInformation.ActiveProcessLimit = 256
    if memory_mb > 0:  # one runaway allocation must not take the machine down: cap the whole tree
        info.BasicLimitInformation.LimitFlags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = memory_mb * 1024 * 1024
    api.kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    ui = api.JOBOBJECT_BASIC_UI_RESTRICTIONS(0xFF)  # handles, clipboard, system parameters, desktop, global atoms...
    api.kernel32.SetInformationJobObject(job, 4, ctypes.byref(ui), ctypes.sizeof(ui))
    return int(job)


async def spawn(
    argv: list[str],
    policy: Policy,
    *,
    env: dict[str, str] | None = None,
    stdin: bool = False,
    stdout: bool = True,
    stderr: str = "pipe",
) -> WinProcess:
    """Starts `argv` inside the AppContainer. `stderr` is "pipe", "stdout" or "null"."""
    api = _load_api()
    if api is None:
        raise WindowsSandboxError("AppContainers are not available on this system")
    import _winapi
    import ctypes
    from asyncio import windows_utils
    from ctypes import wintypes

    sid, sid_text = container_sid(policy.extras_key)
    child_env = dict(env) if env is not None else sandbox_env(policy)
    executable = _resolve_executable(argv[0], child_env)
    await asyncio.to_thread(prepare, policy, executable)

    loop = asyncio.get_running_loop()
    to_close: list[int] = []
    parent_ends: dict[str, int] = {}

    def inheritable(handle: int) -> int:
        dup = _winapi.DuplicateHandle(_winapi.GetCurrentProcess(), handle, _winapi.GetCurrentProcess(), 0, True, _winapi.DUPLICATE_SAME_ACCESS)
        _winapi.CloseHandle(handle)
        return int(dup)

    try:
        if stdin:
            # duplex: asyncio's write transport also issues a read on its end to notice a closed pipe
            child_in, parent_in = windows_utils.pipe(overlapped=(False, True), duplex=True)
            parent_ends["stdin"] = parent_in
            h_in = inheritable(child_in)
        else:
            h_in = _inheritable_nul()
        to_close.append(h_in)
        if stdout:
            parent_out, child_out = windows_utils.pipe(overlapped=(True, False))
            parent_ends["stdout"] = parent_out
            h_out = inheritable(child_out)
        else:
            h_out = _inheritable_nul()
        to_close.append(h_out)
        if stderr == "stdout":
            h_err = h_out
        elif stderr == "pipe":
            parent_err, child_err = windows_utils.pipe(overlapped=(True, False))
            parent_ends["stderr"] = parent_err
            h_err = inheritable(child_err)
            to_close.append(h_err)
        else:
            h_err = _inheritable_nul()
            to_close.append(h_err)

        # attribute list: the container identity and exactly the three handles to inherit
        size = ctypes.c_size_t(0)
        api.kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not api.kernel32.InitializeProcThreadAttributeList(attrs, 2, 0, ctypes.byref(size)):
            raise WindowsSandboxError("InitializeProcThreadAttributeList failed")
        # Network is a capability the container does not have unless the policy asks for it:
        # "internetClient" (S-1-15-3-1) = outbound connections to the internet. (An audit found no
        # reachable LAN host and no loopback, but raw ICMP/UDP sends are not refused: do not rely
        # on this capability to keep a program away from the local network.)
        capability_sid = ctypes.c_void_p()
        capability_array = None
        if policy.network:
            if not api.advapi32.ConvertStringSidToSidW("S-1-15-3-1", ctypes.byref(capability_sid)):
                raise WindowsSandboxError("cannot build the internetClient capability")
            capability_array = (api.SID_AND_ATTRIBUTES * 1)()
            capability_array[0].Sid = capability_sid
            capability_array[0].Attributes = 0x4  # SE_GROUP_ENABLED
            caps = api.SECURITY_CAPABILITIES(sid, ctypes.cast(capability_array, ctypes.c_void_p), 1, 0)
        else:
            caps = api.SECURITY_CAPABILITIES(sid, None, 0, 0)
        if not api.kernel32.UpdateProcThreadAttribute(attrs, 0, 0x00020009, ctypes.byref(caps), ctypes.sizeof(caps), None, None):
            raise WindowsSandboxError("cannot attach the AppContainer identity")
        unique = list(dict.fromkeys([h_in, h_out, h_err]))  # stderr merged into stdout shares a handle
        handle_array = (ctypes.c_void_p * len(unique))(*unique)
        if not api.kernel32.UpdateProcThreadAttribute(attrs, 0, 0x00020002, handle_array, ctypes.sizeof(handle_array), None, None):
            raise WindowsSandboxError("cannot restrict the inherited handles")

        startup = api.STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        desktop_name = _private_desktop(sid_text) if policy.desktop else None
        desktop_buffer = ctypes.create_unicode_buffer(desktop_name) if desktop_name else None
        if desktop_buffer is not None:
            startup.StartupInfo.lpDesktop = ctypes.cast(desktop_buffer, wintypes.LPWSTR)
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = h_in, h_out, h_err
        startup.lpAttributeList = ctypes.addressof(attrs)
        info = api.PROCESS_INFORMATION()
        block = "".join(f"{k}={v}\0" for k, v in sorted(child_env.items(), key=lambda kv: kv[0].upper())) + "\0"
        env_buffer = ctypes.create_unicode_buffer(block, len(block))
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        flags = 0x00080000 | 0x08000000 | 0x00000400 | 0x00000004  # extended startup, no window, unicode env, suspended
        ok = api.kernel32.CreateProcessW(
            executable, command, None, None, True, flags, env_buffer, str(policy.start_dir), ctypes.byref(startup), ctypes.byref(info)
        )
        error = ctypes.get_last_error()
        api.kernel32.DeleteProcThreadAttributeList(attrs)
        if capability_sid.value:
            api.kernel32.LocalFree(capability_sid)
        if not ok:
            raise WindowsSandboxError(f"CreateProcess failed in the sandbox (error {error}): {argv[0]}")
        job = _make_job(policy.memory_mb)
        if not api.kernel32.AssignProcessToJobObject(job, info.hProcess):
            api.kernel32.TerminateProcess(info.hProcess, 1)
            raise WindowsSandboxError("cannot place the process in its job object")
        api.kernel32.ResumeThread(info.hThread)
    except Exception:
        for handle in parent_ends.values():
            _winapi.CloseHandle(handle)
        raise
    finally:
        for handle in to_close:
            api.kernel32.CloseHandle(handle)

    transports: list[Any] = []

    async def reader(handle: int) -> asyncio.StreamReader:
        stream = asyncio.StreamReader(limit=64 * 1024 * 1024, loop=loop)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stream, loop=loop), windows_utils.PipeHandle(handle)
        )
        transports.append(transport)
        return stream

    class _WriteProtocol(asyncio.streams.FlowControlMixin, asyncio.Protocol):
        pass

    in_stream = None
    if "stdin" in parent_ends:
        protocol = _WriteProtocol(loop=loop)
        transport, _ = await loop.connect_write_pipe(lambda: protocol, windows_utils.PipeHandle(parent_ends["stdin"]))
        in_stream = asyncio.StreamWriter(transport, protocol, None, loop)
        transports.append(transport)
    out_stream = await reader(parent_ends["stdout"]) if "stdout" in parent_ends else None
    err_stream = await reader(parent_ends["stderr"]) if "stderr" in parent_ends else None
    return WinProcess(
        int(info.dwProcessId), _Handles(int(info.hProcess), int(info.hThread), job), in_stream, out_stream, err_stream, transports
    )
