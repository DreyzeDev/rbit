# rbit sandbox: security audit (Windows 11, Linux; macOS not tested)

**Status: findings fixed and re-tested on Windows 11 and Linux. macOS is UNVERIFIED** (no Mac was
available; see [macos-verification.md](macos-verification.md) for how to check one).

## Scope and honest limits of this audit

* **What was audited:** the OS-level sandbox in `rbit/sandbox/` (Windows AppContainer + Job Object,
  Linux launcher with namespaces/Landlock/seccomp, macOS Seatbelt profile), the places that start
  programs through it (`bash`, `terminal`, `git`/`ripgrep`/`ruff`, formatters, LSP, MCP servers),
  and the trust prompt (`rbit trust`) for MCP entries.
* **Who audited:** two *independent* AI red-team sessions (separate context, told to break out and
  report, not to fix), one per platform. They are **not human penetration testers**, they ran on the same
  machine and share the model family of the code's author. Treat the result as a thorough
  adversarial test campaign, not as a certification. The author then re-ran the auditors' own
  attack scripts against the fixed code (below).
* **Environments:** Windows 11 (build 10.0.26300); Linux = WSL2 Ubuntu 24.04, kernel 6.18.33.2, x86_64
  (not a native Linux host; WSL adds its own bridges, which turned out to matter). aarch64 seccomp
  numbers are untested on real hardware.
* **Not tested:** macOS; kernel/OS vulnerabilities (any sandbox can be defeated by one); other Windows
  and Linux versions; multi-user machines; long-running behaviour.

## Results at a glance

| | Windows 11 | Linux (WSL2) |
|---|---|---|
| Attack attempts recorded | 125 | 137 |
| Held (blocked) | 81 | 91 |
| Escapes / partial | 27 / 11 | 18 / 6 |
| Built-in `rbit sandbox check` after the fixes | **15 / 15** | **22 / 22** as `nobody` **and** as root |
| Test-suite (whole project) | 790 passed, 7 skipped, 0 failed | 777 passed, 14 skipped, 6 failed (as root **and** as `nobody`) |

The six Linux failures are **not caused by the sandbox**: the same six tests fail identically on the
commit before the sandbox existed (`c678326`). Four are environment or platform issues (no `ruff`
installed; a test written in PowerShell syntax; a service test that sees an un-reaped child as
still alive), two look like real, older Linux bugs unrelated to this work (the persistent `terminal`
tool echoes the command back under `sh -i`; one service-stop check). The first Linux run also found
four real problems in the sandbox that the Windows run could not: WSL's Windows `PATH` entries
(`/mnt/c/...`) were being mounted into the sandbox, the launcher exited with a Python error instead
of refusing when its policy lacked a root, and two tests had only ever run on Windows. All fixed.

## Findings

Severity is the auditor's, adjusted where re-testing showed more (or less). "Fixed" means the
original attack was re-run against the new code and now fails, and a regression test exists.

| # | Platform | Severity | Finding | Status |
|---|---|---|---|---|
| 1 | Linux | **High** (Critical for the WSL bridge, not exploited) | A confined program could `connect()` to **unix sockets outside the workspace**: ssh-agent, `docker.sock`, D-Bus, the WSL interop socket to Windows. Landlock does not govern `connect()` or path lookup. | **Fixed**: the launcher now builds a new root with `pivot_root` (tmpfs + binds of only the granted paths). Before: connected, banner received; after: `No such file or directory`. |
| 2 | Linux | Medium | Abstract unix sockets of the host reachable when `shell_sandbox_network` is on. | **Fixed**: Landlock scope (kernel 6.12+). Before: connected; after: `Operation not permitted`. |
| 3 | Linux | Medium/Low | `AF_VSOCK` (VM/WSL host channel), `name_to_handle_at`, `kcmp`, `pidfd_getfd`, `fanotify`, `AF_PACKET` not blocked. | **Fixed** in the seccomp filter. |
| 4 | Linux | High (root) | `/etc/shadow` readable as root (`/etc` granted as a whole). | **Fixed**: masked with an empty file; `sudoers*`, `ssl/private` hidden. |
| 5 | Linux | Medium / Low-Medium | `.git/hooks` writable (a planted hook runs later *outside* the sandbox); `.rbit/` writable (known limit). | **Fixed**: `.rbit` is hidden, `.git/hooks` and `.git/config` read-only (mounts). |
| 6 | Linux | Info | `/etc/resolv.conf` not readable, so no DNS with network on (WSL points it outside `/etc`). | **Fixed**; DNS works with network on, fails with it off. |
| 7 | both | **High** | An MCP entry's `cwd` became the sandbox **workspace**: with `cwd=~/.ssh` the server read the key and wrote into `.ssh` / `~/.rbit`. | **Fixed**: the workspace is always the project; a `cwd` outside it makes the server not start. Regression test. |
| 8 | both | High (config) / Low | The credential-folder filter only matched exact entries: `read: ["~"]` (or a PATH entry that is `~`) exposed `~/.ssh`. | **Fixed**: paths that *are* the home, an ancestor of it, or *contain* a credential folder are dropped; on Linux the folders are also masked inside. |
| 9 | both | High (trust UX) | `rbit trust` showed only `starts MCP servers: x -> <command>`, hiding `cwd`, `sandbox.read/write/network/env` and `environment`. | **Fixed**: all are listed (variable *values* never). A change re-triggers trust (the fingerprint covers the whole entry). |
| 10 | Windows | **High** | A grant on the workspace reached **outside files hard-linked into it** (same file object, several names). Junctions did *not* propagate. | **Fixed**: hard-linked files are excluded from inheritance before the grant. The test failed before the fix and passes after. |
| 11 | Windows | Medium | The AppContainer identity was shared: a folder opened for one MCP server stayed open for every other program. | **Fixed**: one container identity per set of extra folders. Regression test. |
| 12 | Windows | Medium (design limit) | `.git/hooks` writable from inside. | **Fixed**: read-only for the container. |
| 13 | Windows | Low | Stale grants ledger: a deleted and re-created folder was silently denied. | **Fixed**: the ledger records the folder's identity. |
| 14 | Windows | Low | No memory limit on the job (1 GiB allocated). | **Fixed**: job memory limit (default 8 GiB, `shell_sandbox_memory_mb`). Test: a 700 MB allocation under a 256 MB limit fails. |
| 15 | Windows | Medium | The sandbox could read the desktop's pixels. | **Fixed, effect not measurable here**: programs now run on a private desktop (python, cmd, PowerShell, git all work on it). The auditor's probe is `BLOCKED` on re-run, but in this session the screen is uniform even without a sandbox, so the control cannot tell the variants apart. |
| 16 | Windows | Medium | **DPAPI**: `CryptUnprotectData` succeeds inside the sandbox for a user-protected blob the sandbox can read. | **Not fixable by ACL.** Documented: keep DPAPI blobs and secrets out of workspaces; the AppContainer is not a DPAPI boundary. |
| 17 | Windows | Low | `\\.\pipe\epmapper` (RPC endpoint mapper) opens; about 190 pipe names are listable. No RPC was exercised. | Open, documented. |
| 18 | Windows | Info | Raw ICMP / UDP `sendto` return success with network off. | **Measured, not an escape**: a DNS query to `1.1.1.1:53` gets **no reply** with network off and a reply with it on (as an unsandboxed program does); packets to the LAN address and loopback were never delivered. |
| 19 | Windows | Info | A planted `.rbit` junction: the ACL change hit only the link; the target was untouched. | Not reproduced. |
| 19b | Linux (WSL) | Medium | WSL appends the Windows `PATH` (`/mnt/c/Windows/...`); those directories were mounted into the sandbox. | **Fixed**: Windows drive mounts are never exposed. Found by the first Linux `pytest` run. |
| 20 | Linux | Low | No pid/memory/disk limits (fork bomb capped at 200 in the test, 256 MiB allocation). | Open: needs cgroups, unavailable here (WSL without delegation). |
| 21 | Linux | Low | `memfd_create` + `exec` runs code from memory (Landlock exec is path-based). | Accepted: the code is still confined. |
| 22 | both | By design | A *trusted* MCP entry can explicitly open network, folders and parent variables (`sandbox.*`). | Not a bug: `rbit trust` now shows each of these. |

### Re-test of the auditors' own attack scripts

* **Linux:** the auditor's 130-step driver was re-run against the fixed code as `nobody`.
  Records whose output contained the planted secret: **4 before, 0 after.** Unix sockets (path, WSL
  interop, WSLg, journal), VSOCK, `name_to_handle_at`, `.rbit`, `.git/hooks` and all four MCP/secret
  probes flipped from reachable to blocked; the controls (in-workspace socket, seccomp/NoNewPrivs
  state) still behave as before.
* **Windows:** the auditor's filesystem, MCP and process scripts (97 attacks present in both runs)
  were re-run. Hard link via the sandbox (`C2a`, `C2b`), desktop capture (`D17`) and git hooks (`I3`)
  flipped to blocked. Rows still flagged are: `C4` (a bare `icacls` command, i.e. the OS's own
  behaviour without rbit code: it shows *why* the fix is needed), DPAPI, epmapper, `whoami`, the
  Info items above, and the explicit MCP widenings (approved through `rbit trust`).
  Two rows are artefacts of the scripts, not of the sandbox: `J2` (the canary file was missing:
  `FileNotFoundError`, not `PermissionError`) and `D22` (the child's own handles: DLLs, console,
  standard pipes; only the three standard handles are inherited).

## What the sandbox does and does not promise

Confined programs (`bash`, `terminal`, `git`, `ripgrep`, `ruff`, formatters, LSP and MCP servers) get:
the project workspace (read/write) and a private scratch folder; read-only system and interpreter;
no home directory, no credential folders, no `~/.rbit` (config with the API key), no project `.rbit`
state, no network by default, no view of other processes, no privilege escalation, a filtered
environment. **Not covered:** Python plugins (they run inside rbit: not loaded under `strict`),
DPAPI (Windows), resource limits on Linux, kernel or OS vulnerabilities, macOS (unverified).

## Reproduce

```text
rbit sandbox check            # measures the isolation on this machine (15 checks on Windows, 22 on Linux)
python -m pytest tests/test_sandbox_isolation.py
```

The auditors' scripts and raw evidence (`results.jsonl`, evidence JSON, attack drivers) are kept
outside the repository next to the working session; ask for them if you want to re-run the campaign.


---

## 2026-11-26 Multi-OS Re-Test & Red-Team Summary

Automated re-verification after hardening macOS Seatbelt (`rbit/sandbox/macos.py`), Linux `bwrap` fallback (`rbit/sandbox/linux.py`), symlink escape checks (`rbit/sandbox/selftest.py`), full-tree plugin provenance (`rbit/install.py`), and atomic subagent budget reservation (`rbit/agent.py`):

| OS / Environment | Backend | `rbit sandbox check` | Isolation & Core Tests | Known Findings / Notes |
|---|---|---|---|---|
| **Windows 11** (local host) | AppContainer + Job Object | **15 / 15 PASS** | `tests/test_sandbox_isolation.py`: **32 passed, 1 skipped** (symlink test skipped when unprivileged symlink creation is disabled) | `New-PSDrive` works when temp/workspace are on `C:`; on Windows Server 2022 CI (`D:\` temp), `powershell.exe` `FileSystem` provider hits `DriveNotFound` while direct `rbit sandbox check` passes 15/15 |
| **macOS arm64** (`macos-latest` CI) | Seatbelt (`sandbox-exec`) | **14 / 14 PASS** (after fixing `/private/var/folders`, `HOME` read, `.rbit` read, `.git/hooks`, symlink escape) | `smoke (macos-latest)` **GREEN** (`test_fix_core`, `test_long_horizon`, `test_fix_sandbox`, `test_sandbox_isolation`) | First CI run exposed 4 bypasses (9/13); all 4 closed in `f81b540` + `fb6e38b` |
| **Linux** (`ubuntu-latest` CI, Ubuntu 24.04) | `bwrap` fallback (AppArmor restricts unprivileged `userns` for custom `_linux_exec.py`) | **PASS** (with `.rbit` tmpfs & `.git/hooks` ro-bind added in `48e4b6e`) | `test (3.10, 3.11, 3.12)`: **825+ passed**, `ruff` + `mypy` **GREEN** | Full Landlock + `pivot_root` + `seccomp` (`AF_VSOCK`, abstract unix sockets) verified on WSL2 kernel 6.18 (22/22); `bwrap` fallback on Ubuntu 24.04 cannot install `seccomp` or `pivot_root` |

### Attack Vectors Tested & Blocked
- **Outside workspace read/write**: Blocked on Windows 11, macOS (`sandbox-exec`), and Linux (`landlock` / `bwrap`).
- **Workspace symlink escape (`workspace/escape -> ../outside/secret.txt`)**: Blocked on macOS and Linux (`selftest.py`).
- **Project state tampering (`.rbit/config.json`) & `.git/hooks` planting**: Read and write blocked on Windows 11, macOS, and Linux (`landlock` + `bwrap`).
- **Credential & env leak (`~/.rbit`, `~/.ssh`, `RBIT_SANDBOX_CANARY`, LLM API keys)**: Blocked and scrubbed via `envscrub.py`.
- **Network & loopback escape**: Blocked on Windows 11, macOS, and Linux.
- **Prompt injection (Browser / `webfetch` / GitHub Issues & PRs)**: Stripped zero-width characters, flagged injection directives, and fenced inside `<untrusted_web_content>`.
- **Plugin code tampering**: Full-tree SHA-256 digest (`_plugin_manifest_digest` in `rbit/install.py`) detects modifications to any `.py` or manifest file after install.
- **Concurrent subagent budget overspend**: Prevented via `asyncio.Lock` atomic reservation (`_run_subagent` in `rbit/agent.py`).
