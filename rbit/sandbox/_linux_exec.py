#!/usr/bin/env python3
"""Linux sandbox launcher: confines itself, then replaces itself with the command.

Standalone on purpose (standard library only, no `rbit` import): it runs before any
restriction exists and must not depend on files the sandbox is about to hide.

    python _linux_exec.py '<policy json>' -- command [args...]
    python _linux_exec.py --probe

What it sets up, in this order:

1. new user, mount, pid, ipc, uts namespaces (and a network namespace unless the policy
   allows network): the command is pid 1 of its own pid namespace, cannot see or signal
   other processes, and has no network devices at all;
2. a private mount propagation and a fresh /proc for that pid namespace (the host's
   /proc/<pid>/environ of the parent, with its API keys, is no longer reachable);
3. a new session (no controlling terminal: TIOCSTI keystroke injection is not possible);
4. Landlock: only the listed read-only and read-write paths are accessible. The home
   directory, ~/.ssh, ~/.rbit, /root, other users' files are simply not on the list;
5. no_new_privs, an empty capability set (also bounding and ambient), core dumps off;
6. every inherited file descriptor above 2 is closed.

If ANY step fails the command is NOT run (exit status 126): it never falls back to running
unconfined.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import sys

CLONE_NEWNS = 0x00020000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000

MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REC = 0x4000
MS_PRIVATE = 1 << 18

PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
PR_CAPBSET_DROP = 24
PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4

# Landlock (the syscall numbers are the same on every architecture that has them)
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13  # ABI 2
FS_TRUNCATE = 1 << 14  # ABI 3
FS_IOCTL_DEV = 1 << 15  # ABI 5
NET_BIND_TCP = 1 << 0  # ABI 4
NET_CONNECT_TCP = 1 << 1

_FILE_BITS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV

#: Device nodes a command legitimately needs.
_DEVICES = ("/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty")

EXIT_UNAVAILABLE = 126


class SandboxError(RuntimeError):
    pass


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _check(result: int, what: str) -> None:
    if result != 0:
        code = ctypes.get_errno()
        raise SandboxError(f"{what}: {os.strerror(code)} (errno {code})")


# ---------------------------------------------------------------- namespaces


def _write_file(path: str, text: str) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(text)


def _enter_namespaces(libc: ctypes.CDLL, network: bool) -> None:
    uid, gid = os.getuid(), os.getgid()
    flags = CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWIPC | CLONE_NEWUTS
    if not network:
        flags |= CLONE_NEWNET
    _check(libc.unshare(flags), "unshare (user namespaces unavailable?)")
    # The user keeps its own uid/gid inside: no apparent root, no setgroups.
    _write_file("/proc/self/setgroups", "deny")
    _write_file("/proc/self/uid_map", f"{uid} {uid} 1\n")
    _write_file("/proc/self/gid_map", f"{gid} {gid} 1\n")


def _private_proc(libc: ctypes.CDLL) -> None:
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
    _check(libc.mount(b"none", b"/", None, MS_REC | MS_PRIVATE, None), "mount --make-rprivate /")
    _check(libc.mount(b"proc", b"/proc", b"proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, None), "mount /proc")


# ------------------------------------------------------------------ privileges


def _drop_privileges(libc: ctypes.CDLL) -> None:
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    _check(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(NO_NEW_PRIVS)")
    libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    for cap in range(0, 64):
        if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0 and ctypes.get_errno() == errno.EINVAL:
            break

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]

    header = Header(0x20080522, 0)
    data = (Data * 2)()
    _check(libc.capset(ctypes.byref(header), data), "capset(empty)")


# -------------------------------------------------------------------- landlock


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _landlock_masks(abi: int) -> tuple[int, int, int]:
    """(handled, read-only access, read-write access) for this kernel's ABI."""
    read = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
    write = (
        FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_CHAR | FS_MAKE_DIR | FS_MAKE_REG
        | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM
    )
    if abi >= 2:
        write |= FS_REFER
    if abi >= 3:
        write |= FS_TRUNCATE
    if abi >= 5:
        write |= FS_IOCTL_DEV
    return read | write, read, write


def _apply_landlock(libc: ctypes.CDLL, read_paths: list[str], write_paths: list[str], network: bool) -> int:
    libc.syscall.restype = ctypes.c_long
    abi = int(libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION))
    if abi < 1:
        raise SandboxError("Landlock is not available in this kernel (needs Linux 5.13+ with Landlock enabled)")
    handled, read, write = _landlock_masks(abi)
    attr = _RulesetAttr(handled, 0)
    size = 8
    if abi >= 4 and not network:
        attr.handled_access_net = NET_BIND_TCP | NET_CONNECT_TCP  # no rules: all TCP bind/connect denied
        size = 16
    fd = int(libc.syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), size, 0))
    if fd < 0:
        raise SandboxError(f"landlock_create_ruleset: {os.strerror(ctypes.get_errno())}")

    def allow(path: str, access: int) -> None:
        try:
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return  # a path that does not exist grants nothing
        try:
            if not os.path.isdir(path):
                access &= _FILE_BITS
            rule = _PathBeneath(access & handled, parent)
            if rule.allowed_access and libc.syscall(SYS_LANDLOCK_ADD_RULE, fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(rule), 0) != 0:
                raise SandboxError(f"landlock_add_rule({path}): {os.strerror(ctypes.get_errno())}")
        finally:
            os.close(parent)

    for path in read_paths:
        allow(path, read)
    for path in _DEVICES:
        allow(path, FS_READ_FILE | FS_WRITE_FILE | FS_IOCTL_DEV)
    for path in write_paths:
        allow(path, read | write)
    result = libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, fd, 0)
    os.close(fd)
    if result != 0:
        raise SandboxError(f"landlock_restrict_self: {os.strerror(ctypes.get_errno())}")
    return abi


# --------------------------------------------------------------------- seccomp

PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_KILL_PROCESS = 0x80000000
AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}

#: Syscalls a build tool never needs and an escape would: answered with EPERM.
_DENIED_SYSCALLS: dict[str, dict[str, int]] = {
    "x86_64": {
        "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311, "mount": 165, "umount2": 166,
        "pivot_root": 155, "chroot": 161, "reboot": 169, "swapon": 167, "swapoff": 168, "acct": 163,
        "init_module": 175, "finit_module": 313, "delete_module": 176, "kexec_load": 246,
        "kexec_file_load": 320, "unshare": 272, "setns": 308, "bpf": 321, "keyctl": 250, "add_key": 248,
        "request_key": 249, "perf_event_open": 298, "userfaultfd": 323, "io_uring_setup": 425,
        "io_uring_enter": 426, "io_uring_register": 427, "open_by_handle_at": 304, "mount_setattr": 442,
        "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "open_tree": 428,
        "syslog": 103, "iopl": 172, "ioperm": 173, "modify_ldt": 154, "quotactl": 179, "vhangup": 153,
    },
    "aarch64": {
        "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271, "mount": 40, "umount2": 39,
        "pivot_root": 41, "chroot": 51, "reboot": 142, "swapon": 224, "swapoff": 225, "acct": 89,
        "init_module": 105, "finit_module": 273, "delete_module": 106, "kexec_load": 104,
        "kexec_file_load": 294, "unshare": 97, "setns": 268, "bpf": 280, "keyctl": 219, "add_key": 217,
        "request_key": 218, "perf_event_open": 241, "userfaultfd": 282, "io_uring_setup": 425,
        "io_uring_enter": 426, "io_uring_register": 427, "open_by_handle_at": 265, "mount_setattr": 442,
        "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "open_tree": 428,
        "syslog": 116, "quotactl": 60, "vhangup": 58,
    },
}
_CLONE_NR = {"x86_64": 56, "aarch64": 220}
_EPERM = 1  # Linux errno values, fixed: this table is also built (and tested) on other systems
_ENOSYS = 38
_CLONE3_NR = 435
#: `clone` flags that create a namespace: a sandboxed command may fork, never re-namespace.
_CLONE_NS_FLAGS = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000 | 0x80


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def _seccomp_program(machine: str) -> list[tuple[int, int, int, int]] | None:
    """The BPF program (code, jt, jf, k) for this architecture, or None when unsupported.

    Layout (N = number of denied syscalls):

        0        load arch; 1 jump over 2 when it is the expected one; 2 kill otherwise
        3        load the syscall number
        4..N+3   one equality test per denied syscall            -> EPERM
        N+4      clone                                           -> flag test
        N+5      clone3 (its flags are out of reach: libc falls back to clone) -> ENOSYS
        N+6      ALLOW everything else
        N+7      flag test: load clone flags; any namespace flag -> EPERM, else ALLOW
        N+9..    ALLOW (after the flag test), EPERM, ENOSYS
    """
    if machine not in AUDIT_ARCH:
        return None
    ld, jeq, jset, ret = 0x20, 0x15, 0x45, 0x06
    denied = sorted(_DENIED_SYSCALLS[machine].values())
    n = len(denied)
    clone_test = n + 7
    allow_clone = n + 9
    eperm = n + 10
    enosys = n + 11
    program: list[tuple[int, int, int, int]] = [
        (ld, 0, 0, 4),
        (jeq, 1, 0, AUDIT_ARCH[machine]),
        (ret, 0, 0, SECCOMP_RET_KILL_PROCESS),  # a foreign-ABI call (x32/compat) is never legitimate
        (ld, 0, 0, 0),
    ]
    for nr in denied:
        program.append((jeq, eperm - len(program) - 1, 0, nr))
    program.append((jeq, clone_test - len(program) - 1, 0, _CLONE_NR[machine]))
    program.append((jeq, enosys - len(program) - 1, 0, _CLONE3_NR))
    program.append((ret, 0, 0, SECCOMP_RET_ALLOW))
    assert len(program) == clone_test, (len(program), clone_test)
    program.append((ld, 0, 0, 16))  # clone flags = args[0], low word
    program.append((jset, eperm - len(program) - 1, allow_clone - len(program) - 1, _CLONE_NS_FLAGS))
    program.append((ret, 0, 0, SECCOMP_RET_ALLOW))
    program.append((ret, 0, 0, SECCOMP_RET_ERRNO | _EPERM))
    program.append((ret, 0, 0, SECCOMP_RET_ERRNO | _ENOSYS))
    return program


def _apply_seccomp(libc: ctypes.CDLL) -> bool:
    program = _seccomp_program(os.uname().machine)
    if program is None:
        return False  # unknown architecture: Landlock, no_new_privs and empty capabilities still apply
    filters = (_SockFilter * len(program))(*[_SockFilter(*insn) for insn in program])
    prog = _SockFprog(len(program), filters)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    _check(libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0), "seccomp filter")
    return True


# ------------------------------------------------------------------------ main


def _close_inherited_fds() -> None:
    try:
        limit = min(os.sysconf("SC_OPEN_MAX"), 65536)
    except (ValueError, OSError):
        limit = 4096
    os.closerange(3, limit)


def _run_confined(spec: dict, command: list[str]) -> None:
    """Runs in the pid-namespace child: confine, then exec (never returns)."""
    libc = _libc()
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
    _private_proc(libc)
    os.setsid()
    workspace = str(spec.get("workspace") or "/")
    try:
        os.chdir(workspace)
    except OSError:
        os.chdir("/")
    _check(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(NO_NEW_PRIVS)")
    _apply_landlock(libc, list(spec.get("read", [])), list(spec.get("write", [])), bool(spec.get("network")))
    _drop_privileges(libc)
    _apply_seccomp(libc)
    _close_inherited_fds()
    os.execvp(command[0], command)


def launch(spec: dict, command: list[str]) -> int:
    libc = _libc()
    _enter_namespaces(libc, bool(spec.get("network")))
    child = os.fork()  # the first process of a new pid namespace is pid 1: it must be a child
    if child == 0:
        try:
            _run_confined(spec, command)
        except SandboxError as exc:
            sys.stderr.write(f"rbit-sandbox: {exc}\n")
            os._exit(EXIT_UNAVAILABLE)
        except OSError as exc:  # exec failed: report like a shell would
            sys.stderr.write(f"rbit-sandbox: cannot run {command[0]}: {exc.strerror}\n")
            os._exit(127 if exc.errno == errno.ENOENT else 126)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), lambda signum, _f, c=child: _forward(c, signum))
    while True:
        try:
            _pid, status = os.waitpid(child, 0)
        except InterruptedError:
            continue
        except ChildProcessError:
            return EXIT_UNAVAILABLE
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)


def _forward(child: int, signum: int) -> None:
    try:
        os.kill(child, signum)
    except ProcessLookupError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--probe":
        spec, command = {"workspace": "/", "read": ["/usr", "/bin", "/lib", "/lib64"], "write": [], "network": False}, ["/bin/true"]
    else:
        if len(argv) < 4 or argv[2] != "--":
            sys.stderr.write("usage: _linux_exec.py '<policy json>' -- command [args...]\n")
            return 2
        try:
            spec = json.loads(argv[1])
        except ValueError as exc:
            sys.stderr.write(f"rbit-sandbox: bad policy: {exc}\n")
            return 2
        command = argv[3:]
    try:
        return launch(spec, command)
    except (SandboxError, OSError) as exc:
        sys.stderr.write(f"rbit-sandbox: {exc}\n")
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
